@echo off
echo.
echo  ==========================================
echo   BG-BOT v5 — Full-Stack + Charts + WS
echo  ==========================================
echo.
python --version >nul 2>&1
if errorlevel 1 (echo Python not found! && pause && exit /b)
pip install flask flask-socketio pandas numpy requests -q
if not exist "templates" mkdir templates
echo.
echo  Server: http://localhost:5000
echo  Android: http://YOUR_IP:5000 + Desktop Mode
echo.
python app.py
pause
