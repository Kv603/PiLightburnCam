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

from flask import Flask, request, Response, render_template_string, abort, redirect
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

# --- Global State ---
app = Flask(__name__)
picam = None
cache_lock = Lock()
# We store the 'raw' jpeg bytes to allow conversion to BMP/PNG/GIF on demand
latest_snapshot = {"data": None, "timestamp": None, "stale": True, "raw_bytes": None}
config = {}
cam_overrides = {}

# --- Logic: Constraints & Capture ---

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

def capture_to_buffer():
    """Captures a high-quality JPEG and returns raw bytes + metadata."""
    global picam
    buf = BytesIO()
    try:
        # We always capture JPEG as the 'master' format to allow Exif injection
        metadata = picam.capture_file(buf, format='jpeg')
        ts = datetime.now(UTC)
        return buf.getvalue(), ts, metadata
    except Exception as e:
        if "Camera must be started" not in str(e):
            logger.error(f"Hardware capture failed: {e}")
        return None, None, None

def process_image(raw_bytes, ext, ts, metadata=None):
    """Handles format conversion and Exif injection."""
    ext = ext.lower()

    # Path for JPEGs (with Exif)
    if ext in ['jpg', 'jpeg']:
        exif_dict = {"0th": {piexif.ImageIFD.Model: u"PiCam2-Service"}, "Exif": {}}
        meta_str = f"TS: {ts.isoformat()} | Meta: {str(metadata) if metadata else 'Cached'}"
        exif_dict["Exif"][piexif.ExifIFD.UserComment] = piexif.helper.UserComment.dump(meta_str, encoding="unicode")

        output = BytesIO()
        try:
            piexif.insert(piexif.dump(exif_dict), raw_bytes, output)
            return output.getvalue()
        except:
            return raw_bytes # Fallback to raw if exif fails

    # Path for BMP, PNG, GIF
    img = Image.open(BytesIO(raw_bytes))
    out = BytesIO()
    if ext == 'bmp':
        img.save(out, format='BMP')
    elif ext == 'png':
        q = cam_overrides.get('quality', 90)
        img.save(out, format='PNG', compress_level=int((100-q)/11))
    elif ext == 'gif':
        img.save(out, format='GIF')
    return out.getvalue()

def background_update():
    global latest_snapshot
    if picam is None: return

    raw, ts, meta = capture_to_buffer()
    if raw:
        with cache_lock:
            latest_snapshot.update({
                "raw_bytes": raw,
                "data": process_image(raw, 'jpg', ts, meta), # Cache the JPG version
                "timestamp": ts,
                "stale": False
            })

# --- Routes ---

@app.route('/snapshot.<ext>')
def snapshot(ext):
    ext = ext.lower()
    if ext not in ['jpg', 'jpeg', 'bmp', 'png', 'gif']: abort(404)

    cached_req = request.args.get('cached', '').lower() in ['1', 'true']
    data = None
    ts = None

    with cache_lock:
        # Use Cache if available and requested (or not stale)
        if latest_snapshot['raw_bytes'] and (cached_req or not latest_snapshot['stale']):
            ts = latest_snapshot['timestamp']
            # If they want JPG, we already have it processed in latest_snapshot['data']
            if ext in ['jpg', 'jpeg']:
                data = latest_snapshot['data']
            else:
                data = process_image(latest_snapshot['raw_bytes'], ext, ts)
            latest_snapshot['stale'] = True

    # If no cache available or requested fresh
    if data is None:
        raw, ts, meta = capture_to_buffer()
        if raw:
            data = process_image(raw, ext, ts, meta)
        else:
            # Final fallback to placeholder
            p_path = config['web'].get('placeholder_image', 'placeholder.jpg')
            if os.path.exists(p_path):
                with open(p_path, 'rb') as f: data = f.read()
                ts = datetime.now(UTC)

    if not data: abort(503)

    resp = Response(data, mimetype=f"image/{ext}")
    resp.headers['Last-Modified'] = ts.strftime('%a, %d %b %Y %H:%M:%S GMT')
    return resp


@app.route('/set')
def api_set():
    if request.args.get('apikey') not in config['web'].get('api_keys', []): abort(403)
    try:
        mode = picam.sensor_modes[0]['size']
        w = get_constrained_val('width', request.args.get('width', 0), mode[0])
        h = get_constrained_val('height', request.args.get('height', 0), mode[1])
        if w > 0 and h > 0:
            picam.stop()
            picam.configure(picam.create_video_configuration(main={'size': (w, h)}))
            picam.start()
        return "OK"
    except Exception as e: return str(e), 500

