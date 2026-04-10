"""
BLETrack — Serveur principal FastAPI
Routes REST + WebSocket + service des fichiers HTML
"""

import asyncio
import json
import logging
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite
from fastapi import (
    Depends, FastAPI, File, HTTPException, Query, UploadFile,
    WebSocket, WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .auth import (
    create_token, get_current_user, hash_password,
    require_admin, verify_password,
)
from .database import DB_PATH, init_db
from .models import (
    ConfigUpdate, LoginRequest, PasswordChange,
    PersonnelCreate, PersonnelUpdate,
    RSSIBatch, TokenResponse,
    UserCreate, UserUpdate,
    ZoneCreate, ZoneUpdate,
)
from .position_engine import PositionEngine
from .gateway_logger import gw_logger, GatewayLogger

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger(__name__)

def _setup_server_log(log_dir: Path):
    """Ajoute un fichier log rotatif pour tout le serveur."""
    import logging.handlers
    log_dir.mkdir(parents=True, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(
        log_dir / "server.log",
        maxBytes=10 * 1024 * 1024,  # 10 Mo
        backupCount=5,
        encoding="utf-8",
    )
    h.setFormatter(logging.Formatter(
        fmt="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logging.getLogger().addHandler(h)

# Répertoire contenant les fichiers HTML
DASHBOARD_DIR = Path(__file__).parent.parent / "Projet"

# Répertoire pour les plans uploadés
UPLOADS_DIR = Path(__file__).parent.parent / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)

# Répertoire pour les logs
LOGS_DIR = Path(__file__).parent.parent / "logs"
gw_logger.__init__(LOGS_DIR)  # Réinitialise avec le bon dossier
_setup_server_log(LOGS_DIR)

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "svg", "webp"}

# ─── WEBSOCKET MANAGER ───────────────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self._connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._connections.append(ws)
        logger.info(f"WebSocket connecté ({len(self._connections)} actifs)")

    def disconnect(self, ws: WebSocket):
        self._connections.remove(ws)
        logger.info(f"WebSocket déconnecté ({len(self._connections)} actifs)")

    async def broadcast(self, data: dict):
        dead = []
        for ws in self._connections:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._connections.discard(ws) if hasattr(self._connections, "discard") else None
            try:
                self._connections.remove(ws)
            except ValueError:
                pass


manager = ConnectionManager()
engine = PositionEngine(simulation=False)


# ─── BROADCAST LOOP ──────────────────────────────────────────────────────────

async def broadcast_loop():
    """Diffuse les positions + statut gateways toutes les secondes."""
    while True:
        try:
            has_live    = bool(manager._connections)
            has_alerts  = bool(alerts_manager._connections)
            if has_live or has_alerts:
                positions = await engine.get_positions()
                gateways  = engine.get_gateways_status()
                if has_live:
                    await manager.broadcast({
                        "type":     "positions",
                        "data":     positions,
                        "gateways": gateways,
                    })
                if has_alerts:
                    await alerts_manager.broadcast({
                        "type":     "status",
                        "tags":     [
                            {"name": p["name"], "tag_mac": p["tag_mac"], "online": p["online"]}
                            for p in positions
                        ],
                        "gateways": [
                            {"id": gw["id"], "name": gw["name"], "ip": gw["ip"], "online": gw["online"]}
                            for gw in gateways
                        ],
                    })
        except Exception as e:
            logger.error(f"Erreur broadcast: {e}")
        await asyncio.sleep(1)


# ─── LIFESPAN ────────────────────────────────────────────────────────────────

async def gateway_watchdog():
    """Détecte les gateways qui passent offline (aucune donnée depuis GATEWAY_TIMEOUT)."""
    notified_offline: dict[str, bool] = {}
    while True:
        await asyncio.sleep(15)
        statuses = engine.get_gateways_status()
        for gw in statuses:
            gw_id = gw["id"]
            if not gw["online"] and gw["last_seen"] is not None:
                if not notified_offline.get(gw_id):
                    notified_offline[gw_id] = True
                    gw_logger.gateway_offline(gw_id, gw["ago"] or 0)
                    # Purge les buffers RSSI de cette gateway pour éviter positions aberrantes
                    engine.flush_gateway_buffers(gw_id)
            elif gw["online"]:
                notified_offline[gw_id] = False


async def _restore_engine_config():
    """Charge depuis la DB : config BLE globale + positions gateways de tous les plans."""
    async with aiosqlite.connect(DB_PATH) as db:
        # Config BLE globale (rssi_1m, path_loss, buffer_size, tag_timeout)
        cfg_keys = ("rssi_1m", "path_loss", "buffer_size", "tag_timeout")
        updates = {}
        for key in cfg_keys:
            async with db.execute("SELECT value FROM config WHERE key = ?", (key,)) as cur:
                row = await cur.fetchone()
            if row:
                updates[key] = float(row[0])
        if updates:
            engine.update_config(updates)
            logger.info(f"Config BLE restaurée : {updates}")

        # Positions gateways depuis le plan du bâtiment (tous étages)
        async with db.execute("SELECT key, value FROM config WHERE key LIKE 'floorplan_%'") as cur:
            plans = await cur.fetchall()

    gw_map: dict[str, dict] = {}
    for key, val in plans:
        meta = json.loads(val)
        w, h = meta.get("width", 30.0), meta.get("height", 10.0)
        floor = int(key.split("_")[1])
        for gw in meta.get("gateways", []):
            gw_id = gw["id"]
            if gw.get("px") is not None and gw.get("py") is not None:
                gw_map[gw_id] = {
                    "id":    gw_id,
                    "name":  gw.get("name", gw_id),
                    "ip":    gw.get("ip", ""),
                    "floor": floor,
                    "x":     round(float(gw["px"]) * w, 3),
                    "y":     round(float(gw["py"]) * h, 3),
                }
    if gw_map:
        engine.set_gateways(list(gw_map.values()))
        logger.info(f"Gateways restaurées depuis le plan : { {k: (v['x'], v['y']) for k,v in gw_map.items()} }")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await _restore_engine_config()
    await engine.start()
    gw_logger.server_start("real")
    task1 = asyncio.create_task(broadcast_loop())
    task2 = asyncio.create_task(gateway_watchdog())
    logger.info("BLETrack démarré — http://127.0.0.1:8000")
    yield
    task1.cancel()
    task2.cancel()
    await engine.stop()


# ─── APP ─────────────────────────────────────────────────────────────────────

app = FastAPI(title="BLETrack API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Fichiers statiques
if DASHBOARD_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(DASHBOARD_DIR)), name="static")
app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")


# ─── PAGES HTML ──────────────────────────────────────────────────────────────

_NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}

