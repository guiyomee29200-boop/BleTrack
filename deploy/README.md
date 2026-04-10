# Deploy — Fichiers système BLETrack

Ce dossier contient les fichiers de configuration système à déployer sur chaque Raspberry Pi.

---

## Structure

```
deploy/
├── pi-maitre/          # Pi 4 — serveur principal (IP RJ45 : 10.0.1.10)
│   ├── bletrack-server.service     → /etc/systemd/system/
│   ├── mosquitto-bletrack.conf     → /etc/mosquitto/conf.d/
│   └── eth0-static.txt             → référence config NetworkManager eth0
│
└── pi-500/             # Pi 500 — gateway secondaire (IP RJ45 : 10.0.1.20)
    ├── bletrack-gateway.service    → /etc/systemd/system/
    ├── config.yaml                 → /opt/bletrack/config/
    └── eth0-static.txt             → référence config NetworkManager eth0
```

---

## Déploiement Pi maître

```bash
sudo cp bletrack-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable bletrack-server
sudo systemctl start bletrack-server

sudo cp mosquitto-bletrack.conf /etc/mosquitto/conf.d/
sudo systemctl restart mosquitto

# IP statique RJ45
sudo nmcli connection modify 'netplan-eth0' \
  ipv4.method manual ipv4.addresses '10.0.1.10/24' ipv4.gateway ''
sudo nmcli connection up 'netplan-eth0'
```

## Déploiement Pi 500

```bash
sudo cp bletrack-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable bletrack-gateway
sudo systemctl start bletrack-gateway

# IP statique RJ45
sudo nmcli connection modify 'netplan-eth0' \
  ipv4.method manual ipv4.addresses '10.0.1.20/24' ipv4.gateway ''
sudo nmcli connection up 'netplan-eth0'
```

---

## Réseau

| Appareil   | WiFi (DHCP)      | RJ45 (statique) | Rôle              |
|------------|------------------|-----------------|-------------------|
| Pi maître  | DHCP TouchePas   | 10.0.1.10/24    | Serveur + MQTT    |
| Pi 500     | DHCP TouchePas   | 10.0.1.20/24    | Gateway BLE gw2   |
