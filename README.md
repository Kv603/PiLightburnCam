# PiLightburnCam
Simple picamera2 web server for Lightburn 2.1

This is a minimalist implementation of a HTTP webserver to serve up /snapshot.jpg to Lightburn for the new HTTP camera support in LB2.1

Prerequisites:

  A supported pi camera
  pip install flask pyyaml apscheduler picamera2 Pillow
  Add the user this will under to the group "video"
     sudo groupmod -a -U $USER video
       
