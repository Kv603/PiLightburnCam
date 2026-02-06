import os
import sys
import yaml
import logging
import logging.handlers
import argparse
import syslog
import time
from datetime import datetime, UTC
from threading import Lock
from io import BytesIO

from flask import Flask, request, Response, render_template_string, abort, redirect, g
from picamera2 import Picamera2
from apscheduler.schedulers.background import BackgroundScheduler
import piexif
import piexif.helper
from PIL import Image

# --- Logging Setup ---
class SyslogHandler(logging.Handler):
    def emit(self, record):
        priority = syslog.LOG_INFO
        if record.levelno >= logging.ERROR: priority = syslog.LOG_ERR
        elif record.levelno >= logging.WARNING: priority = syslog.LOG_WARNING
        syslog.syslog(priority, f"PicameraService: {self.format(record)}")

logger = logging.getLogger("PicameraService")
logger.setLevel(logging.INFO)
logger.addHandler(SyslogHandler())

app = Flask(__name__)
picam = None
cache_lock = Lock()
latest_snapshot = {"data": None, "timestamp": None, "stale": True, "raw_bytes": None}
config = {}
cam_overrides = {}

# --- Helper Logic ---

def get_log_level(level_name):
    levels = {
        'debug': logging.DEBUG,
        'info': logging.INFO,
        'warning': logging.WARNING,
        'error': logging.ERROR
    }
    return levels.get(level_name.lower()) if level_name else None

@app.before_request
def start_timer():
    g.start_time = time.perf_counter()

@app.after_request
def log_request_timed(response):
    elapsed_ms = (time.perf_counter() - g.start_time) * 1000
    level_name = config.get('logging', {}).get(f'{request.method.lower()}_level')
    level = get_log_level(level_name)
    
    if level:
        msg = f"{request.method} {request.path} - {response.status_code}"
        if request.method == "GET":
            msg += f" ({elapsed_ms:.2f}ms)"
        logger.log(level, msg)
    return response

def get_constrained_val(key, requested_val, sensor_limit=None):
    hard_limits = config.get('limits', {})
    floor = hard_limits.get(f'min_{key}', 0)
    try:
        val = max(floor, int(requested_val or 0))
    except (ValueError, TypeError):
        val = floor
    if sensor_limit:
        val = min(val, sensor_limit)
    return val

# --- Capture & Processing ---

def capture_to_buffer():
    global picam
    buf = BytesIO()
    try:
        metadata = picam.capture_file(buf, format='jpeg')
        return buf.getvalue(), datetime.now(UTC), metadata
    except Exception as e:
        if "Camera must be started" not in str(e):
            logger.error(f"Hardware capture failed: {e}")
        return None, None, None

def process_image(raw_bytes, ext, ts, metadata=None):
    ext = ext.lower()
    if ext in ['jpg', 'jpeg']:
        exif_dict = {"0th": {piexif.ImageIFD.Model: u"PiCam2-Service"}, "Exif": {}}
        meta_str = f"TS: {ts.isoformat()} | Meta: {str(metadata) if metadata else 'Cached'}"
        exif_dict["Exif"][piexif.ExifIFD.UserComment] = piexif.helper.UserComment.dump(meta_str, encoding="unicode")
        output = BytesIO()
        try:
            piexif.insert(piexif.dump(exif_dict), raw_bytes, output)
            return output.getvalue()
        except: return raw_bytes
    img = Image.open(BytesIO(raw_bytes))
    out = BytesIO()
    if ext == 'bmp': img.save(out, format='BMP')
    elif ext == 'png':
        q = cam_overrides.get('quality', 90)
        img.save(out, format='PNG', compress_level=int((100-q)/11))
    elif ext == 'gif': img.save(out, format='GIF')
    return out.getvalue()

def background_update():
    global latest_snapshot
    if picam is None: return
    raw, ts, meta = capture_to_buffer()
    if raw:
        with cache_lock:
            latest_snapshot.update({
                "raw_bytes": raw,
                "data": process_image(raw, 'jpg', ts, meta),
                "timestamp": ts,
                "stale": False
            })

# --- Endpoints ---

@app.route('/camera')
def camera_info():
    infos = Picamera2.global_camera_info()
    current_id = getattr(picam, 'camera_id', None)
    html = "<h2>Discovered Cameras</h2><table border='1'><tr><th>Index</th><th>Model</th><th>ID</th><th>Usage</th></tr>"
    for i, info in enumerate(infos):
        is_active = "<strong>ACTIVE</strong>" if i == picam.camera_index else ""
        cfg_line = f"camera_id: '{info['id']}'"
        html += f"<tr><td>{i}</td><td>{info['model']}</td><td>{info['id']}</td><td>{is_active}<br><code>{cfg_line}</code></td></tr>"
    html += "</table><br><a href='/'>Back</a>"
    return html

