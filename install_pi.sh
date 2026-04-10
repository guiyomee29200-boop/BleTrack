#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# BLETrack — Script d'installation automatique pour Raspberry Pi
# Usage :
#   Serveur principal (Pi 4)  : sudo bash install_pi.sh server
#   Gateway secondaire (Pi 500): sudo bash install_pi.sh gateway
# ─────────────────────────────────────────────────────────────────────────────
set -e

ROLE=${1:-server}
APP_DIR="/opt/bletrack"
USER_RUN="pi"

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║       BLETrack — Installation Pi         ║"
echo "║  Rôle : $ROLE"
echo "╚══════════════════════════════════════════╝"
echo ""

# ─── 1. Mise à jour système ───────────────────────────────────────────────────
echo "[1/7] Mise à jour des paquets..."
apt-get update -qq
apt-get install -y -qq \
    python3 python3-venv python3-pip \
    git curl mosquitto mosquitto-clients \
    bluetooth bluez libbluetooth-dev \
    libglib2.0-dev

# ─── 2. Copie des fichiers ────────────────────────────────────────────────────
echo "[2/7] Création du répertoire $APP_DIR..."
mkdir -p "$APP_DIR"
cp -r . "$APP_DIR/"
chown -R "$USER_RUN":"$USER_RUN" "$APP_DIR"

# ─── 3. Environnement Python ─────────────────────────────────────────────────
echo "[3/7] Création du venv Python..."
cd "$APP_DIR"
sudo -u "$USER_RUN" python3 -m venv venv

if [ "$ROLE" = "server" ]; then
    echo "[3/7] Installation des dépendances serveur..."
    sudo -u "$USER_RUN" venv/bin/pip install -q --upgrade pip
    sudo -u "$USER_RUN" venv/bin/pip install -q -r requirements.txt
else
    echo "[3/7] Installation des dépendances gateway..."
    sudo -u "$USER_RUN" venv/bin/pip install -q --upgrade pip
    sudo -u "$USER_RUN" venv/bin/pip install -q -r requirements-gateway.txt
fi

# ─── 4. Dossiers nécessaires ─────────────────────────────────────────────────
echo "[4/7] Création des dossiers..."
mkdir -p "$APP_DIR/uploads"
mkdir -p "$APP_DIR/logs"
chown -R "$USER_RUN":"$USER_RUN" "$APP_DIR/uploads" "$APP_DIR/logs"

# ─── 5. Configuration Mosquitto ──────────────────────────────────────────────
if [ "$ROLE" = "server" ]; then
    echo "[5/7] Configuration Mosquitto (broker MQTT)..."
    cat > /etc/mosquitto/conf.d/bletrack.conf << 'EOF'
listener 1883
allow_anonymous true
log_dest file /var/log/mosquitto/mosquitto.log
EOF
    systemctl enable mosquitto
    systemctl restart mosquitto
    echo "      Mosquitto démarré sur le port 1883"
else
    echo "[5/7] Mosquitto non nécessaire sur gateway — ignoré"
fi

# ─── 6. Services systemd ─────────────────────────────────────────────────────
echo "[6/7] Création des services systemd..."

if [ "$ROLE" = "server" ]; then
    # Service principal — FastAPI
    cat > /etc/systemd/system/bletrack-server.service << EOF
[Unit]
Description=BLETrack Serveur FastAPI
After=network.target mosquitto.service
Wants=mosquitto.service

[Service]
Type=simple
User=$USER_RUN
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python -m uvicorn server.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5
StandardOutput=append:$APP_DIR/logs/server.log
StandardError=append:$APP_DIR/logs/server.log

[Install]
WantedBy=multi-user.target
EOF

    # Service gateway gw1 (Pi 4 scanne aussi le BLE)
    cat > /etc/systemd/system/bletrack-gateway.service << EOF
[Unit]
Description=BLETrack Gateway BLE (gw1)
After=network.target bletrack-server.service bluetooth.service
Wants=bluetooth.service

[Service]
Type=simple
User=root
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python gateway/ble_scanner.py --gateway gw1 --config $APP_DIR/config/config.yaml
Restart=always
RestartSec=10
StandardOutput=append:$APP_DIR/logs/gateway-gw1.log
StandardError=append:$APP_DIR/logs/gateway-gw1.log

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable bletrack-server bletrack-gateway
    systemctl start bletrack-server bletrack-gateway
    echo "      Services bletrack-server et bletrack-gateway démarrés"

else
    # Gateway secondaire — seulement le scanner BLE
    cat > /etc/systemd/system/bletrack-gateway.service << EOF
[Unit]
Description=BLETrack Gateway BLE (gw2)
After=network.target bluetooth.service
Wants=bluetooth.service

[Service]
Type=simple
User=root
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python gateway/ble_scanner.py --gateway gw2 --config $APP_DIR/config/config.yaml
Restart=always
RestartSec=10
StandardOutput=append:$APP_DIR/logs/gateway-gw2.log
StandardError=append:$APP_DIR/logs/gateway-gw2.log

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable bletrack-gateway
    systemctl start bletrack-gateway
    echo "      Service bletrack-gateway (gw2) démarré"
fi

# ─── 7. Bluetooth ─────────────────────────────────────────────────────────────
echo "[7/7] Activation Bluetooth..."
systemctl enable bluetooth
systemctl start bluetooth
hciconfig hci0 up 2>/dev/null || echo "      (adapter BLE activé au démarrage)"

# ─── Résumé ──────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════╗"
echo "║         Installation terminée !          ║"
echo "╚══════════════════════════════════════════╝"
echo ""

if [ "$ROLE" = "server" ]; then
    IP=$(hostname -I | awk '{print $1}')
    echo "  Interface web : http://$IP:8000"
    echo "  Logs serveur  : tail -f $APP_DIR/logs/server.log"
    echo "  Logs gateway  : tail -f $APP_DIR/logs/gateway-gw1.log"
    echo ""
    echo "  Commandes utiles :"
    echo "    systemctl status bletrack-server"
    echo "    systemctl status bletrack-gateway"
    echo "    systemctl restart bletrack-server"
else
    echo "  Logs gateway  : tail -f $APP_DIR/logs/gateway-gw2.log"
    echo ""
    echo "  Commandes utiles :"
    echo "    systemctl status bletrack-gateway"
    echo "    systemctl restart bletrack-gateway"
fi
echo ""
