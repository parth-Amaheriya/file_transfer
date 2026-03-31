from __future__ import annotations

import asyncio
import errno
import logging
import secrets
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import aiofiles
from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import get_settings
from .schemas import (
    DeviceDescriptor,
    FileTransferMessage,
    PairingCodeCreate,
    PairingCodeJoin,
    PairingCodeOut,
    TextMessage,
)

settings = get_settings()
app = FastAPI(title="P2P Transfer", version="1.0.0")

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
static_dir = APP_DIR / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")

logger = logging.getLogger("p2p_transfer")


def _prepare_upload_root(raw_path: Path) -> Path:
    try:
        raw_path.mkdir(parents=True, exist_ok=True)
        return raw_path
    except OSError as exc:
        if exc.errno != errno.EROFS:
            raise
        fallback = Path(tempfile.gettempdir()) / "transfer_hub_uploads"
        fallback.mkdir(parents=True, exist_ok=True)
        logger.warning(
            "Uploads path %s is read-only; using temporary directory %s instead.",
            raw_path,
            fallback,
        )
        return fallback


uploads_root = _prepare_upload_root(Path(settings.uploads_path))

PAIRING_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PAIRING_CODE_LENGTH = 6


class PairingCode:
    """In-memory pairing code with expiration"""

    def __init__(
        self,
        code: str,
        initiator: DeviceDescriptor,
        ttl_seconds: int = 3600,
    ):
        self.id = uuid4().hex
        self.code = code
        self.status: str = "pending"
        self.initiator = initiator
        self.peer: Optional[DeviceDescriptor] = None
        self.created_at = datetime.utcnow()
        self.connected_at: Optional[datetime] = None
        self.expires_at = self.created_at + timedelta(seconds=ttl_seconds)

    def is_expired(self) -> bool:
        return datetime.utcnow() > self.expires_at

    def connect_peer(self, peer: DeviceDescriptor) -> None:
        if self.status != "pending":
            raise ValueError("Pairing is not pending")
        if self.is_expired():
            self.status = "expired"
            raise ValueError("Pairing code expired")
        self.peer = peer
        self.status = "connected"
        self.connected_at = datetime.utcnow()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "code": self.code,
            "status": self.status,
            "initiator": self.initiator.model_dump(),
            "peer": self.peer.model_dump() if self.peer else None,
            "created_at": self.created_at,
            "connected_at": self.connected_at,
            "expires_at": self.expires_at,
        }


class PairingManager:
    """Manage in-memory pairings with auto-cleanup"""

    def __init__(self):
        self.pairings: dict[str, PairingCode] = {}
        self.codes: dict[str, PairingCode] = {}  # code -> PairingCode mapping
        self.lock = asyncio.Lock()

    async def create_pairing(self, device: DeviceDescriptor) -> PairingCode:
        async with self.lock:
            code = await self._generate_unique_code()
            pairing = PairingCode(code, device, settings.pairing_code_ttl)
            self.pairings[pairing.id] = pairing
            self.codes[code] = pairing
            return pairing

    async def _generate_unique_code(self) -> str:
        """Generate a unique pairing code"""
        while True:
            code = "".join(secrets.choice(PAIRING_CODE_ALPHABET) for _ in range(PAIRING_CODE_LENGTH))
            if code not in self.codes:
                return code

    async def join_pairing(self, code: str, device: DeviceDescriptor) -> PairingCode:
        async with self.lock:
            normalized_code = code.strip().upper()
            pairing = self.codes.get(normalized_code)

            if not pairing:
                raise HTTPException(status_code=404, detail="Pairing code not found")

            if pairing.is_expired():
                self.pause_pairing(pairing.id)
                raise HTTPException(status_code=410, detail="Pairing code expired")

            if pairing.status != "pending":
                raise HTTPException(status_code=409, detail="Pairing code already used")

            pairing.connect_peer(device)
            return pairing

    async def get_pairing(self, code: str) -> PairingCode:
        normalized_code = code.strip().upper()
        pairing = self.codes.get(normalized_code)

        if not pairing:
            raise HTTPException(status_code=404, detail="Pairing code not found")

        if pairing.is_expired():
            self.pause_pairing(pairing.id)
            raise HTTPException(status_code=410, detail="Pairing code expired")

        return pairing

    async def get_pairing_by_id(self, pairing_id: str) -> PairingCode:
        pairing = self.pairings.get(pairing_id)
        if not pairing:
            raise HTTPException(status_code=404, detail="Pairing not found")
        if pairing.is_expired():
            self.pause_pairing(pairing_id)
            raise HTTPException(status_code=410, detail="Pairing expired")
        return pairing

    def pause_pairing(self, pairing_id: str) -> None:
        """Remove expired pairing"""
        pairing = self.pairings.pop(pairing_id, None)
        if pairing:
            self.codes.pop(pairing.code, None)

    async def cleanup_expired(self) -> None:
        """Periodic cleanup of expired pairings"""
        async with self.lock:
            expired_ids = [pid for pid, p in self.pairings.items() if p.is_expired()]
            for pid in expired_ids:
                self.pause_pairing(pid)