@app.get("/", include_in_schema=False)
async def serve_home():
    return FileResponse(DASHBOARD_DIR / "index.html", headers=_NO_CACHE)


@app.get("/login", include_in_schema=False)
async def serve_login():
    return FileResponse(DASHBOARD_DIR / "login.html", headers=_NO_CACHE)


@app.get("/combat", include_in_schema=False)
async def serve_combat():
    return FileResponse(DASHBOARD_DIR / "combat.html", headers=_NO_CACHE)


@app.get("/admin", include_in_schema=False)
async def serve_admin():
    return FileResponse(DASHBOARD_DIR / "admin.html", headers=_NO_CACHE)


@app.get("/alerte", include_in_schema=False)
async def serve_alerte():
    return FileResponse(DASHBOARD_DIR / "alerte.html", headers=_NO_CACHE)


# ─── AUTH ────────────────────────────────────────────────────────────────────

@app.post("/api/auth/login", response_model=TokenResponse, tags=["Auth"])
async def login(req: LoginRequest):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE username = ?", (req.username,)
        ) as cur:
            user = await cur.fetchone()

        success = user is not None and verify_password(req.password, user["password_hash"])

        # Journaliser la tentative (succès ou échec)
        await db.execute(
            "INSERT INTO connection_logs (timestamp, username, success, mode) VALUES (?, ?, ?, ?)",
            (time.time(), req.username, 1 if success else 0, req.mode),
        )

        if success:
            await db.execute(
                "UPDATE users SET last_login = ? WHERE username = ?",
                (time.time(), req.username),
            )

        await db.commit()

    if not success:
        raise HTTPException(status_code=401, detail="Identifiant ou mot de passe incorrect")

    token = create_token({"sub": user["username"], "role": user["role"]})
    return TokenResponse(
        access_token=token,
        role=user["role"],
        must_change_password=bool(user["must_change_password"]),
    )


