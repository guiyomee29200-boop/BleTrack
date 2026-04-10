# PROMPT TECHNIQUE — Projet BLETrack
# Système de localisation indoor BLE
# Version 1.0 — Mars 2026
# ============================================================
#
# Ce prompt contient la spécification complète du projet.
# Copiez-le tel quel dans une nouvelle conversation pour
# relancer le développement à n'importe quelle étape.
#
# ============================================================

Tu es un développeur fullstack senior spécialisé en IoT et systèmes embarqués.
Tu dois développer **BLETrack**, un système de localisation indoor de personnel
par Bluetooth Low Energy (BLE).

---

## CONTEXTE GÉNÉRAL

- **Objectif** : Localiser en temps réel 150 personnes consentantes dans un bâtiment
- **Bâtiment** : 100m de long, 10 étages, couloirs étroits
- **Précision cible** : < 2 mètres
- **Technologie** : Bluetooth Low Energy (beacons H2/H2A Navigation Beacon)
- **Réseau** : 100% Ethernet RJ45 — PAS de WiFi (désactivé sur tous les Pi)
- **Langue de l'interface** : 100% français
- **Style UI** : Industriel / salle de contrôle (sombre, sobre, monospace)

---

## MATÉRIEL

| Composant | Modèle | Quantité | IP / Rôle |
|-----------|--------|----------|-----------|
| Serveur + Gateway BLE | Raspberry Pi 4 | 1 | 192.168.1.10 — Serveur central + scan BLE |
| Gateway BLE | Raspberry Pi 500 | 1 | 192.168.1.11 — Scan BLE secondaire |
| Switch réseau | TP-Link TL-SG108PE | 1 | Interconnexion Ethernet |
| Beacons BLE | H2/H2A | 150 | 1 par personne (porté) |
| Câbles | RJ45 Cat.6 | 3 | 10m + 20m + 1m |
| Alimentation | USB-C 5V/3A | 2 | 1 par Raspberry Pi |

**Câblage** :
- Pi 4 → Port 1 du switch (RJ45) + alim USB-C
- Pi 500 → Port 2 du switch (RJ45) + alim USB-C
- Laptop → Port 5+ du switch (RJ45)
- WiFi désactivé sur les 2 Pi (éviter interférences BLE 2.4 GHz)

**Placement gateways (prototype 30m en couloir étroit)** :
- Pi 4 : fixé au mur à 3m d'une extrémité, hauteur 2.5m
- Pi 500 : fixé au mur à 3m de l'autre extrémité, hauteur 2.5m
- Distance entre les deux : ~24m
- Pas de boîtier métallique fermé (bloque le BLE)

---

## STACK TECHNIQUE

| Couche | Technologie |
|--------|-------------|
| Backend API | Python FastAPI |
| Auth | JWT (python-jose) + bcrypt |
| Frontend | React standalone (PAS de build/npm, un seul fichier HTML) |
| Carte indoor | Leaflet.js avec CRS.Simple |
| Temps réel | WebSocket natif FastAPI |
| Scanner BLE | Python Bleak |
| Messaging | MQTT (Mosquitto) |
| Base de données | SQLite (aiosqlite) |
| Cache positions | Redis (TTL 30s) |
| Positionnement | Trilatération RSSI + filtre de Kalman |
| Détection zones | Shapely (point-in-polygon) |

---

## PAGES DE L'APPLICATION

### Page 1 — Accueil
- Logo "BLETrack"
- 1 seul bouton : "Combat" → redirige vers /login?mode=combat
- Barre d'état haut : indicateurs réseau (eth0, MQTT, gateways) + horloge
- Barre d'état bas : IP des Pi, nombre de beacons détectés, version
- Style : fond sombre, grille technique en arrière-plan, lignes de visée

### Page 2 — Login
- Champs : identifiant + mot de passe
- Bouton : "Se connecter"
- Message d'erreur si échec
- **Chaque tentative est loggée** en base SQLite avec :
  - Horodatage (date + heure)
  - Nom d'utilisateur saisi
  - Résultat (succès / échec)
  - Mode accédé (Combat / Admin)
- Redirection après succès :
  - Rôle Admin → page Administration
  - Rôle Opérateur → page Mode Combat