class ConnectionManager:
    """Manage WebSocket connections between paired devices"""

    def __init__(self):
        self.connections: dict[str, dict[str, WebSocket]] = {}  # pairing_id -> {device_id: ws}
        self.lock = asyncio.Lock()

    async def connect(self, pairing_id: str, device_id: str, ws: WebSocket) -> None:
        await ws.accept()
        async with self.lock:
            if pairing_id not in self.connections:
                self.connections[pairing_id] = {}
            self.connections[pairing_id][device_id] = ws

    async def disconnect(self, pairing_id: str, device_id: str) -> None:
        async with self.lock:
            if pairing_id in self.connections:
                self.connections[pairing_id].pop(device_id, None)
                if not self.connections[pairing_id]:
                    self.connections.pop(pairing_id, None)

    async def send_to_peer(self, pairing_id: str, from_device_id: str, payload: dict[str, Any]) -> bool:
        """Send message to peer device. Returns True if delivered."""
        async with self.lock:
            if pairing_id not in self.connections:
                return False

            devices = list(self.connections[pairing_id].keys())
            target_device = next((d for d in devices if d != from_device_id), None)

            if not target_device:
                return False

            ws = self.connections[pairing_id][target_device]

        try:
            await ws.send_json(payload)
            return True
        except RuntimeError:
            await self.disconnect(pairing_id, target_device)
            return False

    async def broadcast_pair(self, pairing_id: str, payload: dict[str, Any]) -> None:
        """Send message to both devices in pair"""
        async with self.lock:
            if pairing_id not in self.connections:
                return

            sockets = list(self.connections[pairing_id].values())

        for ws in sockets:
            try:
                await ws.send_json(payload)
            except RuntimeError:
                pass


pairing_manager = PairingManager()
connection_manager = ConnectionManager()


# ============================================================================
# REST API Endpoints
# ============================================================================


@app.post("/api/pairing/initiate", response_model=PairingCodeOut)
async def initiate_pairing(body: PairingCodeCreate) -> dict[str, Any]:
    """Device A initiates a pairing and gets a code"""
    pairing = await pairing_manager.create_pairing(body.device)
    logger.info(f"Pairing initiated: {pairing.code} by {body.device.identifier}")
    return pairing.to_dict()


@app.post("/api/pairing/join/{code}", response_model=PairingCodeOut)
async def join_pairing(code: str, body: PairingCodeJoin) -> dict[str, Any]:
    """Device B joins a pairing using the code"""
    pairing = await pairing_manager.join_pairing(code, body.device)
    logger.info(
        f"Pairing joined: {code} by {body.device.identifier}. "
        f"Connected: {pairing.initiator.identifier} <-> {body.device.identifier}"
    )
    return pairing.to_dict()


@app.get("/api/pairing/{code}", response_model=PairingCodeOut)
async def get_pairing_status(code: str) -> dict[str, Any]:
    """Check pairing status"""
    pairing = await pairing_manager.get_pairing(code)
    return pairing.to_dict()


@app.post("/api/pairing/{pairing_id}/files")
async def upload_to_peer(pairing_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    """Upload file to send to peer (stores in temp location)"""
    pairing = await pairing_manager.get_pairing_by_id(pairing_id)
    if pairing.status != "connected":
        raise HTTPException(status_code=409, detail="Pairing not connected")

    # Store file in pairing-specific directory
    pairing_dir = uploads_root / pairing_id
    pairing_dir.mkdir(parents=True, exist_ok=True)
    target_path = pairing_dir / file.filename

    size = 0
    async with aiofiles.open(target_path, "wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            await out.write(chunk)

    logger.info(f"File uploaded for pairing {pairing_id}: {file.filename} ({size} bytes)")
    return {"status": "uploaded", "filename": file.filename, "size": size}


@app.get("/api/pairing/{pairing_id}/files/{filename}")
async def download_from_peer(pairing_id: str, filename: str):
    """Download file from peer"""
    pairing = await pairing_manager.get_pairing_by_id(pairing_id)
    if pairing.status != "connected":
        raise HTTPException(status_code=409, detail="Pairing not connected")

    path = uploads_root / pairing_id / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    logger.info(f"File downloaded from pairing {pairing_id}: {filename}")
    return FileResponse(path)


# ============================================================================
# WebSocket Endpoints
# ============================================================================


@app.websocket("/ws/pairing/{pairing_id}/{device_id}")
async def websocket_peer_connection(pairing_id: str, device_id: str, ws: WebSocket):
    """WebSocket for P2P communication between paired devices"""
    try:
        pairing = await pairing_manager.get_pairing_by_id(pairing_id)
        if pairing.status != "connected":
            await ws.close(code=4000, reason="Pairing not connected")
            return

        await connection_manager.connect(pairing_id, device_id, ws)
        logger.info(f"Device {device_id} connected to pairing {pairing_id}")

        while True:
            data = await ws.receive_json()
            msg_type = data.get("type")

            # Relay messages to peer
            if msg_type in ["text", "file_init", "file_chunk", "file_end"]:
                success = await connection_manager.send_to_peer(
                    pairing_id, device_id, {"sender": device_id, **data}
                )
                if not success:
                    await ws.send_json({"type": "error", "message": "Peer not connected"})
            else:
                await ws.send_json({"type": "error", "message": f"Unknown message type: {msg_type}"})

    except WebSocketDisconnect:
        await connection_manager.disconnect(pairing_id, device_id)
        logger.info(f"Device {device_id} disconnected from pairing {pairing_id}")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        await connection_manager.disconnect(pairing_id, device_id)


# ============================================================================
# UI Endpoints
# ============================================================================


@app.get("/")
async def dashboard(request: Request):
    """Dashboard to create or join pairing"""
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/chat")
async def chat_page(request: Request):
    """Chat interface for paired devices"""
    return templates.TemplateResponse("pairing_chat.html", {"request": request})


# ============================================================================
# Cleanup Task
# ============================================================================


@app.on_event("startup")
async def startup_cleanup():
    """Start periodic cleanup of expired pairings"""
    async def cleanup_task():
        while True:
            await asyncio.sleep(60)  # Run every minute
            await pairing_manager.cleanup_expired()

    asyncio.create_task(cleanup_task())

