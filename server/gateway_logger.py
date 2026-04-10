"""
Système de logs des gateways BLETrack.

Enregistre tous les événements des Raspberry Pi (RSSI reçu, statut,
positions calculées, alertes) dans :
  - Un ring buffer mémoire (1000 derniers événements) — accès rapide API
  - Un fichier rotatif sur disque — persistance après redémarrage
"""

import json
import logging
import logging.handlers
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional

# ─── Types d'événements ───────────────────────────────────────────────────────
EVENT_RSSI_BATCH    = "rssi_batch"      # Données RSSI reçues d'un gateway
EVENT_GW_ONLINE     = "gw_online"       # Gateway vient de passer online
EVENT_GW_OFFLINE    = "gw_offline"      # Gateway timeout — offline
EVENT_POSITION      = "position"        # Position calculée pour un tag
EVENT_ALERT         = "alert"           # Alerte zone déclenchée
EVENT_MQTT_CONNECT  = "mqtt_connect"    # Connexion MQTT établie
EVENT_MQTT_DISCONNECT = "mqtt_disconnect"  # Déconnexion MQTT
EVENT_SERVER_START  = "server_start"    # Démarrage du serveur
EVENT_CONFIG_CHANGE = "config_change"   # Modification de config

# Couleur par type (utilisée dans l'UI)
EVENT_COLORS = {
    EVENT_RSSI_BATCH:      "accent",
    EVENT_GW_ONLINE:       "success",
    EVENT_GW_OFFLINE:      "danger",
    EVENT_POSITION:        "dim",
    EVENT_ALERT:           "warning",
    EVENT_MQTT_CONNECT:    "success",
    EVENT_MQTT_DISCONNECT: "danger",
    EVENT_SERVER_START:    "info",
    EVENT_CONFIG_CHANGE:   "warning",
}

logger = logging.getLogger(__name__)


