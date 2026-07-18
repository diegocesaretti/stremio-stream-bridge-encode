@echo off
cd /d "%~dp0"
echo Add-on disponible en http://IP_DE_ESTA_PC:7000/manifest.json
echo Para cerrar, presiona Ctrl+C.
py -m http.server 7000 --bind 0.0.0.0
pause
