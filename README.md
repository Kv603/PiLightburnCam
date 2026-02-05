# PiLightburnCam
## Simple picamera2 web server for Lightburn 2.1

This is a minimalist implementation of a HTTP webserver to serve up /snapshot.jpg to Lightburn for the new HTTP camera support in [Lightburn RC 2.1.00](https://lightburnsoftware.com/blogs/news/lightburn-2-1-00-release-candidate)

Prerequisites:

 * A supported pi camera
 * Modules as listed in requirements.txt:
 *     flask pyyaml apscheduler picamera2 Pillow
 * Add the user this will run under to the group "video" (or use the provided .service file)
 *     sudo groupmod -a -U $USER video
       
Fetch all required modules using  ``
Raspian quick stat:
```
sudo apt-get update -y
sudo apt-get upgrade -y
sudo apt install git python3-flask python3-picamera2 python3-yaml python3-apscheduler --fix-missing
sudo useradd -m -G video picam
sudo su - picam
mkdir var etc bin
git clone https://github.com/Kv603/PiLightburnCam
cp -r PiLightburnCam/etc/ PiLightburnCam/var/ $HOME
cp PiLightburnCam/src/camera_service.py bin/ 

# Test the service by running it in the foreground:
cd $HOME/var
python3 $HOME/bin/camera_service.py -c $HOME/etc/config.yaml
```

### Notes on picamera2

[Picamera2](https://github.com/raspberrypi/picamera2) is only supported on Raspberry Pi OS Bullseye (or later) images, both 32 and 64-bit. As of September 2022, Picamera2 is pre-installed on Raspberry Pi OS images, but not on Raspberry Pi OS Lite images. It works on all Raspberry Pi boards right down to the Pi Zero, although performance in some areas may be worse on less powerful devices.
