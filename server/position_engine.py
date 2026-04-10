"""
Moteur de positionnement BLETrack.

Deux modes :
  - simulation=True  : génère de fausses positions pour le développement
  - simulation=False : calcule les positions réelles depuis les RSSI MQTT
"""

import asyncio
import json
import math
import random
import time
import logging
import uuid
from pathlib import Path

import aiosqlite
import yaml

from .database import DB_PATH
from .gateway_logger import gw_logger

logger = logging.getLogger(__name__)

# Gateways par défaut (surchargées par config.yaml au runtime)
DEFAULT_GATEWAYS = [
    {"id": "gw1", "name": "Gateway Pi4",   "ip": "192.168.1.10", "floor": 1, "x": 3.0,  "y": 5.0},
    {"id": "gw2", "name": "Gateway Pi500", "ip": "192.168.1.11", "floor": 1, "x": 27.0, "y": 5.0},
]

DEMO_TAGS = [
    {"mac": "AA:BB:CC:DD:EE:01", "name": "Martin Dupont",   "role": "Opérateur",  "badge": "B001"},
    {"mac": "AA:BB:CC:DD:EE:02", "name": "Sophie Leroux",   "role": "Technicien", "badge": "B002"},
    {"mac": "AA:BB:CC:DD:EE:03", "name": "Lucas Bernard",   "role": "Opérateur",  "badge": "B003"},
    {"mac": "AA:BB:CC:DD:EE:04", "name": "Emma Moreau",     "role": "Superviseur","badge": "B004"},
    {"mac": "AA:BB:CC:DD:EE:05", "name": "Thomas Petit",    "role": "Technicien", "badge": "B005"},
]


class KalmanFilter1D:
    """Filtre de Kalman monodimensionnel pour lisser une mesure bruitée."""

    def __init__(self, initial: float, process_noise: float = 0.5, measurement_noise: float = 1.0):
        self.estimate = initial
        self.error = 1.0
        self.Q = process_noise
        self.R = measurement_noise

    def update(self, measurement: float) -> float:
        self.error += self.Q
        K = self.error / (self.error + self.R)
        self.estimate += K * (measurement - self.estimate)
        self.error *= 1 - K
        return self.estimate


