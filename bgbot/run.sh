#!/bin/bash
echo "BG-BOT v5 — Full-Stack + Charts + WebSocket"
pip install flask flask-socketio pandas numpy requests -q 2>/dev/null
pip3 install flask flask-socketio pandas numpy requests -q 2>/dev/null
mkdir -p templates
IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
echo "Desktop: http://localhost:5000"
echo "Android: http://${IP}:5000 → ⋮ → Desktop site"
python app.py 2>/dev/null || python3 app.py
