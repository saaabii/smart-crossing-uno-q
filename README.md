# BeSafe – Smart Crossing 🚦

A system for safer pedestrian crossings built on **Arduino UNO Q**, with computer vision and AI running **locally on the board**.

Project developed for the **Smart Crossing** challenge (hackathon, October 2026).

## 🎥 Watch the video
See BeSafe in action on a real crossing: **[Watch the demo video](https://www.youtube.com/watch?v=EA7CRsFscs4)**

[![BeSafe demo video](https://img.youtube.com/vi/EA7CRsFscs4/hqdefault.jpg)](https://www.youtube.com/watch?v=EA7CRsFscs4)

## The problem
Many accidents at pedestrian crossings happen because pedestrians, cyclists and vehicles don't notice each other in time.

## Our solution
BeSafe watches the crossing through a camera and:
- **detects** pedestrians, cyclists and vehicles
- **estimates speeds and trajectories** to predict a potential conflict *before* it happens
- **signals danger** in real time with light and sound alerts
- **counts crossings** by direction

The system was tested on a real crossing in Turin.

## Web dashboard
The app includes a real-time web interface with:
- live video with detections
- danger status
- adjustable thresholds
- crossing counters

## Project structure
```
smart-crossing/
├── app.yaml            # Arduino App Lab app configuration
├── sketch/
│   └── sketch.ino      # microcontroller code (sensors, LEDs, buzzer, display)
├── python/
│   ├── main.py         # main logic: detection and danger rules
│   ├── frame_data.py   # video frame data handling
│   └── config.json     # parameters and thresholds
└── assets/
    ├── index.html      # dashboard
    ├── segnale.html    # signal page
    ├── app.js
    └── style.css
```

## How to run it
1. Connect the Arduino UNO Q and the camera
2. Open the project in **Arduino App Lab**
3. Press **Run**
4. Open the dashboard from the link shown by App Lab

## Technologies
- Arduino UNO Q (Linux + microcontroller)
- Arduino App Lab
- Python, C++ (Arduino)
- HTML, CSS, JavaScript, Socket.IO

## Team
- Sofia Grumelli
- Sabina Frigerio
- Valerio Galasso
