from __future__ import annotations

import asyncio
import errno
import logging
import secrets
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4
from io import BytesIO
import base64

import aiofiles
import qrcode
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

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

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "https://nexdroppair.vercel.app"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

APP_DIR = Path(__file__).resolve().parent
static_dir = APP_DIR / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")

logger = logging.getLogger("p2p_transfer")


def _prepare_upload_root(raw_path: Path) -> Path:
    logger.info(f"Attempting to use uploads path: {raw_path}")
    try:
        raw_path.mkdir(parents=True, exist_ok=True)
        logger.info(f"Using uploads path: {raw_path}")
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
logger.info(f"Final uploads root: {uploads_root}")

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

    async def cleanup_expired(self) -> int:
        """Periodic cleanup of expired pairings"""
        async with self.lock:
            expired_ids = [pid for pid, p in self.pairings.items() if p.is_expired()]
            for pid in expired_ids:
                logger.info(f"Cleaning up expired pairing {pid}")
                self.pause_pairing(pid)
            return len(expired_ids)


class ConnectionManager:
    """Manage WebSocket connections between paired devices"""

    def __init__(self):
        self.connections: dict[str, dict[str, WebSocket]] = {}  # pairing_id -> {device_id: ws}
        self.message_queue: dict[str, dict[str, list[dict]]] = {}  # pairing_id -> {from_device: [messages]}
        self.lock = asyncio.Lock()

    async def connect(self, pairing_id: str, device_id: str, ws: WebSocket) -> None:
        await ws.accept()
        async with self.lock:
            if pairing_id not in self.connections:
                self.connections[pairing_id] = {}
            self.connections[pairing_id][device_id] = ws

            # Send queued messages to this device
            if pairing_id in self.message_queue:
                for from_device, messages in list(self.message_queue[pairing_id].items()):
                    target_device = next((d for d in self.connections[pairing_id].keys() if d != from_device), None)
                    if target_device == device_id:
                        for msg in messages:
                            try:
                                await ws.send_json(msg)
                                logger.info(f"Delivered queued message from {from_device} to {device_id}")
                            except RuntimeError:
                                pass
                        self.message_queue[pairing_id].pop(from_device, None)
                if not self.message_queue[pairing_id]:
                    self.message_queue.pop(pairing_id, None)

            # Notify if both devices are now connected
            if len(self.connections[pairing_id]) == 2:
                peer_connected_msg = {"type": "peer_connected"}
                for dev_id, sock in self.connections[pairing_id].items():
                    try:
                        await sock.send_json(peer_connected_msg)
                        logger.info(f"Sent peer_connected to {dev_id}")
                    except RuntimeError:
                        pass

    async def disconnect(self, pairing_id: str, device_id: str) -> None:
        async with self.lock:
            if pairing_id in self.connections:
                self.connections[pairing_id].pop(device_id, None)
                if not self.connections[pairing_id]:
                    self.connections.pop(pairing_id, None)
                    # Clean up message queue
                    self.message_queue.pop(pairing_id, None)

    async def send_to_peer(self, pairing_id: str, from_device_id: str, payload: dict[str, Any]) -> bool:
        """Send message to peer device. Returns True if delivered or queued."""
        async with self.lock:
            if pairing_id not in self.connections:
                logger.warning(f"Pairing {pairing_id} not in connections. Available: {list(self.connections.keys())}")
                return False

            devices = list(self.connections[pairing_id].keys())
            target_device = next((d for d in devices if d != from_device_id), None)

            if not target_device:
                logger.warning(f"No peer found for {from_device_id} in pairing {pairing_id}. Connected devices: {devices}")
                # Queue the message for when peer connects
                if pairing_id not in self.message_queue:
                    self.message_queue[pairing_id] = {}
                if from_device_id not in self.message_queue[pairing_id]:
                    self.message_queue[pairing_id][from_device_id] = []
                self.message_queue[pairing_id][from_device_id].append(payload)
                logger.info(f"Queued message from {from_device_id} for peer in pairing {pairing_id}")
                return True

            ws = self.connections[pairing_id][target_device]

        try:
            await ws.send_json(payload)
            logger.info(f"Message relayed from {from_device_id} to {target_device}")
            return True
        except RuntimeError as e:
            logger.error(f"Failed to send to {target_device}: {e}")
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
    logger.info(f"Pairing initiated: {pairing.code} by {body.device.identifier}, ID: {pairing.id}")
    return pairing.to_dict()