class GatewayLogger:
    """
    Logger centralisé pour tous les événements des gateways.
    Thread-safe via asyncio (pas de threads concurrents).
    """

    RING_SIZE = 1000   # Nombre max d'événements en mémoire

    def __init__(self, log_dir: Optional[Path] = None):
        self._ring: deque[dict] = deque(maxlen=self.RING_SIZE)
        self._log_dir = log_dir
        self._log_file = None      # Fichier JSONL (machine)
        self._text_handler = None  # Handler texte lisible (tail -f)
        self._seq = 0

        # Logger dédié aux événements gateway (séparé du logger uvicorn)
        self._file_logger = logging.getLogger("bletrack.gateway")
        self._file_logger.setLevel(logging.DEBUG)
        self._file_logger.propagate = False  # Ne pas remonter au root logger

        if log_dir:
            self._setup_log_dir(log_dir)

    def _setup_log_dir(self, log_dir: Path):
        log_dir.mkdir(parents=True, exist_ok=True)

        # Fichier JSONL machine (pour l'API)
        self._log_file = log_dir / "gateway_events.jsonl"

        # Fichier texte lisible — rotation 5 Mo, 5 fichiers conservés
        text_log_path = log_dir / "gateway.log"
        self._text_handler = logging.handlers.RotatingFileHandler(
            text_log_path,
            maxBytes=5 * 1024 * 1024,   # 5 Mo
            backupCount=5,
            encoding="utf-8",
        )
        self._text_handler.setFormatter(logging.Formatter(
            fmt="%(asctime)s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        self._file_logger.addHandler(self._text_handler)

    # ─── API PUBLIQUE ─────────────────────────────────────────────────────────

    def log(
        self,
        event_type: str,
        gateway_id: str = "",
        message: str = "",
        data: dict = None,
        level: str = "info",
    ) -> dict:
        """Enregistre un événement."""
        self._seq += 1
        entry = {
            "seq":        self._seq,
            "timestamp":  time.time(),
            "time_str":   datetime.now().strftime("%H:%M:%S"),
            "event_type": event_type,
            "gateway_id": gateway_id,
            "message":    message,
            "data":       data or {},
            "level":      level,
            "color":      EVENT_COLORS.get(event_type, "dim"),
        }

        self._ring.append(entry)

        # Écriture JSONL machine
        if self._log_file:
            try:
                with self._log_file.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except Exception as e:
                logger.warning(f"Erreur écriture log gateway: {e}")

        # Écriture texte lisible (tail -f gateway.log)
        gw_label = f"[{gateway_id:<8}]" if gateway_id else "[system  ]"
        type_label = f"{event_type.upper():<20}"
        text_line = f"{gw_label} {type_label} {message}"
        log_fn = getattr(self._file_logger, level if level in ("debug", "info", "warning", "error") else "info")
        log_fn(text_line)

        # Log console uvicorn (niveau warning+ seulement pour ne pas saturer)
        if level in ("warning", "error"):
            getattr(logger, level)(f"[{gateway_id}] {event_type} — {message}")

        return entry

    def get_recent(
        self,
        limit: int = 200,
        gateway_id: str = "",
        event_type: str = "",
        since_seq: int = 0,
    ) -> list[dict]:
        """Retourne les événements récents avec filtres optionnels."""
        events = list(self._ring)

        if since_seq:
            events = [e for e in events if e["seq"] > since_seq]
        if gateway_id:
            events = [e for e in events if e["gateway_id"] == gateway_id]
        if event_type:
            events = [e for e in events if e["event_type"] == event_type]

        return events[-limit:]

    def get_stats(self) -> dict:
        """Statistiques rapides pour le dashboard."""
        events = list(self._ring)
        now = time.time()
        last_5min = [e for e in events if now - e["timestamp"] < 300]

        by_gw: dict[str, int] = {}
        for e in last_5min:
            if e["gateway_id"]:
                by_gw[e["gateway_id"]] = by_gw.get(e["gateway_id"], 0) + 1

        return {
            "total_in_memory": len(events),
            "last_5min":       len(last_5min),
            "by_gateway":      by_gw,
            "last_seq":        self._seq,
        }

    # ─── HELPERS SPÉCIALISÉS ──────────────────────────────────────────────────

    def rssi_batch(self, gateway_id: str, nb_tags: int, readings: list[dict]):
        """Log d'un batch RSSI reçu."""
        rssi_vals = [r["rssi"] for r in readings]
        avg = round(sum(rssi_vals) / len(rssi_vals), 1) if rssi_vals else 0
        self.log(
            EVENT_RSSI_BATCH,
            gateway_id=gateway_id,
            message=f"{nb_tags} tag(s) détecté(s) — RSSI moy. {avg} dBm",
            data={
                "nb_tags":  nb_tags,
                "nb_readings": len(readings),
                "rssi_avg": avg,
                "rssi_min": round(min(rssi_vals), 1) if rssi_vals else 0,
                "rssi_max": round(max(rssi_vals), 1) if rssi_vals else 0,
                "tags":     list({r["tag_mac"] for r in readings}),
            },
        )

    def gateway_online(self, gateway_id: str, ip: str = ""):
        self.log(
            EVENT_GW_ONLINE,
            gateway_id=gateway_id,
            message=f"Gateway en ligne{f' ({ip})' if ip else ''}",
            data={"ip": ip},
            level="info",
        )

    def gateway_offline(self, gateway_id: str, ago: int = 0):
        self.log(
            EVENT_GW_OFFLINE,
            gateway_id=gateway_id,
            message=f"Gateway hors ligne — aucune donnée depuis {ago}s",
            data={"last_seen_ago": ago},
            level="warning",
        )

    def position_computed(self, tag_mac: str, name: str, x: float, y: float, accuracy: float, nb_anchors: int):
        self.log(
            EVENT_POSITION,
            gateway_id="server",
            message=f"{name} → x={x:.1f}m y={y:.1f}m ±{accuracy:.1f}m ({nb_anchors} ancres)",
            data={"tag_mac": tag_mac, "name": name, "x": x, "y": y, "accuracy": accuracy, "anchors": nb_anchors},
        )

    def alert_triggered(self, tag_mac: str, name: str, zone_name: str, alert_type: str):
        self.log(
            EVENT_ALERT,
            gateway_id="server",
            message=f"Alerte {alert_type} : {name} dans zone '{zone_name}'",
            data={"tag_mac": tag_mac, "name": name, "zone_name": zone_name, "alert_type": alert_type},
            level="warning",
        )

    def server_start(self, mode: str = "real"):
        self.log(
            EVENT_SERVER_START,
            gateway_id="server",
            message=f"Serveur BLETrack démarré — mode {mode}",
            data={"mode": mode},
        )

    def config_changed(self, changes: dict):
        self.log(
            EVENT_CONFIG_CHANGE,
            gateway_id="server",
            message=f"Configuration mise à jour : {', '.join(changes.keys())}",
            data=changes,
        )


# Instance globale — importée par main.py et position_engine.py
gw_logger = GatewayLogger()