@app.post("/api/auth/change-password", tags=["Auth"])
async def change_password(req: PasswordChange, user=Depends(get_current_user)):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT password_hash FROM users WHERE username = ?", (user["username"],)
        ) as cur:
            row = await cur.fetchone()

    if not row or not verify_password(req.old_password, row["password_hash"]):
        raise HTTPException(status_code=400, detail="Ancien mot de passe incorrect")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET password_hash = ?, must_change_password = 0 WHERE username = ?",
            (hash_password(req.new_password), user["username"]),
        )
        await db.commit()

    return {"message": "Mot de passe modifié avec succès"}


@app.get("/api/auth/me", tags=["Auth"])
async def me(user=Depends(get_current_user)):
    return user


# ─── POSITIONS ───────────────────────────────────────────────────────────────

@app.get("/api/positions", tags=["Positions"])
async def get_positions(user=Depends(get_current_user)):
    return await engine.get_positions()


@app.get("/api/positions/debug", tags=["Positions"])
async def debug_positions(user=Depends(get_current_user)):
    """Debug : état interne du moteur pour chaque tag enregistré."""
    now = time.time()
    timeout = engine.config["tag_timeout"]
    result = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT tag_mac, name FROM personnel") as cur:
            personnel = {r["tag_mac"]: r["name"] for r in await cur.fetchall()}
    for mac, name in personnel.items():
        pos = engine._positions.get(mac)
        buffers = engine._rssi_buffers.get(mac, {})
        age = round(now - pos["timestamp"], 1) if pos else None
        result.append({
            "mac":        mac,
            "name":       name,
            "online":     bool(pos and age < timeout),
            "last_pos_age_s": age,
            "timeout_s":  timeout,
            "rssi_buffers": {gw: buf for gw, buf in buffers.items()},
            "live_rssi":  {
                gw: engine._live_rssi[gw].get(mac, {})
                for gw in engine._live_rssi
                if mac in engine._live_rssi[gw]
            },
        })
    return result


@app.post("/api/positions/rssi", tags=["Positions"])
async def receive_rssi(batch: RSSIBatch, user=Depends(get_current_user)):
    """Endpoint MQTT-to-HTTP de secours : un gateway peut POST ses RSSI ici."""
    readings = [{"tag_mac": r.tag_mac, "rssi": r.rssi} for r in batch.readings]
    await engine.process_rssi_batch(batch.gateway_id, readings)
    return {"message": f"{len(readings)} mesure(s) traitée(s)"}


# ─── PERSONNEL ───────────────────────────────────────────────────────────────

@app.get("/api/personnel", tags=["Personnel"])
async def list_personnel(user=Depends(get_current_user)):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM personnel ORDER BY name") as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


@app.post("/api/personnel", status_code=201, tags=["Personnel"])
async def create_personnel(data: PersonnelCreate, user=Depends(require_admin)):
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO personnel (tag_mac, name, role, badge_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (data.tag_mac, data.name, data.role, data.badge_id, time.time()),
            )
            await db.commit()
        except aiosqlite.IntegrityError:
            raise HTTPException(status_code=409, detail="Adresse MAC déjà enregistrée")
    return {"message": "Personnel créé", "tag_mac": data.tag_mac}


@app.put("/api/personnel/{tag_mac}", tags=["Personnel"])
async def update_personnel(tag_mac: str, data: PersonnelUpdate, user=Depends(require_admin)):
    updates = {k: v for k, v in data.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="Aucune donnée à mettre à jour")
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE personnel SET {set_clause} WHERE tag_mac = ?",
            [*updates.values(), tag_mac],
        )
        await db.commit()
    return {"message": "Personnel mis à jour"}