### Page 3 — Administration (rôle Admin uniquement)
- **5 onglets** dans une sidebar de navigation à gauche :

  **Onglet 1 — Utilisateurs** :
  - Tableau : identifiant, nom, rôle (badge Admin/Opérateur), dernière connexion
  - Boutons : Modifier, Supprimer
  - Formulaire dépliable : créer un compte (identifiant, mdp, nom, rôle)

  **Onglet 2 — Personnel / Beacons** :
  - Tableau : nom, adresse MAC beacon, rôle/fonction, n° badge, statut online/offline
  - Boutons : Modifier, Supprimer
  - Formulaire : enregistrer un beacon (MAC, nom, rôle, badge)

  **Onglet 3 — Zones d'alerte** :
  - Tableau : nom, étage, type (normale/restreinte), alerte entrée oui/non, sortie oui/non, nb personnes
  - Formulaire : créer une zone (nom, étage, polygone en coordonnées x,y, config alertes)

  **Onglet 4 — Logs de connexion** :
  - Tableau filtrable : horodatage, utilisateur, résultat (succès/échec), mode accédé
  - Barre de recherche par utilisateur

  **Onglet 5 — Calibration BLE** :
  - 4 sliders avec affichage valeur en direct :
    - RSSI @ 1m (défaut -59 dBm, plage -80 à -40)
    - Path loss exponent (défaut 2.5, plage 2.0 à 3.5)
    - Buffer RSSI (défaut 8, plage 3 à 20)
    - Timeout tag (défaut 30s, plage 5 à 120s)
  - Bouton "Sauvegarder la calibration"
  - Cartes statut gateways : IP, rôle, position, étage, tags détectés, statut

- **Header** : logo, badge "Administration", bouton "Combat" (rouge), bouton "Déconnexion"
- **Footer** : indicateurs réseau, utilisateur connecté, temps de session

### Page 4 — Mode Combat (Admin + Opérateur)
- **Layout 3 colonnes** :

  **Colonne gauche (300px) — Personnel** :
  - Titre : "Personnel — Étage N"
  - Barre de recherche (filtre par nom, rôle, MAC)
  - Liste scrollable : pip statut (vert/gris), nom, rôle, MAC, zone actuelle, coordonnées
  - Clic sur une personne = sélection (highlight sur la carte + centrage)
  - Tri : online d'abord, puis alphabétique

  **Colonne centrale — Carte 2D** :
  - SVG interactif ou Leaflet.js (CRS.Simple)
  - Plan de l'étage : contour bâtiment, grille métrique, murs internes
  - Zones d'alerte : polygones colorés (violet = normale, rouge = restreinte) avec labels
  - Gateways : marqueur + label + rayon de couverture BLE
  - Personnel : points colorés (1 couleur par personne), pulsation animée, initiales ou nom
  - Sélecteur d'étage 1-10 (sidebar gauche de la carte)
  - Zoom +/- (sidebar droite de la carte)
  - Infos en bas : étage courant, dimensions, nb personnes visibles
  - **Mise à jour temps réel via WebSocket**

  **Colonne droite (340px) — Alertes** :
  - Liste scrollable des alertes récentes
  - Types : "→ Entrée zone" (amber) / "← Sortie zone" (rouge)
  - Contenu : nom personne, nom zone, horodatage
  - Bouton "Acquitter" sur chaque alerte non acquittée
  - Alertes acquittées : opacité réduite

- **Header** : logo, badge "Combat" (rouge pulsant), stats (en ligne / hors ligne / alertes), horloge, déconnexion
- **Footer** : indicateurs réseau + gateways

---

## AUTHENTIFICATION ET SESSIONS

| Paramètre | Valeur |
|-----------|--------|
| Mécanisme | JWT (JSON Web Token) |
| Hash mots de passe | bcrypt |
| Expiration session | 30 minutes d'inactivité → déconnexion auto, retour au login |
| Compte initial | admin / admin — **changement obligatoire au premier lancement** |
| Rôles | Admin (tout) / Opérateur (Combat uniquement) |

---

## BASE DE DONNÉES SQLite

### Table `users`
- id (INTEGER PK)
- username (TEXT UNIQUE)
- password_hash (TEXT) — bcrypt
- full_name (TEXT)
- role (TEXT) — "admin" ou "operator"
- created_at (REAL)
- must_change_password (INTEGER) — 1 pour le compte admin initial

### Table `personnel`
- tag_mac (TEXT PK) — adresse MAC du beacon
- name (TEXT)
- role (TEXT) — fonction/métier
- badge_id (TEXT)
- created_at (REAL)

### Table `zones`
- id (TEXT PK)
- name (TEXT)
- floor (INTEGER)
- polygon (TEXT) — JSON [[x,y], ...]
- alert_on_enter (INTEGER)
- alert_on_exit (INTEGER)

### Table `connection_logs`
- id (INTEGER PK AUTOINCREMENT)
- timestamp (REAL)
- username (TEXT)
- success (INTEGER) — 0 ou 1
- mode (TEXT) — "combat" ou "admin"

### Table `position_history`
- id (INTEGER PK AUTOINCREMENT)
- tag_mac (TEXT)
- x (REAL)
- y (REAL)
- floor (INTEGER)
- accuracy (REAL)
- timestamp (REAL)

### Table `alerts`
- id (TEXT PK)
- type (TEXT) — "zone_enter" ou "zone_exit"
- tag_mac (TEXT)
- person_name (TEXT)
- zone_id (TEXT)
- zone_name (TEXT)
- timestamp (REAL)
- acknowledged (INTEGER)

---

## API REST

