@echo off
REM BLETrack — Lancement du serveur (Windows dev)

cd /d "%~dp0"

REM Vérifier que l'environnement virtuel existe
if not exist "venv\Scripts\activate.bat" (
    echo Création de l'environnement virtuel...
    python -m venv venv
    call venv\Scripts\activate.bat
    echo Installation des dépendances...
    pip install -r requirements.txt
) else (
    call venv\Scripts\activate.bat
)

echo.
echo  BLETrack démarré sur http://127.0.0.1:8000
echo  Documentation API : http://127.0.0.1:8000/docs
echo  Ctrl+C pour arrêter
echo.

python -m uvicorn server.main:app --host 127.0.0.1 --port 8000 --reload