@app.delete("/api/personnel/{tag_mac}", tags=["Personnel"])
async def delete_personnel(tag_mac: str, user=Depends(require_admin)):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM personnel WHERE tag_mac = ?", (tag_mac,))
        await db.commit()
    return {"message": "Personnel supprimé"}


# ─── ZONES ───────────────────────────────────────────────────────────────────

@app.get("/api/zones", tags=["Zones"])
async def list_zones(user=Depends(get_current_user)):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM zones ORDER BY name") as cur:
            rows = await cur.fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["polygon"] = json.loads(d["polygon"])
        d["alert_on_enter"] = bool(d["alert_on_enter"])
        d["alert_on_exit"] = bool(d["alert_on_exit"])
        result.append(d)
    return result


@app.post("/api/zones", status_code=201, tags=["Zones"])
async def create_zone(data: ZoneCreate, user=Depends(require_admin)):
    zone_id = str(uuid.uuid4())[:8]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO zones (id, name, floor, polygon, alert_on_enter, alert_on_exit) VALUES (?, ?, ?, ?, ?, ?)",
            (zone_id, data.name, data.floor, json.dumps(data.polygon),
             int(data.alert_on_enter), int(data.alert_on_exit)),
        )
        await db.commit()
    return {"id": zone_id, "message": "Zone créée"}


@app.put("/api/zones/{zone_id}", tags=["Zones"])
async def update_zone(zone_id: str, data: ZoneUpdate, user=Depends(require_admin)):
    updates = {k: v for k, v in data.model_dump().items() if v is not None}
    if "polygon" in updates:
        updates["polygon"] = json.dumps(updates["polygon"])
    if "alert_on_enter" in updates:
        updates["alert_on_enter"] = int(updates["alert_on_enter"])
    if "alert_on_exit" in updates:
        updates["alert_on_exit"] = int(updates["alert_on_exit"])
    if not updates:
        raise HTTPException(status_code=400, detail="Aucune donnée à mettre à jour")
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE zones SET {set_clause} WHERE id = ?",
            [*updates.values(), zone_id],
        )
        await db.commit()
    return {"message": "Zone mise à jour"}


@app.delete("/api/zones/{zone_id}", tags=["Zones"])
async def delete_zone(zone_id: str, user=Depends(require_admin)):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM zones WHERE id = ?", (zone_id,))
        await db.commit()
    return {"message": "Zone supprimée"}


# ─── ALERTES ─────────────────────────────────────────────────────────────────

@app.get("/api/alerts", tags=["Alertes"])
async def list_alerts(
    limit: int = Query(50, ge=1, le=200),
    user=Depends(get_current_user),
):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM alerts ORDER BY timestamp DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


@app.post("/api/alerts/{alert_id}/ack", tags=["Alertes"])
async def ack_alert(alert_id: str, user=Depends(get_current_user)):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE alerts SET acknowledged = 1 WHERE id = ?", (alert_id,)
        )
        await db.commit()
    # Notifier les clients WebSocket
    await manager.broadcast({"type": "alert_acked", "id": alert_id})
    return {"message": "Alerte acquittée"}


# ─── UTILISATEURS ────────────────────────────────────────────────────────────

@app.get("/api/users", tags=["Utilisateurs"])
async def list_users(user=Depends(require_admin)):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, username, full_name, role, created_at, last_login, must_change_password "
            "FROM users ORDER BY username"
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


@app.post("/api/users", status_code=201, tags=["Utilisateurs"])
async def create_user(data: UserCreate, user=Depends(require_admin)):
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO users (username, password_hash, full_name, role, created_at) VALUES (?, ?, ?, ?, ?)",
                (data.username, hash_password(data.password), data.full_name, data.role, time.time()),
            )
            await db.commit()
        except aiosqlite.IntegrityError:
            raise HTTPException(status_code=409, detail="Nom d'utilisateur déjà utilisé")
    return {"message": "Utilisateur créé", "username": data.username}


