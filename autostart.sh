#!/bin/bash
DISPLAY=:0 xrandr --output DSI-1 --rotate left & firefox --kiosk http://127.0.0.1:5000 & screen -dmS "MediSpender" bash -c "cd /home/roman/Desktop/medikamenten-spender && python3 /home/roman/Desktop/medikamenten-spender/main.py; exec bash"