@app.route('/config', methods=['GET', 'POST'])
def config_ui():
    u, p = config['web'].get('auth', {}).get('username'), config['web'].get('auth', {}).get('password')
    if u and p:
        auth = request.authorization
        if not auth or auth.username != u or auth.password != p:
            return Response('Auth Required', 401, {'WWW-Authenticate': 'Basic realm="Login"'})

    sensor_size = picam.sensor_modes[0]['size']
    if request.method == 'POST':
        new_ov = {
            'width': get_constrained_val('width', request.form.get('width'), sensor_size[0]),
            'height': get_constrained_val('height', request.form.get('height'), sensor_size[1]),
            'quality': get_constrained_val('quality', request.form.get('quality')),
            'capture_interval': float(request.form.get('capture_interval', 2.0))
        }
        for k, v in picam.camera_controls.items():
            val = request.form.get(f"ctrl_{k}")
            if val:
                try: new_ov[k] = type(getattr(picam.controls, k))(val)
                except: continue

        try:
            with open(config.get('camera_config_path'), 'w') as f: yaml.dump(new_ov, f)
            picam.stop()
            picam.configure(picam.create_video_configuration(main={'size': (new_ov['width'], new_ov['height'])}))
            picam.start()
            for k, v in new_ov.items():
                if k not in ['width', 'height', 'quality', 'capture_interval']:
                    picam.set_controls({k: v})
        except Exception as e: logger.error(f"Save failed: {e}")

    conf = picam.main_configuration()
    html = f"<h2>Config</h2><form method='POST'>"
    html += f"Width (Min {config.get('limits',{}).get('min_width',0)}): <input name='width' value='{conf['main']['size'][0]}'><br>"
    html += f"Height (Min {config.get('limits',{}).get('min_height',0)}): <input name='height' value='{conf['main']['size'][1]}'><br>"
    html += f"Quality: <input name='quality' value='{cam_overrides.get('quality', 90)}'><br>"
    html += f"Capture Interval: <input name='capture_interval' value='{cam_overrides.get('capture_interval', 2.0)}'><br>"
    html += "<hr><h3>Camera Hardware Controls</h3>"
    for k, v in picam.camera_controls.items():
        cur = getattr(picam.controls, k, "")
        html += f"{k}: <input name='ctrl_{k}' value='{cur}'><br>"
    html += "<br><button type='submit'>Apply & Save</button></form>"
    html += "<hr><form action='/reboot' method='POST'><button type='submit'>Reboot Camera Hardware</button></form>"
    return html

@app.route('/reboot', methods=['POST'])
def reboot():
    picam.stop(); time.sleep(1); picam.start(); return redirect('/config')

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config", default="config.yaml")
    parser.add_argument("-p", "--port", type=int); parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose: logger.addHandler(logging.StreamHandler(sys.stderr))
    with open(args.config, 'r') as f: config = yaml.safe_load(f)

    # 1. Initialize Picamera2 BUT DON'T START YET
    picam = Picamera2()

    # 2. Get sensor modes while stopped
    sensor_size = picam.sensor_modes[0]['size']

    # 3. Load overrides
    cam_path = config.get('camera_config_path', 'config-camera.yaml')
    if os.path.exists(cam_path):
        with open(cam_path, 'r') as f: cam_overrides = yaml.safe_load(f) or {}

    # 4. Configure based on overrides (or default)
    w = get_constrained_val('width', cam_overrides.get('width', sensor_size[0]), sensor_size[0])
    h = get_constrained_val('height', cam_overrides.get('height', sensor_size[1]), sensor_size[1])
    picam.configure(picam.create_video_configuration(main={'size': (w, h)}))

    # 5. NOW START
    picam.start()

    # 6. Apply controls (can be done while running)
    for k, v in cam_overrides.items():
        if k not in ['width', 'height', 'quality', 'capture_interval']:
            try: picam.set_controls({k: v})
            except: pass

# Start Background Loop LAST
    sched = BackgroundScheduler()
    sched.add_job(
        background_update,
        'interval',
        seconds=max(0.1, cam_overrides.get('capture_interval', 2.0)),
        misfire_grace_time=1 # Avoid backlog of captures
    )
    sched.start()

    app.run(host='0.0.0.0', port=args.port or config['web']['port'])