@app.put("/api/users/{username}", tags=["Utilisateurs"])
async def update_user(username: str, data: UserUpdate, user=Depends(require_admin)):
    async with aiosqlite.connect(DB_PATH) as db:
        if data.password:
            await db.execute(
                "UPDATE users SET password_hash = ? WHERE username = ?",
                (hash_password(data.password), username),
            )
        if data.full_name:
            await db.execute(
                "UPDATE users SET full_name = ? WHERE username = ?",
                (data.full_name, username),
            )
        if data.role:
            await db.execute(
                "UPDATE users SET role = ? WHERE username = ?",
                (data.role, username),
            )
        await db.commit()
    return {"message": "Utilisateur mis à jour"}


@app.delete("/api/users/{username}", tags=["Utilisateurs"])
async def delete_user(username: str, user=Depends(require_admin)):
    if username == "admin":
        raise HTTPException(status_code=400, detail="Impossible de supprimer le compte admin principal")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM users WHERE username = ?", (username,))
        await db.commit()
    return {"message": "Utilisateur supprimé"}


# ─── LOGS ────────────────────────────────────────────────────────────────────

@app.get("/api/logs", tags=["Logs"])
async def get_logs(
    username: str = Query(None),
    limit: int = Query(100, ge=1, le=500),
    user=Depends(require_admin),
):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if username:
            async with db.execute(
                "SELECT * FROM connection_logs WHERE username LIKE ? ORDER BY timestamp DESC LIMIT ?",
                (f"%{username}%", limit),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute(
                "SELECT * FROM connection_logs ORDER BY timestamp DESC LIMIT ?", (limit,)
            ) as cur:
                rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ─── CONFIG ──────────────────────────────────────────────────────────────────

@app.get("/api/config", tags=["Config"])
async def get_config(user=Depends(require_admin)):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT key, value FROM config") as cur:
            rows = await cur.fetchall()
    return {r["key"]: r["value"] for r in rows}


@app.put("/api/config", tags=["Config"])
async def update_config(data: ConfigUpdate, user=Depends(require_admin)):
    updates = {k: v for k, v in data.model_dump().items() if v is not None}
    async with aiosqlite.connect(DB_PATH) as db:
        for key, value in updates.items():
            await db.execute(
                "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                (key, str(value)),
            )
        await db.commit()
    engine.update_config(updates)
    gw_logger.config_changed(updates)
    return {"message": "Configuration sauvegardée"}


@app.get("/api/gateways", tags=["Config"])
async def get_gateways(user=Depends(get_current_user)):
    """Retourne la liste des gateways avec statut, stats et calibration."""
    return engine.get_gateways_status()


@app.put("/api/gateways/{gw_id}", tags=["Config"])
async def update_gateway(gw_id: str, data: dict, user=Depends(require_admin)):
    """Met à jour la config d'une gateway (nom, IP, calibration RSSI)."""
    ok = engine.update_gateway(gw_id, data)
    if not ok:
        raise HTTPException(404, f"Gateway '{gw_id}' introuvable")
    return {"message": "Gateway mise à jour"}


@app.post("/api/gateways", tags=["Config"])
async def add_gateway(data: dict, user=Depends(require_admin)):
    """Ajoute une nouvelle gateway."""
    ok = engine.add_gateway(data)
    if not ok:
        raise HTTPException(400, "ID manquant ou déjà existant")
    return {"message": "Gateway ajoutée"}


@app.delete("/api/gateways/{gw_id}", tags=["Config"])
async def delete_gateway(gw_id: str, user=Depends(require_admin)):
    """Supprime une gateway."""
    ok = engine.delete_gateway(gw_id)
    if not ok:
        raise HTTPException(404, f"Gateway '{gw_id}' introuvable")
    return {"message": "Gateway supprimée"}


@app.get("/api/gateways/{gw_id}/rssi_live", tags=["Config"])
async def gateway_rssi_live(gw_id: str, user=Depends(get_current_user)):
    """Retourne le RSSI live de tous les tags visibles par cette gateway."""
    return engine.get_gateway_live_rssi(gw_id)


@app.delete("/api/rssi_buffer", tags=["Config"])
async def clear_rssi_buffer(tag_mac: str = Query(""), user=Depends(require_admin)):
    """Vide le buffer RSSI (tag spécifique ou tous)."""
    engine.clear_rssi_buffer(tag_mac.upper() if tag_mac else "")
    return {"message": "Buffer vidé"}


@app.post("/api/gateways/{gw_id}/calibrate", tags=["Config"])
async def calibrate_gateway(
    gw_id: str,
    tag_mac: str = Query(..., description="MAC du tag placé à 1m"),
    duration: int = Query(5, ge=3, le=30, description="Durée de mesure en secondes"),
    save: bool = Query(False, description="Sauvegarder automatiquement le rssi_1m mesuré"),
    user=Depends(require_admin),
):
    """Mesure le RSSI moyen d'un tag sur N secondes et retourne la moyenne (optionnellement sauvegarde rssi_1m)."""
    if gw_id not in engine.gateways:
        raise HTTPException(404, f"Gateway '{gw_id}' introuvable")
    result = await engine.measure_rssi(gw_id, tag_mac.upper(), duration)
    if save and result["rssi_avg"] is not None:
        engine.update_gateway(gw_id, {"rssi_1m": result["rssi_avg"]})
        result["saved"] = True
    return result


@app.get("/api/logs/gateway", tags=["Logs"])
async def get_gateway_logs(
    limit:      int = Query(200, ge=1, le=1000),
    gateway_id: str = Query(""),
    event_type: str = Query(""),
    since_seq:  int = Query(0),
    user=Depends(require_admin),
):
    """Retourne les logs récents des gateways (ring buffer mémoire)."""
    events = gw_logger.get_recent(
        limit=limit,
        gateway_id=gateway_id,
        event_type=event_type,
        since_seq=since_seq,
    )
    return {
        "events": list(reversed(events)),  # Plus récent en premier
        "stats":  gw_logger.get_stats(),
    }


@app.get("/api/ble/scan", tags=["BLE"])
async def ble_scan(duration: int = Query(5, ge=2, le=15), user=Depends(require_admin)):
    """Scan BLE rapide — retourne les appareils détectés avec MAC et RSSI."""
    try:
        from bleak import BleakScanner
    except ImportError:
        raise HTTPException(503, "bleak non installé sur ce serveur")

    results: dict[str, dict] = {}

    def callback(device, adv):
        results[device.address] = {
            "mac":  device.address,
            "name": device.name or "",
            "rssi": adv.rssi,
        }

    try:
        async with BleakScanner(callback):
            await asyncio.sleep(duration)
    except Exception as e:
        raise HTTPException(500, f"Erreur scan BLE : {e}")

    devices = sorted(results.values(), key=lambda d: -d["rssi"])
    return {"devices": devices, "count": len(devices), "duration": duration}


# ─── PLAN DU BÂTIMENT ────────────────────────────────────────────────────────

@app.post("/api/floorplan/{floor}", tags=["Plan"])
async def upload_floorplan(
    floor: int,
    file: UploadFile = File(...),
    user=Depends(require_admin),
):
    """Upload l'image du plan pour un étage donné."""
    ext = (file.filename or "").rsplit(".", 1)[-1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Format non supporté. Acceptés : {', '.join(ALLOWED_EXTENSIONS)}")

    # Supprimer l'ancien fichier de cet étage s'il existe
    for old in UPLOADS_DIR.glob(f"floorplan_{floor}.*"):
        old.unlink()

    filename = f"floorplan_{floor}.{ext}"
    dest = UPLOADS_DIR / filename
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    # Méta par défaut : dimensions 30m × 10m, pas de gateways placés
    async with aiosqlite.connect(DB_PATH) as db:
        meta = {"file": filename, "width": 30.0, "height": 10.0, "gateways": []}
        await db.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
            (f"floorplan_{floor}", json.dumps(meta)),
        )
        await db.commit()

    return {"message": "Plan importé", "url": f"/uploads/{filename}"}


@app.get("/api/floorplan/{floor}", tags=["Plan"])
async def get_floorplan(floor: int, user=Depends(get_current_user)):
    """Retourne les métadonnées du plan d'un étage (url, dimensions, positions gateways)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT value FROM config WHERE key = ?", (f"floorplan_{floor}",)
        ) as cur:
            row = await cur.fetchone()

    if not row:
        return None

    meta = json.loads(row[0])
    meta["url"] = f"/uploads/{meta['file']}"
    return meta


@app.put("/api/floorplan/{floor}", tags=["Plan"])
async def update_floorplan(floor: int, data: dict, user=Depends(require_admin)):
    """
    Sauvegarde les dimensions réelles et les positions des gateways sur le plan.
    data = {width: float, height: float, gateways: [{id, name, ip, px, py}]}
    px/py sont des coordonnées normalisées [0-1] par rapport aux dimensions de l'image.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT value FROM config WHERE key = ?", (f"floorplan_{floor}",)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            raise HTTPException(404, "Aucun plan pour cet étage")

        meta = json.loads(row[0])
        if "width" in data:
            meta["width"] = float(data["width"])
        if "height" in data:
            meta["height"] = float(data["height"])
        if "gateways" in data:
            meta["gateways"] = data["gateways"]

        await db.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
            (f"floorplan_{floor}", json.dumps(meta)),
        )
        await db.commit()

    # Mettre à jour le moteur de positionnement avec les vraies positions
    if "gateways" in data and data["gateways"]:
        gw_list = []
        w = meta["width"]
        h = meta["height"]
        for gw in data["gateways"]:
            if gw.get("px") is not None and gw.get("py") is not None:
                gw_list.append({
                    "id":    gw["id"],
                    "name":  gw.get("name", gw["id"]),
                    "ip":    gw.get("ip", ""),
                    "floor": floor,
                    "x":     float(gw["px"]) * w,
                    "y":     float(gw["py"]) * h,
                })
        if gw_list:
            engine.set_gateways(gw_list)

    return {"message": "Plan mis à jour"}


