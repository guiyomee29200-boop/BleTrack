import aiosqlite
import time
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = str(Path(__file__).parent.parent / "bletrack.db")

CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    full_name TEXT,
    role TEXT NOT NULL DEFAULT 'operator',
    created_at REAL,
    last_login REAL,
    must_change_password INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS personnel (
    tag_mac TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT DEFAULT '',
    badge_id TEXT DEFAULT '',
    created_at REAL
);

CREATE TABLE IF NOT EXISTS zones (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    floor INTEGER DEFAULT 1,
    polygon TEXT DEFAULT '[]',
    alert_on_enter INTEGER DEFAULT 0,
    alert_on_exit INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS connection_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL,
    username TEXT,
    success INTEGER,
    mode TEXT
);

CREATE TABLE IF NOT EXISTS position_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag_mac TEXT,
    x REAL,
    y REAL,
    floor INTEGER,
    accuracy REAL,
    timestamp REAL
);

CREATE TABLE IF NOT EXISTS alerts (
    id TEXT PRIMARY KEY,
    type TEXT,
    tag_mac TEXT,
    person_name TEXT,
    zone_id TEXT,
    zone_name TEXT,
    timestamp REAL,
    acknowledged INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_position_history_mac ON position_history(tag_mac);
CREATE INDEX IF NOT EXISTS idx_position_history_ts ON position_history(timestamp);
CREATE INDEX IF NOT EXISTS idx_connection_logs_ts ON connection_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(timestamp);
"""

DEFAULT_CONFIG = {
    "rssi_1m": "-59",
    "path_loss": "2.5",
    "buffer_size": "8",
    "tag_timeout": "30",
}


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_TABLES)

        # Compte admin initial (admin/admin, changement forcé)
        async with db.execute(
            "SELECT id FROM users WHERE username = 'admin'"
        ) as cursor:
            if not await cursor.fetchone():
                from .auth import hash_password
                await db.execute(
                    "INSERT INTO users (username, password_hash, full_name, role, created_at, must_change_password) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("admin", hash_password("admin"), "Administrateur", "admin", time.time(), 1),
                )
                logger.info("Compte admin initial créé (admin/admin)")

        # Config BLE par défaut
        for key, value in DEFAULT_CONFIG.items():
            await db.execute(
                "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)",
                (key, value),
            )

        await db.commit()
    logger.info(f"Base de données initialisée : {DB_PATH}")


async def cleanup_old_data():
    """Supprime l'historique de positions de plus de 30 jours."""
    cutoff = time.time() - (30 * 86400)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM position_history WHERE timestamp < ?", (cutoff,)
        )
        await db.commit()
