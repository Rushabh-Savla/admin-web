@echo off
setlocal
cd /d "%~dp0\.."
echo Starting Wi-Fi / LAN Bridge for NAS Annotation Platform...
python "%~dp0wifi_lan_bridge.py"
pause