@app.delete("/api/floorplan/{floor}", tags=["Plan"])
async def delete_floorplan(floor: int, user=Depends(require_admin)):
    """Supprime le plan d'un étage."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT value FROM config WHERE key = ?", (f"floorplan_{floor}",)
        ) as cur:
            row = await cur.fetchone()
        if row:
            meta = json.loads(row[0])
            old = UPLOADS_DIR / meta.get("file", "")
            if old.exists():
                old.unlink()
            await db.execute(
                "DELETE FROM config WHERE key = ?", (f"floorplan_{floor}",)
            )
            await db.commit()

    return {"message": "Plan supprimé"}


# ─── WEBSOCKET ───────────────────────────────────────────────────────────────

# Manager dédié aux alertes publiques (pas de positions, pas d'auth)
alerts_manager = ConnectionManager()

@app.websocket("/ws/alerts")
async def websocket_alerts(ws: WebSocket):
    """WebSocket public — envoie uniquement l'état online/offline des tags enregistrés."""
    await alerts_manager.connect(ws)
    try:
        while True:
            msg = await ws.receive_text()
            if msg == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        alerts_manager.disconnect(ws)


@app.websocket("/ws/live")
async def websocket_live(ws: WebSocket):
    """
    WebSocket temps réel.
    Le client peut passer son token en query param : /ws/live?token=<jwt>
    """
    # Validation JWT optionnelle (ne bloque pas si absent en dev)
    token = ws.query_params.get("token")
    if token:
        try:
            from .auth import decode_token
            decode_token(token)
        except Exception:
            await ws.close(code=4001)
            return

    await manager.connect(ws)
    try:
        while True:
            # Écoute les messages du client (ex: ping keepalive)
            msg = await ws.receive_text()
            if msg == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        manager.disconnect(ws)
