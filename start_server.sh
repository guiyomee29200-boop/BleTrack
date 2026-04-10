#!/bin/bash
# BLETrack — Lancement du serveur (Raspberry Pi / Linux)

set -e
cd "$(dirname "$0")"

# Créer l'environnement virtuel si absent
if [ ! -d "venv" ]; then
    echo "Création de l'environnement virtuel..."
    python3 -m venv venv
    source venv/bin/activate
    echo "Installation des dépendances..."
    pip install -r requirements.txt
else
    source venv/bin/activate
fi

echo ""
echo " BLETrack démarré sur http://192.168.1.10:8000"
echo " Ctrl+C pour arrêter"
echo ""

python -m uvicorn server.main:app --host 0.0.0.0 --port 8000
