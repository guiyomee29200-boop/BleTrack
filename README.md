# BLETrack — Système de localisation indoor BLE

Système de localisation en temps réel de personnel équipé de tags BLE (Bluetooth Low Energy), basé sur la trilatération RSSI multi-gateway.

---

## Fonctionnalités

- Localisation temps réel sur plan de bâtiment (multi-étages)
- Alertes automatiques si un tag BLE se déconnecte
- Alertes si une gateway perd la connexion
- Interface combat (opérateur) et admin (configuration)
- Page d'alertes publique (sans authentification)
- Calibration RSSI guidée par gateway
- Réseau RJ45 isolé pour le trafic MQTT entre gateways

---

## Architecture

```
[Tag BLE] --BLE--> [Gateway Pi 500 / gw2]
                          |
                        MQTT (RJ45 10.0.1.x)
                          |
[Tag BLE] --BLE--> [Gateway Pi 4 / gw1 + Serveur FastAPI + Broker MQTT]
                          |
                        WiFi
                          |
                   [Navigateur admin/combat]
```

---

## Stack technique

| Composant | Technologie |
|-----------|-------------|
| Serveur API | FastAPI (Python) + WebSocket |
| Base de données | SQLite (aiosqlite) |
| Broker MQTT | Mosquitto |
| Algorithme | Trilatération RSSI + Kalman filter |
| Frontend | HTML/CSS/JS vanilla |
| Auth | JWT (bcrypt) |

---

## Structure du projet

```
BleTrack/
├── server/             # Backend FastAPI
│   ├── main.py         # Routes REST + WebSocket
│   ├── position_engine.py  # Trilatération + Kalman
│   ├── auth.py         # JWT
│   ├── database.py     # Init SQLite
│   └── models.py       # Schemas Pydantic
├── Projet/             # Frontend HTML
│   ├── combat.html     # Interface opérateur
│   ├── admin.html      # Interface configuration
│   ├── index.html      # Page d'accueil
│   └── alerte.html     # Page alertes (sans login)
├── config/
│   └── config.yaml     # Configuration principale
├── deploy/             # Fichiers système des Pi
│   ├── pi-maitre/
│   └── pi-500/
└── docs/               # Documentation
    ├── guide-reglage-gateways.md
    └── plan-adressage-ip.md
```

---

## Démarrage rapide

### Prérequis
- Raspberry Pi 4 (maître) + Raspberry Pi 500 (gateway)
- Python 3.11+
- Mosquitto MQTT broker

### Installation

```bash
git clone <repo>
cd BleTrack
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Lancement

```bash
uvicorn server.main:app --host 0.0.0.0 --port 8000
```

Accéder à `http://<ip-pi>:8000`

---

## Réseau

| Appareil  | WiFi (DHCP)    | RJ45 (statique) | Rôle            |
|-----------|----------------|-----------------|-----------------|
| Pi maître | DHCP | 10.0.1.10/24    | Serveur + MQTT  |
| Pi 500    | DHCP | 10.0.1.20/24    | Gateway BLE gw2 |

Voir [docs/plan-adressage-ip.md](docs/plan-adressage-ip.md) pour les détails réseau.
Voir [deploy/README.md](deploy/README.md) pour le déploiement sur les Pi.

---

## Calibration

Voir [docs/guide-reglage-gateways.md](docs/guide-reglage-gateways.md) pour la procédure complète de calibration RSSI.

---

## Sécurité

- Le secret JWT (`config.yaml`) doit être changé en production
- La base de données `bletrack.db` n'est pas versionnée (voir `.gitignore`)
- L'accès admin est protégé par authentification JWT