| Endpoint | Méthode | Auth | Description |
|----------|---------|------|-------------|
| POST /api/auth/login | POST | Non | Login → JWT token |
| GET /api/positions | GET | JWT | Positions courantes |
| GET/POST/DELETE /api/personnel | * | JWT Admin | CRUD personnel |
| GET/POST/DELETE /api/zones | * | JWT Admin | CRUD zones |
| GET /api/alerts | GET | JWT | Alertes récentes |
| POST /api/alerts/{id}/ack | POST | JWT | Acquitter alerte |
| GET/POST/DELETE /api/users | * | JWT Admin | CRUD comptes |
| GET /api/logs | GET | JWT Admin | Logs connexion |
| GET/PUT /api/config | * | JWT Admin | Config BLE |
| WS /ws/live | WebSocket | JWT | Temps réel (positions + alertes) |

---

## MOTEUR DE POSITIONNEMENT

1. Chaque gateway scanne les beacons BLE en continu
2. Les RSSI sont envoyés par batch (500ms) en MQTT vers le serveur
3. Le serveur moyenne les RSSI (buffer glissant pondéré)
4. Conversion RSSI → distance : `d = 10^((RSSI_1m - RSSI) / (10 * path_loss))`
5. Trilatération (2+ gateways) pour calculer (x, y)
6. Filtre de Kalman 1D sur X et Y pour lisser
7. Détection zones avec Shapely (point-in-polygon)
8. Publication position + alertes via MQTT → WebSocket

---

## STRUCTURE DU PROJET

```
ble-tracking/
├── config/
│   └── config.yaml              # Config YAML centralisée
├── gateway/
│   └── ble_scanner.py           # Scanner BLE (tourne sur chaque Pi)
├── server/
│   ├── __init__.py
│   ├── main.py                  # FastAPI (API + WebSocket + MQTT)
│   ├── auth.py                  # JWT + bcrypt + login/logout
│   ├── position_engine.py       # Trilatération + Kalman + zones
│   ├── database.py              # SQLite (toutes les tables)
│   └── models.py                # Pydantic models
├── dashboard/
│   └── index.html               # Application React complète (standalone)
├── requirements.txt             # Dépendances serveur
├── requirements-gateway.txt     # Dépendances gateway
├── start_server.sh              # Script de lancement
└── README.md
```

---

## STYLE UI — Industriel / Salle de contrôle

### Polices
- **Titres / logo** : Chakra Petch (bold, angulaire, industriel)
- **Données / monospace** : IBM Plex Mono (lisible, technique)

### Couleurs
```css
--bg-deep: #060a0f;           /* Fond principal */
--bg-panel: #0b1018;          /* Panneaux latéraux */
--bg-surface: #101822;        /* Cartes et surfaces */
--bg-elevated: #162030;       /* Éléments surélevés */
--border-dim: rgba(45, 65, 95, 0.4);
--border-active: rgba(60, 140, 200, 0.3);
--text-primary: #c8d6e5;
--text-dim: #4a6080;
--accent: #2d8ac4;            /* Bleu principal */
--accent-bright: #3da5e8;
--success: #28a068;           /* Vert (online, OK) */
--danger: #c44030;            /* Rouge (alertes, erreurs) */
--amber: #c49030;             /* Orange (warnings) */
--purple: #8870c4;            /* Violet (zones, badges) */
```

### Éléments de design
- Grille technique en arrière-plan (lignes subtiles)
- Coins renforcés sur les boutons importants (bordures L aux 4 coins)
- Effet scanline lumineux au hover sur le bouton Combat
- Points du personnel avec pulsation animée
- Badges de statut avec glow (online = vert lumineux)
- Barres d'état haut et bas avec indicateurs temps réel

---

## ORDRE DE DÉVELOPPEMENT

1. **Backend auth** : JWT + bcrypt + table users + logs connexion + rôles
2. **Frontend routing** : Page accueil + login + redirection par rôle
3. **Mode Combat** : Carte 2D + WebSocket + personnel + alertes
4. **Page Admin** : 5 onglets complets
5. **Gateway BLE** : Scanner Bleak + MQTT + batch
6. **Position Engine** : Trilatération + Kalman + zones
7. **Intégration** : Tests sur les Pi, calibration, déploiement

---

## CONTRAINTES IMPORTANTES

- **PAS de WiFi** : tout en Ethernet RJ45
- **PAS de build frontend** : React chargé via CDN, un seul fichier HTML
- **PAS de Docker** : installation directe sur Raspbian
- **SQLite uniquement** : pas de PostgreSQL
- **100% français** : toute l'interface, les messages d'erreur, les labels
- **Session 30 min** : déconnexion automatique après inactivité
- **Mot de passe admin initial** : admin/admin, changement forcé au 1er login
- **Historique positions** : sauvegarde toutes les 5s par tag (pas à chaque mesure)
- **Nettoyage auto** : suppression historique > 30 jours
- **Batch MQTT** : envoi toutes les 500ms (pas à chaque scan BLE)