@app.route('/focus', methods=['GET', 'POST'])
def focus_control():
    if 'AfMode' not in picam.camera_controls:
        return "Autofocus unsupported with current camera. <a href='/'>Back</a>"
    
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'set_mode':
            mode = int(request.form.get('mode'))
            picam.set_controls({"AfMode": mode})
            cam_overrides['AfMode'] = mode
        elif action == 'trigger':
            picam.autofocus_cycle()
        elif action == 'lock':
            # Set to manual (0) and save current lens position
            pos = getattr(picam.controls, 'LensPosition', 0.0)
            picam.set_controls({"AfMode": 0, "LensPosition": pos})
            cam_overrides['AfMode'] = 0
            cam_overrides['LensPosition'] = pos
        
        # Save persistence
        with open(config.get('camera_config_path'), 'w') as f:
            yaml.dump(cam_overrides, f)

    cur_mode = getattr(picam.controls, 'AfMode', 0)
    html = f"<h2>Focus Control (Current Mode: {cur_mode})</h2>"
    html += "<form method='POST'><input type='hidden' name='action' value='set_mode'>"
    html += "<button name='mode' value='0'>Manual</button>"
    html += "<button name='mode' value='1'>Auto (Single)</button>"
    html += "<button name='mode' value='2'>Continuous</button></form>"
    
    if cur_mode == 0:
        html += "<form method='POST'><input type='hidden' name='action' value='trigger'><button>Trigger AfCycle</button></form>"
    
    html += "<form method='POST'><input type='hidden' name='action' value='lock'><button style='background: #ffcc00'>Lock Focus & Save</button></form>"
    html += "<br><a href='/'>Back</a>"
    return html

@app.route('/snapshot.<ext>')
def snapshot(ext):
    ext = ext.lower()
    if ext not in ['jpg', 'jpeg', 'bmp', 'png', 'gif']: abort(404)
    cached_req = request.args.get('cached', '').lower() in ['1', 'true']
    data = None
    with cache_lock:
        if latest_snapshot['raw_bytes'] and (cached_req or not latest_snapshot['stale']):
            ts = latest_snapshot['timestamp']
            data = latest_snapshot['data'] if ext in ['jpg', 'jpeg'] else process_image(latest_snapshot['raw_bytes'], ext, ts)
            latest_snapshot['stale'] = True
    if data is None:
        raw, ts, meta = capture_to_buffer()
        if raw: data = process_image(raw, ext, ts, meta)
    if not data: abort(503)
    resp = Response(data, mimetype=f"image/{ext}")
    resp.headers['Last-Modified'] = ts.strftime('%a, %d %b %Y %H:%M:%S GMT')
    return resp

# ... (Include /config, /set, /reboot from previous iterations) ...

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config", default="config.yaml")
    parser.add_argument("-p", "--port", type=int)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--camera-id", type=str)
    args = parser.parse_args()

    with open(args.config, 'r') as f: config = yaml.safe_load(f)
    if args.verbose: logger.addHandler(logging.StreamHandler(sys.stderr))

    # --- Camera Selection ---
    cam_id = args.camera_id or config.get('camera_id')
    cam_index = 0
    if cam_id:
        found = False
        for i, info in enumerate(Picamera2.global_camera_info()):
            if info['id'] == cam_id:
                cam_index = i
                found = True
                break
        if not found:
            logger.warning(f"Configured camera ID {cam_id} not found. Defaulting to index 0.")

    picam = Picamera2(cam_index)
    picam.camera_index = cam_index # Store for /camera UI
    
    # --- Initialization ---
    sensor_size = picam.sensor_modes[0]['size']
    cam_path = config.get('camera_config_path', 'config-camera.yaml')
    if os.path.exists(cam_path):
        with open(cam_path, 'r') as f: cam_overrides = yaml.safe_load(f) or {}

    w = get_constrained_val('width', cam_overrides.get('width', sensor_size[0]), sensor_size[0])
    h = get_constrained_val('height', cam_overrides.get('height', sensor_size[1]), sensor_size[1])
    picam.configure(picam.create_video_configuration(main={'size': (w, h)}))
    picam.start()

    # Apply Focus & Controls
    if 'LensPosition' in cam_overrides and 'LensPosition' in picam.camera_controls:
        picam.set_controls({"AfMode": 0, "LensPosition": cam_overrides['LensPosition']})
    elif 'AfMode' in cam_overrides:
        picam.set_controls({"AfMode": cam_overrides['AfMode']})

    sched = BackgroundScheduler()
    sched.add_job(background_update, 'interval', seconds=max(0.1, cam_overrides.get('capture_interval', 2.0)))
    sched.start()

    app.run(host='0.0.0.0', port=args.port or config['web']['port'])