@app.post("/api/pairing/join/{code}", response_model=PairingCodeOut)
async def join_pairing(code: str, body: PairingCodeJoin) -> dict[str, Any]:
    """Device B joins a pairing using the code"""
    logger.info(f"Join request for code: {code}")
    pairing = await pairing_manager.join_pairing(code, body.device)
    logger.info(
        f"Pairing joined: {code} by {body.device.identifier}. "
        f"Pairing ID: {pairing.id}, Status: {pairing.status}"
    )
    return pairing.to_dict()


@app.get("/api/pairing/{code}", response_model=PairingCodeOut)
async def get_pairing_status(code: str) -> dict[str, Any]:
    """Check pairing status"""
    pairing = await pairing_manager.get_pairing(code)
    return pairing.to_dict()


@app.post("/api/pairing/{pairing_id}/files")
async def upload_to_peer(pairing_id: str, device_id: str = Form(...), file: UploadFile = File(...)) -> dict[str, Any]:
    """Upload a file to send to peer (stores in temp location)"""
    files = [file]  # Treat as list for consistent processing
    try:
        pairing = await pairing_manager.get_pairing_by_id(pairing_id)
        logger.info(f"Found pairing {pairing_id}, status: {pairing.status}")
    except HTTPException as e:
        logger.warning(f"Pairing lookup failed for {pairing_id}: {e.detail}")
        raise HTTPException(status_code=404, detail=f"Pairing not found: {e.detail}")
    
    if pairing.status != "connected":
        logger.warning(f"Pairing {pairing_id} status is {pairing.status}, not connected")
        raise HTTPException(status_code=409, detail=f"Pairing not connected (status: {pairing.status})")

    # Store files in pairing-specific directory
    pairing_dir = uploads_root / pairing_id
    pairing_dir.mkdir(parents=True, exist_ok=True)
    
    uploaded_files = []
    failed_files = []
    
    for file in files:
        try:
            target_path = pairing_dir / file.filename
            size = 0
            
            # Use larger chunks for faster processing of large files
            async with aiofiles.open(target_path, "wb") as out:
                while chunk := await file.read(10 * 1024 * 1024):  # 10MB chunks
                    size += len(chunk)
                    await out.write(chunk)
            
            uploaded_files.append({
                "filename": file.filename,
                "size": size,
                "status": "uploaded"
            })
            logger.info(f"File uploaded for pairing {pairing_id}: {file.filename} ({size} bytes)")
            
            # Notify peer via WebSocket that file is available
            if device_id:
                notification_payload = {
                    "type": "file_shared",
                    "filename": file.filename,
                    "file_size": size,
                    "mime_type": file.content_type,
                    "timestamp": int(time.time() * 1000)
                }
                
                # Send notification only to the peer, not back to sender
                success = await connection_manager.send_to_peer(pairing_id, device_id, notification_payload)
                if success:
                    logger.info(f"Sent file_shared notification for {file.filename} to peer in pairing {pairing_id}")
                else:
                    logger.warning(f"Failed to send file_shared notification for {file.filename} - peer not connected")
            else:
                logger.warning(f"No device_id provided for file upload notification: {file.filename}")
            
        except Exception as e:
            logger.error(f"Failed to write file {file.filename}: {e}")
            failed_files.append({
                "filename": file.filename,
                "error": str(e)
            })
    
    return {
        "status": "completed",
        "uploaded": uploaded_files,
        "failed": failed_files,
        "total_uploaded": len(uploaded_files),
        "total_failed": len(failed_files)
    }


