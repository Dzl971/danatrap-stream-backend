@echo off
echo Fermeture de l'ancien serveur...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8000 ^| findstr LISTENING') do taskkill /F /PID %%a >nul 2>&1
timeout /t 2 /nobreak >nul
echo Demarrage du serveur...
cd /d "C:\Users\prodd\Documents\DanaTrapStream\backend"
"C:\Python314\python.exe" -m uvicorn serveur:app --reload --host 127.0.0.1 --port 8000
pause