class PositionEngine:
    def __init__(self, simulation: bool = True):
        self.simulation = simulation
        self.gateways = {gw["id"]: gw for gw in DEFAULT_GATEWAYS}

        # Config BLE (synchronisée avec la table config)
        self.config = {
            "rssi_1m":    -59.0,
            "path_loss":   2.5,
            "buffer_size": 4,
            "tag_timeout": 30,
        }

        # {tag_mac: {x, y, floor, accuracy, timestamp}}
        self._positions: dict[str, dict] = {}

        # {tag_mac: {gateway_id: [rssi, ...]}}
        self._rssi_buffers: dict[str, dict[str, list[float]]] = {}

        # {tag_mac: {x: KalmanFilter1D, y: KalmanFilter1D}}
        self._kalman: dict[str, dict[str, KalmanFilter1D]] = {}

        # Timestamp dernière sauvegarde position par tag
        self._last_save: dict[str, float] = {}

        # Anti-spam alertes zone : {tag_mac: {zone_id: last_alert_timestamp}}
        self._zone_alert_cooldown: dict[str, dict[str, float]] = {}

        # Dernière réception RSSI par gateway {gateway_id: timestamp}
        self._gateway_last_seen: dict[str, float] = {}

        # Timeout gateway (secondes sans données = offline)
        self.GATEWAY_TIMEOUT = 30

        # Stats par gateway : {gw_id: {"tags_seen": int, "batches": int}}
        self._gateway_stats: dict[str, dict] = {}

        # RSSI live : {gw_id: {tag_mac: {"rssi": float, "ts": float}}}
        self._live_rssi: dict[str, dict[str, dict]] = {}

        # Sessions de calibration en cours : {f"{gw_id}:{tag_mac}": [rssi, ...]}
        self._calibration_sessions: dict[str, list[float]] = {}

        # Simulation : cibles de déplacement
        self._sim_targets: dict[str, dict] = {}

        self._sim_task: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None
        self._mqtt_task: asyncio.Task | None = None

        # Config MQTT depuis config.yaml
        cfg_path = Path(__file__).parent.parent / "config" / "config.yaml"
        try:
            cfg = yaml.safe_load(cfg_path.read_text())
            self._mqtt_cfg = cfg.get("mqtt", {})
        except Exception:
            self._mqtt_cfg = {}

    # ─── PUBLIC ──────────────────────────────────────────────────────────────

    async def start(self):
        """Démarre les tâches de fond."""
        logger.info("Moteur de positionnement : mode réel (MQTT)")
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        if self._mqtt_cfg.get("enabled", False):
            self._mqtt_task = asyncio.create_task(self._mqtt_loop())
            logger.info(f"Abonnement MQTT → {self._mqtt_cfg.get('broker','localhost')}:{self._mqtt_cfg.get('port',1883)}")

    async def stop(self):
        for task in (self._sim_task, self._cleanup_task, self._mqtt_task):
            if task:
                task.cancel()

    def update_config(self, updates: dict):
        """Met à jour les paramètres BLE depuis l'API /config."""
        mapping = {
            "rssi_1m":    float,
            "path_loss":  float,
            "buffer_size": int,
            "tag_timeout": int,
        }
        for key, cast in mapping.items():
            if key in updates and updates[key] is not None:
                self.config[key] = cast(updates[key])
        logger.info(f"Config BLE mise à jour : {self.config}")

    def set_gateways(self, gateways: list[dict]):
        self.gateways = {gw["id"]: gw for gw in gateways}

    async def get_positions(self) -> list[dict]:
        """Retourne toutes les positions connues enrichies avec les infos personnel."""
        now = time.time()
        timeout = self.config["tag_timeout"]

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM personnel") as cur:
                personnel = {r["tag_mac"]: dict(r) for r in await cur.fetchall()}

        result = []
        for mac, person in personnel.items():
            pos = self._positions.get(mac)
            if pos and (now - pos["timestamp"]) < timeout:
                result.append({
                    "tag_mac":   mac,
                    "name":      person["name"],
                    "role":      person.get("role", ""),
                    "x":         round(pos["x"], 2),
                    "y":         round(pos["y"], 2),
                    "floor":     pos.get("floor", 1),
                    "accuracy":  round(pos.get("accuracy", 2.0), 2),
                    "timestamp": pos["timestamp"],
                    "online":    True,
                })
            else:
                result.append({
                    "tag_mac":   mac,
                    "name":      person["name"],
                    "role":      person.get("role", ""),
                    "x":         0.0,
                    "y":         0.0,
                    "floor":     1,
                    "accuracy":  0.0,
                    "timestamp": 0.0,
                    "online":    False,
                })

        return result

    def get_gateways_status(self) -> list[dict]:
        """Retourne le statut de chaque gateway (online/offline + stats + calibration)."""
        now = time.time()
        result = []
        for gw_id, gw in self.gateways.items():
            last_seen = self._gateway_last_seen.get(gw_id, 0)
            online = (now - last_seen) < self.GATEWAY_TIMEOUT if last_seen else False
            stats = self._gateway_stats.get(gw_id, {"tags_seen": 0, "batches": 0})
            result.append({
                "id":        gw_id,
                "name":      gw.get("name", gw_id),
                "ip":        gw.get("ip", ""),
                "floor":     gw.get("floor", 1),
                "x":         gw.get("x", 0.0),
                "y":         gw.get("y", 0.0),
                "online":    online,
                "last_seen": round(last_seen, 1) if last_seen else None,
                "ago":       round(now - last_seen) if last_seen else None,
                "rssi_1m":   gw.get("rssi_1m", self.config["rssi_1m"]),
                "path_loss": gw.get("path_loss", self.config["path_loss"]),
                "tags_seen": stats["tags_seen"],
                "batches":   stats["batches"],
            })
        return result

    def update_gateway(self, gw_id: str, data: dict):
        """Met à jour la config d'une gateway (calibration, nom, IP)."""
        if gw_id not in self.gateways:
            return False
        gw = self.gateways[gw_id]
        for key in ("name", "ip", "rssi_1m", "path_loss", "x", "y", "floor"):
            if key in data and data[key] is not None:
                gw[key] = float(data[key]) if key in ("rssi_1m", "path_loss", "x", "y") else data[key]
        return True

    def add_gateway(self, gw_data: dict) -> bool:
        """Ajoute une nouvelle gateway."""
        gw_id = gw_data.get("id")
        if not gw_id or gw_id in self.gateways:
            return False
        self.gateways[gw_id] = {
            "id":        gw_id,
            "name":      gw_data.get("name", gw_id),
            "ip":        gw_data.get("ip", ""),
            "floor":     gw_data.get("floor", 1),
            "x":         float(gw_data.get("x", 0)),
            "y":         float(gw_data.get("y", 0)),
            "rssi_1m":   float(gw_data.get("rssi_1m", self.config["rssi_1m"])),
            "path_loss": float(gw_data.get("path_loss", self.config["path_loss"])),
        }
        return True

    def delete_gateway(self, gw_id: str) -> bool:
        if gw_id in self.gateways:
            del self.gateways[gw_id]
            return True
        return False

    def get_gateway_live_rssi(self, gw_id: str) -> list[dict]:
        """Retourne le RSSI live de tous les tags visibles par une gateway (dernières 30s)."""
        now = time.time()
        gw = self.gateways.get(gw_id, {})
        rssi_1m   = gw.get("rssi_1m",   self.config["rssi_1m"])
        path_loss = gw.get("path_loss",  self.config["path_loss"])
        gw_data = self._live_rssi.get(gw_id, {})
        result = []
        for mac, entry in gw_data.items():
            if now - entry["ts"] < 30:
                rssi_avg = entry.get("avg", entry["rssi"])
                dist = round(10 ** ((rssi_1m - rssi_avg) / (10 * path_loss)), 2)
                result.append({
                    "tag_mac":   mac,
                    "rssi_last": entry["rssi"],
                    "rssi_avg":  rssi_avg,
                    "samples":   entry.get("samples", 1),
                    "ago":       round(now - entry["ts"]),
                    "distance_m": dist,
                })
        result.sort(key=lambda x: x["rssi_last"], reverse=True)
        return result

    def flush_gateway_buffers(self, gw_id: str):
        """Supprime les données RSSI d'une gateway offline de tous les buffers."""
        for mac_bufs in self._rssi_buffers.values():
            mac_bufs.pop(gw_id, None)
        self._live_rssi.pop(gw_id, None)
        logger.info(f"Buffers RSSI purgés pour gateway offline : {gw_id}")

    def clear_rssi_buffer(self, mac: str = ""):
        """Vide le buffer RSSI — pour un tag spécifique ou tous les tags."""
        if mac:
            self._rssi_buffers.pop(mac, None)
            self._kalman.pop(mac, None)
            self._positions.pop(mac, None)
            for gw_data in self._live_rssi.values():
                gw_data.pop(mac, None)
        else:
            self._rssi_buffers.clear()
            self._kalman.clear()
            self._positions.clear()
            self._live_rssi.clear()

    async def measure_rssi(self, gw_id: str, tag_mac: str, duration: int = 5) -> dict:
        """Collecte les RSSI du tag sur `duration` secondes.
        Retourne la moyenne + suggestions path_loss pour les autres gateways
        (calculé depuis la position connue des gateways)."""
        key = f"{gw_id}:{tag_mac}"
        self._calibration_sessions[key] = []
        await asyncio.sleep(duration)
        samples = self._calibration_sessions.pop(key, [])
        if not samples:
            return {"rssi_avg": None, "samples": 0}

        avg = round(sum(samples) / len(samples), 1)
        result = {"rssi_avg": avg, "samples": len(samples)}

        # Auto-suggestion path_loss pour les autres gateways :
        # Le tag est à ~1m de gw_id, donc à (dist_entre_gw - 1m) des autres gateways.
        gw_cal = self.gateways.get(gw_id)
        suggestions = []
        for other_id, other_gw in self.gateways.items():
            if other_id == gw_id or not gw_cal:
                continue
            entry = self._live_rssi.get(other_id, {}).get(tag_mac)
            if not entry or (time.time() - entry["ts"]) > duration + 5:
                continue
            rssi_other = entry.get("avg", entry["rssi"])
            rssi_1m_other = other_gw.get("rssi_1m", self.config["rssi_1m"])
            d_gw = math.hypot(other_gw["x"] - gw_cal["x"], other_gw["y"] - gw_cal["y"])
            d_tag = max(1.5, d_gw - 1.0)
            pl = (rssi_1m_other - rssi_other) / (10 * math.log10(d_tag))
            pl = round(pl, 2)
            if 1.5 <= pl <= 5.0:
                suggestions.append({
                    "gw_id":               other_id,
                    "rssi_measured":       round(rssi_other, 1),
                    "distance_m":          round(d_tag, 1),
                    "path_loss_suggested": pl,
                })

        if suggestions:
            result["path_loss_suggestions"] = suggestions
        return result

    async def process_rssi_batch(self, gateway_id: str, readings: list[dict]):
        """Traite un batch RSSI reçu depuis un gateway via MQTT."""
        was_offline = self._gateway_last_seen.get(gateway_id, 0)
        self._gateway_last_seen[gateway_id] = time.time()

        # Stats par gateway
        if gateway_id not in self._gateway_stats:
            self._gateway_stats[gateway_id] = {"tags_seen": 0, "batches": 0}
        self._gateway_stats[gateway_id]["tags_seen"] = len({r["tag_mac"] for r in readings})
        self._gateway_stats[gateway_id]["batches"] += 1

        # Log du batch RSSI
        gw_logger.rssi_batch(gateway_id, len({r["tag_mac"] for r in readings}), readings)

        # Détection retour online après absence
        if was_offline and (time.time() - was_offline) > self.GATEWAY_TIMEOUT:
            gw = self.gateways.get(gateway_id, {})
            gw_logger.gateway_online(gateway_id, gw.get("ip", ""))

        for r in readings:
            mac = r["tag_mac"]
            rssi = float(r["rssi"])

            # Mise à jour RSSI live
            if gateway_id not in self._live_rssi:
                self._live_rssi[gateway_id] = {}
            buf_live = self._live_rssi[gateway_id].setdefault(mac, {"rssi": rssi, "ts": 0, "avg": rssi, "samples": 0})
            buf_live["rssi"] = rssi
            buf_live["ts"] = time.time()

            # Alimentation session de calibration si active
            key = f"{gateway_id}:{mac}"
            if key in self._calibration_sessions:
                self._calibration_sessions[key].append(rssi)

            if mac not in self._rssi_buffers:
                self._rssi_buffers[mac] = {}
            buf = self._rssi_buffers[mac].setdefault(gateway_id, [])
            buf.append(rssi)
            if len(buf) > int(self.config["buffer_size"]):
                buf.pop(0)
            buf_live["avg"] = round(sum(buf) / len(buf), 1)
            buf_live["samples"] = len(buf)

        # Recalcule la position pour chaque tag mis à jour
        updated_macs = {r["tag_mac"] for r in readings}
        for mac in updated_macs:
            await self._compute_position(mac)

    # ─── ALGORITHMES ─────────────────────────────────────────────────────────

    def rssi_to_distance(self, rssi: float) -> float:
        """RSSI → distance (mètres) avec modèle log-distance."""
        return 10 ** ((self.config["rssi_1m"] - rssi) / (10 * self.config["path_loss"]))

    def trilaterate(self, anchors: list[tuple]) -> tuple[float, float] | None:
        """
        Trilatération pondérée à partir d'une liste (x, y, distance).
        Poids = 1/d² — la gateway la plus proche influence le plus la position.
        Retourne (x, y) ou None si pas de points.
        """
        if not anchors:
            return None

        if len(anchors) == 1:
            # Une seule gateway : distance connue, direction inconnue.
            # On place le tag à distance d sur l'axe principal du bâtiment (x).
            x, y, d = anchors[0]
            return (x + d, y)

        if len(anchors) == 2:
            x1, y1, r1 = anchors[0]
            x2, y2, r2 = anchors[1]
            d = math.hypot(x2 - x1, y2 - y1)
            if d < 1e-6:
                return (x1, y1)
            a = (r1 ** 2 - r2 ** 2 + d ** 2) / (2 * d)
            h2 = r1 ** 2 - a ** 2
            mx = x1 + a * (x2 - x1) / d
            my = y1 + a * (y2 - y1) / d
            if h2 > 0:
                # Deux intersections — choisir celle côté gateway la plus proche
                h = math.sqrt(h2)
                px = -(y2 - y1) / d * h
                py =  (x2 - x1) / d * h
                # Pondérer par 1/r² : le point biaisé vers la gateway proche
                w1, w2 = 1 / max(r1, 0.1) ** 2, 1 / max(r2, 0.1) ** 2
                wt = w1 + w2
                bx = (w1 * x1 + w2 * x2) / wt
                by = (w1 * y1 + w2 * y2) / wt
                # Choisir le point d'intersection le plus proche du barycentre pondéré
                p1 = (mx + px, my + py)
                p2 = (mx - px, my - py)
                if math.hypot(p1[0] - bx, p1[1] - by) < math.hypot(p2[0] - bx, p2[1] - by):
                    return p1
                return p2
            return (mx, my)

        # 3+ gateways : moindres carrés pondérés
        try:
            import numpy as np
            # Trier par distance croissante pour stabilité
            anchors_s = sorted(anchors, key=lambda a: a[2])
            x0, y0, r0 = anchors_s[0]
            A, b, W = [], [], []
            for xi, yi, ri in anchors_s[1:]:
                A.append([2 * (xi - x0), 2 * (yi - y0)])
                b.append(xi**2 - x0**2 + yi**2 - y0**2 + r0**2 - ri**2)
                W.append(1 / max(ri, 0.1) ** 2)
            Wm = np.diag(W)
            Anp = np.array(A)
            bnp = np.array(b)
            AtW = Anp.T @ Wm
            result, *_ = np.linalg.lstsq(AtW @ Anp, AtW @ bnp, rcond=None)
            return (float(result[0]), float(result[1]))
        except Exception:
            # Fallback : moyenne pondérée par 1/d²
            wt = sum(1 / max(a[2], 0.1) ** 2 for a in anchors)
            x = sum(a[0] / max(a[2], 0.1) ** 2 for a in anchors) / wt
            y = sum(a[1] / max(a[2], 0.1) ** 2 for a in anchors) / wt
            return (x, y)

    def _kalman_smooth(self, mac: str, x: float, y: float) -> tuple[float, float]:
        if mac not in self._kalman:
            self._kalman[mac] = {
                "x": KalmanFilter1D(x),
                "y": KalmanFilter1D(y),
            }
        sx = self._kalman[mac]["x"].update(x)
        sy = self._kalman[mac]["y"].update(y)
        return (sx, sy)

    async def _compute_position(self, mac: str):
        buffers = self._rssi_buffers.get(mac, {})
        if not buffers:
            return

        anchors = []
        for gw_id, rssi_buf in buffers.items():
            gw = self.gateways.get(gw_id)
            if not gw or not rssi_buf:
                continue
            avg_rssi = sum(rssi_buf) / len(rssi_buf)
            # Calibration par gateway (sinon valeur globale)
            rssi_1m   = gw.get("rssi_1m",   self.config["rssi_1m"])
            path_loss  = gw.get("path_loss",  self.config["path_loss"])
            dist = 10 ** ((rssi_1m - avg_rssi) / (10 * path_loss))
            anchors.append((gw["x"], gw["y"], dist))

        pos = self.trilaterate(anchors)
        if pos is None:
            return

        x, y = self._kalman_smooth(mac, pos[0], pos[1])

        # Précision estimée (plus de gateways = meilleure précision)
        accuracy = max(0.5, 3.0 - len(anchors) * 0.5)

        self._positions[mac] = {
            "x": x, "y": y, "floor": 1,
            "accuracy": accuracy,
            "timestamp": time.time(),
        }

        # Log de position (uniquement toutes les 5s pour ne pas saturer)
        if time.time() - self._last_save.get(mac, 0) >= 5:
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute("SELECT name FROM personnel WHERE tag_mac = ?", (mac,)) as cur:
                    row = await cur.fetchone()
                    name = row["name"] if row else mac
            gw_logger.position_computed(mac, name, round(x, 2), round(y, 2), round(accuracy, 2), len(anchors))

        # Détection de zone et génération d'alertes
        asyncio.create_task(self.check_zones(mac, x, y, 1))

        # Sauvegarde périodique (toutes les 5s)
        if time.time() - self._last_save.get(mac, 0) >= 5:
            self._last_save[mac] = time.time()
            asyncio.create_task(self._save_position(mac, x, y, 1, accuracy))

    async def _save_position(self, mac: str, x: float, y: float, floor: int, accuracy: float):
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "INSERT INTO position_history (tag_mac, x, y, floor, accuracy, timestamp) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (mac, x, y, floor, accuracy, time.time()),
                )
                await db.commit()
        except Exception as e:
            logger.warning(f"Erreur sauvegarde position {mac}: {e}")

    # ─── ZONE DETECTION ──────────────────────────────────────────────────────

    async def check_zones(self, mac: str, x: float, y: float, floor: int) -> list[dict]:
        """
        Vérifie si le tag est dans une zone et génère des alertes si nécessaire.
        Retourne la liste des alertes créées.
        """
        try:
            from shapely.geometry import Point, Polygon
        except ImportError:
            return []

        point = Point(x, y)
        new_alerts = []

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM zones WHERE floor = ?", (floor,)
            ) as cur:
                zones = [dict(r) for r in await cur.fetchall()]

            async with db.execute(
                "SELECT name FROM personnel WHERE tag_mac = ?", (mac,)
            ) as cur:
                row = await cur.fetchone()
                person_name = row["name"] if row else mac

            import json
            for zone in zones:
                polygon_coords = json.loads(zone["polygon"])
                if len(polygon_coords) < 3:
                    continue

                poly = Polygon(polygon_coords)
                in_zone = poly.contains(point)

                if in_zone and zone["alert_on_enter"]:
                    # Anti-spam : une alerte par zone toutes les 60s max
                    cooldowns = self._zone_alert_cooldown.setdefault(mac, {})
                    last = cooldowns.get(zone["id"], 0)
                    if time.time() - last < 60:
                        continue
                    cooldowns[zone["id"]] = time.time()

                    alert_id = str(uuid.uuid4())[:8]
                    await db.execute(
                        "INSERT INTO alerts (id, type, tag_mac, person_name, zone_id, zone_name, timestamp, acknowledged) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                        (alert_id, "zone_enter", mac, person_name, zone["id"], zone["name"], time.time()),
                    )
                    new_alerts.append({"id": alert_id, "type": "zone_enter", "zone_name": zone["name"]})
                    gw_logger.alert_triggered(mac, person_name, zone["name"], "zone_enter")

            await db.commit()

        return new_alerts

    # ─── SIMULATION ──────────────────────────────────────────────────────────

    async def _simulation_loop(self):
        """Génère de fausses positions qui se déplacent dans le bâtiment."""
        # Initialisation
        for tag in DEMO_TAGS:
            mac = tag["mac"]
            start_x = random.uniform(2, 28)
            start_y = random.uniform(1, 9)
            self._positions[mac] = {
                "x": start_x, "y": start_y, "floor": 1,
                "accuracy": 1.5, "timestamp": time.time(),
            }
            self._sim_targets[mac] = {
                "x": random.uniform(2, 28),
                "y": random.uniform(1, 9),
            }

        while True:
            try:
                for tag in DEMO_TAGS:
                    mac = tag["mac"]
                    pos = self._positions[mac]
                    target = self._sim_targets[mac]

                    dx = target["x"] - pos["x"]
                    dy = target["y"] - pos["y"]
                    dist = math.hypot(dx, dy)

                    if dist < 0.3:
                        # Nouvelle cible aléatoire
                        self._sim_targets[mac] = {
                            "x": random.uniform(2, 28),
                            "y": random.uniform(1, 9),
                        }
                    else:
                        speed = 0.15  # m/update
                        noise_x = random.gauss(0, 0.03)
                        noise_y = random.gauss(0, 0.03)
                        new_x = pos["x"] + (dx / dist) * speed + noise_x
                        new_y = pos["y"] + (dy / dist) * speed + noise_y
                        # Contraindre dans le bâtiment
                        new_x = max(0.5, min(29.5, new_x))
                        new_y = max(0.5, min(9.5, new_y))

                        # Kalman sur les positions simulées
                        sx, sy = self._kalman_smooth(mac, new_x, new_y)

                        self._positions[mac] = {
                            "x": sx, "y": sy, "floor": 1,
                            "accuracy": 1.5,
                            "timestamp": time.time(),
                        }

            except Exception as e:
                logger.error(f"Erreur simulation: {e}")

            await asyncio.sleep(0.5)

    async def _ensure_demo_personnel(self):
        """Insère le personnel de démonstration si la table est vide."""
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT COUNT(*) FROM personnel") as cur:
                count = (await cur.fetchone())[0]

            if count == 0:
                for tag in DEMO_TAGS:
                    await db.execute(
                        "INSERT OR IGNORE INTO personnel (tag_mac, name, role, badge_id, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (tag["mac"], tag["name"], tag["role"], tag["badge"], time.time()),
                    )
                await db.commit()
                logger.info("Personnel de démonstration inséré")

    async def _mqtt_loop(self):
        """S'abonne au broker MQTT et traite les batches RSSI des gateways."""
        try:
            import aiomqtt
        except ImportError:
            logger.error("aiomqtt non installé — pip install aiomqtt")
            return

        broker = self._mqtt_cfg.get("broker", "localhost")
        port   = self._mqtt_cfg.get("port", 1883)
        topic  = self._mqtt_cfg.get("topic_prefix", "bletrack") + "/rssi"

        while True:
            try:
                async with aiomqtt.Client(broker, port) as client:
                    logger.info(f"MQTT connecté → {broker}:{port} topic={topic}")
                    await client.subscribe(topic)
                    async for message in client.messages:
                        try:
                            data = json.loads(message.payload)
                            await self.process_rssi_batch(
                                data["gateway_id"],
                                data["readings"],
                            )
                        except Exception as e:
                            logger.warning(f"Erreur traitement MQTT: {e}")
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning(f"MQTT déconnecté ({e}) — reconnexion dans 5s")
                await asyncio.sleep(5)

    async def _cleanup_loop(self):
        """Nettoie l'historique des positions toutes les heures."""
        while True:
            await asyncio.sleep(3600)
            try:
                from .database import cleanup_old_data
                await cleanup_old_data()
            except Exception as e:
                logger.warning(f"Erreur nettoyage: {e}")