@app.get("/api/pairing/{pairing_id}/files/{filename}")
async def download_from_peer(pairing_id: str, filename: str):
    """Download file from peer"""
    try:
        pairing = await pairing_manager.get_pairing_by_id(pairing_id)
        logger.info(f"Found pairing {pairing_id} for download, status: {pairing.status}")
    except HTTPException as e:
        logger.warning(f"Pairing lookup failed for download {pairing_id}: {e.detail}")
        raise HTTPException(status_code=404, detail=f"Pairing not found: {e.detail}")
    
    if pairing.status != "connected":
        logger.warning(f"Pairing {pairing_id} not connected for download (status: {pairing.status})")
        raise HTTPException(status_code=409, detail=f"Pairing not connected (status: {pairing.status})")

    path = uploads_root / pairing_id / filename
    if not path.exists():
        logger.warning(f"File not found: {path}")
        raise HTTPException(status_code=404, detail=f"File not found: {filename}")

    logger.info(f"File downloaded from pairing {pairing_id}: {filename}")
    return FileResponse(path)


@app.get("/api/pairing/{code}/qrcode")
async def generate_qrcode(code: str) -> dict[str, Any]:
    """Generate QR code for pairing code"""
    # Verify pairing code exists
    pairing = await pairing_manager.get_pairing(code)
    
    # Generate QR code
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    
    # Use direct pairing code for scanning
    qr.add_data(code)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    
    # Convert to base64 data URL
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    img_base64 = base64.b64encode(buffer.getvalue()).decode()
    data_url = f"data:image/png;base64,{img_base64}"
    
    logger.info(f"QR code generated for pairing code: {code}")
    return {"code": code, "qrcode": data_url}


# ============================================================================
# WebSocket Endpoints
# ============================================================================


@app.websocket("/ws/pairing/{pairing_id}/{device_id}")
async def websocket_peer_connection(pairing_id: str, device_id: str, ws: WebSocket):
    """WebSocket for P2P communication between paired devices"""
    logger.info(f"WebSocket connection attempt: pairing_id={pairing_id}, device_id={device_id}")
    
    try:
        pairing = await pairing_manager.get_pairing_by_id(pairing_id)
        logger.info(f"Found pairing {pairing_id}, status: {pairing.status}, code: {pairing.code}")
        
        # Allow connections for both pending (host) and connected (peer) states
        if pairing.status not in ["pending", "connected"]:
            logger.warning(f"Rejecting WebSocket for pairing {pairing_id} with status {pairing.status}")
            await ws.close(code=4000, reason=f"Pairing not in valid state: {pairing.status}")
            return

        await connection_manager.connect(pairing_id, device_id, ws)
        logger.info(f"Device {device_id} connected to pairing {pairing_id} (status: {pairing.status})")

        while True:
            data = await ws.receive_json()
            msg_type = data.get("type")

            # Handle keep-alive ping
            if msg_type == "ping":
                await ws.send_json({"type": "pong"})
                continue

            # Relay messages to peer
            if msg_type in ["text", "file_init", "file_chunk", "file_end", "file_shared", "snippet"]:
                success = await connection_manager.send_to_peer(
                    pairing_id, device_id, {"sender": device_id, **data}
                )
                if not success:
                    await ws.send_json({"type": "error", "message": "Peer not connected"})
            else:
                await ws.send_json({"type": "error", "message": f"Unknown message type: {msg_type}"})

    except HTTPException as e:
        logger.error(f"WebSocket pairing lookup failed for {pairing_id}/{device_id}: {e.detail}")
        await ws.close(code=4000, reason=f"Pairing error: {e.detail}")
    except WebSocketDisconnect:
        await connection_manager.disconnect(pairing_id, device_id)
        logger.info(f"Device {device_id} disconnected from pairing {pairing_id}")
    except Exception as e:
        logger.error(f"WebSocket error for {pairing_id}/{device_id}: {e}")
        await connection_manager.disconnect(pairing_id, device_id)


# ============================================================================
# UI Endpoints
# ============================================================================


# ============================================================================
# Cleanup Task
# ============================================================================


@app.on_event("startup")
async def startup_cleanup():
    """Start periodic cleanup of expired pairings"""
    async def cleanup_task():
        while True:
            await asyncio.sleep(60)  # Run every minute
            logger.info("Running pairing cleanup...")
            expired_count = await pairing_manager.cleanup_expired()
            if expired_count > 0:
                logger.info(f"Cleaned up {expired_count} expired pairings")

    asyncio.create_task(cleanup_task())

