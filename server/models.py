from pydantic import BaseModel, field_validator
from typing import Optional, List


# ─── AUTH ────────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str
    mode: str = "combat"  # "combat" ou "admin"


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    must_change_password: bool = False


class PasswordChange(BaseModel):
    old_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def password_not_empty(cls, v):
        if len(v) < 4:
            raise ValueError("Le mot de passe doit comporter au moins 4 caractères")
        return v


# ─── USERS ───────────────────────────────────────────────────────────────────

class UserCreate(BaseModel):
    username: str
    password: str
    full_name: str
    role: str = "operator"

    @field_validator("role")
    @classmethod
    def valid_role(cls, v):
        if v not in ("admin", "operator"):
            raise ValueError("Rôle invalide : admin ou operator")
        return v


class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    role: Optional[str] = None
    password: Optional[str] = None


class UserOut(BaseModel):
    id: int
    username: str
    full_name: Optional[str]
    role: str
    created_at: float
    last_login: Optional[float]
    must_change_password: int


# ─── PERSONNEL ───────────────────────────────────────────────────────────────

class PersonnelCreate(BaseModel):
    tag_mac: str
    name: str
    role: str = ""
    badge_id: str = ""

    @field_validator("tag_mac")
    @classmethod
    def mac_format(cls, v):
        v = v.upper().strip()
        parts = v.split(":")
        if len(parts) != 6 or not all(len(p) == 2 for p in parts):
            raise ValueError("Format MAC invalide (ex: AA:BB:CC:DD:EE:FF)")
        return v


class PersonnelUpdate(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    badge_id: Optional[str] = None


class PersonnelOut(BaseModel):
    tag_mac: str
    name: str
    role: str
    badge_id: str
    created_at: float


# ─── ZONES ───────────────────────────────────────────────────────────────────

class ZoneCreate(BaseModel):
    name: str
    floor: int = 1
    polygon: List[List[float]] = []
    alert_on_enter: bool = False
    alert_on_exit: bool = False


class ZoneUpdate(BaseModel):
    name: Optional[str] = None
    floor: Optional[int] = None
    polygon: Optional[List[List[float]]] = None
    alert_on_enter: Optional[bool] = None
    alert_on_exit: Optional[bool] = None


class ZoneOut(BaseModel):
    id: str
    name: str
    floor: int
    polygon: List[List[float]]
    alert_on_enter: bool
    alert_on_exit: bool


# ─── ALERTES ─────────────────────────────────────────────────────────────────

class AlertOut(BaseModel):
    id: str
    type: str
    tag_mac: str
    person_name: str
    zone_id: str
    zone_name: str
    timestamp: float
    acknowledged: bool


# ─── POSITIONS ───────────────────────────────────────────────────────────────

class PositionOut(BaseModel):
    tag_mac: str
    name: str
    role: str
    x: float
    y: float
    floor: int
    accuracy: float
    timestamp: float
    online: bool


# ─── CONFIG ──────────────────────────────────────────────────────────────────

class ConfigUpdate(BaseModel):
    rssi_1m: Optional[float] = None
    path_loss: Optional[float] = None
    buffer_size: Optional[int] = None
    tag_timeout: Optional[int] = None


# ─── GATEWAY (MQTT) ──────────────────────────────────────────────────────────

class RSSIReading(BaseModel):
    tag_mac: str
    rssi: float


class RSSIBatch(BaseModel):
    gateway_id: str
    timestamp: float
    readings: List[RSSIReading]
