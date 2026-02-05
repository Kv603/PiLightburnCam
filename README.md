# PiLightburnCam
## Simple picamera2 web server for Lightburn 2.1

This is a minimalist implementation of a HTTP webserver to serve up /snapshot.jpg to Lightburn for the new HTTP camera support in [Lightburn RC 2.1.00](https://lightburnsoftware.com/blogs/news/lightburn-2-1-00-release-candidate)

Prerequisites:

 * A supported pi camera
 * Modules as listed in requirements.txt
 * Add the user this will run under to the group "video" (or use the provided .service file)
 *    sudo groupmod -a -U $USER video
       
You can get the required modules using  `pip install flask pyyaml apscheduler picamera2 Pillow`
On Raspian, easiest to get the packaged version of the required modules via:
```
sudo apt-get update
sudo apt install python3-flask python3-picamera2 python3-yaml python3-apscheduler --fix-missing
```
