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
import json
import os
import hmac
import hashlib
from urllib.parse import quote

import qrcode
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .admin_store import AdminStore
from .config import get_settings
from .schemas import (
    AdminDashboardOut,
    AdminForgotPasswordRequest,
    AdminForgotPasswordVerifyRequest,
    AdminLoginRequest,
    AdminLoginResponse,
    AdminResetPasswordRequest,
    AdminSettingsUpdate,
    AdminUserCreate,
    AdminUserOut,
    AdminUserUpdate,
    AuditLogEntry,
    DeviceDescriptor,
    FileTransferMessage,
    PairingCodeCreate,
    PairingCodeJoin,
    PairingCodeOut,
    PublicConfigOut,
    RuntimeSettings,
    TextMessage,
)

settings = get_settings()
app = FastAPI(title="Nex-Drop - Next Gen Data Transfer", version="1.0.0",docs_url="/nex-drop-docs")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://nexdroppair.vercel.app",
        "https://www.nex-drop.app",
        "http://localhost:8080",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

APP_DIR = Path(__file__).resolve().parent
static_dir = APP_DIR / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")

logger = logging.getLogger("p2p_transfer")

# Configure logging to print to console
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


@app.get("/")
async def root() -> dict[str, str]:
    return {"status": "ok"}


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


def _build_pairing_join_url(request: Request, code: str) -> str:
    base_url = (settings.frontend_url or request.headers.get("origin") or str(request.base_url)).rstrip("/")
    return f"{base_url}/{quote(code)}"


admin_store = AdminStore(settings)
runtime_settings = RuntimeSettings()
bearer_scheme = HTTPBearer(auto_error=False)


class SlidingWindowRateLimiter:
    def __init__(self) -> None:
        self.events: dict[str, list[float]] = {}
        self.lock = asyncio.Lock()

    async def check(self, key: str, limit: int, window_seconds: int = 60) -> None:
        now = time.time()
        async with self.lock:
            recent_events = [timestamp for timestamp in self.events.get(key, []) if now - timestamp < window_seconds]
            if len(recent_events) >= limit:
                raise HTTPException(status_code=429, detail="Rate limit exceeded")
            recent_events.append(now)
            self.events[key] = recent_events


pairing_rate_limiter = SlidingWindowRateLimiter()
admin_rate_limiter = SlidingWindowRateLimiter()


class PairingCode:
    """In-memory pairing code with expiration - supports multiple devices"""

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
        self.peers: list[DeviceDescriptor] = []  # Changed from single peer to list
        self.created_at = datetime.utcnow()
        self.connected_at: Optional[datetime] = None
        self.expires_at = self.created_at + timedelta(seconds=ttl_seconds)
        self.terminated_at: Optional[datetime] = None
        self.termination_reason: Optional[str] = None

    @property
    def device_count(self) -> int:
        return 1 + len(self.peers)

    def is_expired(self) -> bool:
        return self.status != "terminated" and datetime.utcnow() > self.expires_at

    def connect_peer(self, peer: DeviceDescriptor) -> None:
        """Add a peer to the connection - supports multiple peers"""
        if self.status == "terminated":
            raise ValueError("Pairing has been terminated")

        if self.is_expired():
            self.status = "expired"
            raise ValueError("Pairing code expired")
        
        # Add peer to the list if not already present
        for existing_peer in self.peers:
            if existing_peer.identifier == peer.identifier:
                return  # Peer already connected
        
        self.peers.append(peer)
        
        # Update status only on first peer connection
        if self.status == "pending":
            self.status = "connected"
            self.connected_at = datetime.utcnow()

    def terminate(self, reason: str) -> None:
        self.status = "terminated"
        self.terminated_at = datetime.utcnow()
        self.termination_reason = reason

    def remove_peer(self, peer_identifier: str) -> bool:
        """Remove a peer from the pairing."""
        if self.status == "terminated":
            return False

        original_count = len(self.peers)
        self.peers = [peer for peer in self.peers if peer.identifier != peer_identifier]

        if self.peers:
            self.status = "connected"
        elif self.status != "expired":
            self.status = "pending"

        return len(self.peers) != original_count

    @staticmethod
    def _serialize_datetime(value: Optional[datetime]) -> Optional[str]:
        if value is None:
            return None
        return value.isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "code": self.code,
            "status": self.status,
            "initiator": self.initiator.model_dump(),
            "peers": [peer.model_dump() for peer in self.peers],  # Return list of peers
            "peer_count": len(self.peers),  # Also provide peer count
            "device_count": self.device_count,
            "created_at": self._serialize_datetime(self.created_at),
            "connected_at": self._serialize_datetime(self.connected_at),
            "expires_at": self._serialize_datetime(self.expires_at),
            "terminated_at": self._serialize_datetime(self.terminated_at),
            "termination_reason": self.termination_reason,
        }


class PairingManager:
    """Manage in-memory pairings with auto-cleanup"""

    def __init__(self):
        self.pairings: dict[str, PairingCode] = {}
        self.codes: dict[str, PairingCode] = {}  # code -> PairingCode mapping
        self.lock = asyncio.Lock()
        self.sse_queues: dict[str, set[asyncio.Queue]] = {}  # pairing_id -> subscriber queues for SSE updates
        self.persistence_file = uploads_root / "pairings.json"
        self._load_pairings()

    def _load_pairings(self):
        """Load pairings from file"""
        if self.persistence_file.exists():
            try:
                with open(self.persistence_file, 'r') as f:
                    data = json.load(f)
                    for pairing_data in data.get('pairings', []):
                        pairing = self._deserialize_pairing(pairing_data)
                        if not pairing.is_expired():
                            self.pairings[pairing.id] = pairing
                            self.codes[pairing.code] = pairing
                logger.info(f"Loaded {len(self.pairings)} pairings from file")
            except Exception as e:
                logger.error(f"Failed to load pairings: {e}")

    def _deserialize_pairing(self, data: dict) -> PairingCode:
        """Deserialize pairing from dict"""
        initiator = DeviceDescriptor.model_validate(data["initiator"])
        pairing = PairingCode(data["code"], initiator, 3600)  # ttl not used
        pairing.id = data['id']
        pairing.status = data['status']
        pairing.peers = [DeviceDescriptor.model_validate(peer) for peer in data.get("peers", [])]
        pairing.created_at = datetime.fromisoformat(data['created_at'])
        pairing.connected_at = datetime.fromisoformat(data['connected_at']) if data.get('connected_at') else None
        pairing.expires_at = datetime.fromisoformat(data['expires_at'])
        pairing.terminated_at = datetime.fromisoformat(data['terminated_at']) if data.get('terminated_at') else None
        pairing.termination_reason = data.get('termination_reason')
        return pairing

    def _save_pairings(self):
        """Save pairings to file"""
        try:
            data = {
                'pairings': [p.to_dict() for p in self.pairings.values()]
            }
            with open(self.persistence_file, 'w') as f:
                json.dump(data, f, default=str)
        except Exception as e:
            logger.error(f"Failed to save pairings: {e}")

    async def create_pairing(self, device: DeviceDescriptor, client_key: str) -> PairingCode:
        if runtime_settings.maintenance_mode != "off":
            raise HTTPException(status_code=503, detail="Pairing creation is disabled during maintenance")

        await pairing_rate_limiter.check(f"create:{client_key}", runtime_settings.policy.pairing_rate_limit_per_minute)

        async with self.lock:
            code = await self._generate_unique_code()
            pairing = PairingCode(code, device, runtime_settings.policy.pairing_ttl_seconds)
            self.pairings[pairing.id] = pairing
            self.codes[code] = pairing
            self._save_pairings()
            return pairing

    async def _generate_unique_code(self) -> str:
        """Generate a unique pairing code"""
        while True:
            code = "".join(secrets.choice(PAIRING_CODE_ALPHABET) for _ in range(PAIRING_CODE_LENGTH))
            if code not in self.codes:
                return code

    async def join_pairing(self, code: str, device: DeviceDescriptor, client_key: str) -> PairingCode:
        should_broadcast = False
        if runtime_settings.maintenance_mode != "off":
            raise HTTPException(status_code=503, detail="Joining pairings is disabled during maintenance")

        await pairing_rate_limiter.check(f"join:{client_key}", runtime_settings.policy.pairing_rate_limit_per_minute)

        async with self.lock:
            normalized_code = code.strip().upper()
            pairing = self.codes.get(normalized_code)

            if not pairing:
                pairing = PairingCode(normalized_code, device, runtime_settings.policy.pairing_ttl_seconds)
                self.pairings[pairing.id] = pairing
                self.codes[normalized_code] = pairing
                self._save_pairings()
                return pairing

            if pairing.status == "terminated":
                raise HTTPException(status_code=410, detail="Pairing has been terminated")

            if pairing.is_expired():
                self.pause_pairing(pairing.id)
                pairing = PairingCode(normalized_code, device, runtime_settings.policy.pairing_ttl_seconds)
                self.pairings[pairing.id] = pairing
                self.codes[normalized_code] = pairing
                self._save_pairings()
                return pairing

            if pairing.initiator.identifier == device.identifier:
                return pairing

            existing_peer = next((peer for peer in pairing.peers if peer.identifier == device.identifier), None)
            if existing_peer is None and pairing.device_count >= runtime_settings.policy.max_devices_per_session:
                raise HTTPException(status_code=409, detail="Maximum devices per session reached")

            # Allow multiple devices to join - removed the "already used" check
            pairing.connect_peer(device)
            self._save_pairings()
            should_broadcast = True

        if should_broadcast:
            await self.broadcast_pairing_update(pairing)

        return pairing

    async def get_pairing(self, code: str) -> PairingCode:
        normalized_code = code.strip().upper()
        pairing = self.codes.get(normalized_code)

        if not pairing:
            raise HTTPException(status_code=404, detail="Pairing code not found")

        if pairing.status == "terminated":
            raise HTTPException(status_code=410, detail="Pairing has been terminated")

        if pairing.is_expired():
            self.pause_pairing(pairing.id)
            raise HTTPException(status_code=410, detail="Pairing code expired")

        return pairing

    async def get_pairing_by_id(self, pairing_id: str) -> PairingCode:
        pairing = self.pairings.get(pairing_id)
        if not pairing:
            raise HTTPException(status_code=404, detail="Pairing not found")
        if pairing.status == "terminated":
            raise HTTPException(status_code=410, detail="Pairing has been terminated")
        if pairing.is_expired():
            self.pause_pairing(pairing_id)
            raise HTTPException(status_code=410, detail="Pairing expired")
        return pairing

    async def remove_peer(self, pairing_id: str, peer_identifier: str) -> PairingCode:
        should_broadcast = False
        async with self.lock:
            pairing = self.pairings.get(pairing_id)
            if not pairing:
                raise HTTPException(status_code=404, detail="Pairing not found")

            removed = pairing.remove_peer(peer_identifier)
            if not removed:
                return pairing

            self._save_pairings()
            should_broadcast = True

        if should_broadcast:
            await self.broadcast_pairing_update(pairing)

        return pairing

    def pause_pairing(self, pairing_id: str) -> None:
        """Remove expired pairing"""
        pairing = self.pairings.pop(pairing_id, None)
        if pairing:
            self.codes.pop(pairing.code, None)

    async def terminate_pairing(self, pairing_id: str, reason: str) -> PairingCode:
        async with self.lock:
            pairing = self.pairings.get(pairing_id)
            if not pairing:
                raise HTTPException(status_code=404, detail="Pairing not found")

            pairing.terminate(reason)
            self._save_pairings()

        await self.broadcast_pairing_update(pairing)

        async with self.lock:
            self.pause_pairing(pairing_id)
            self._save_pairings()

        return pairing

    async def list_active_pairings(self) -> list[PairingCode]:
        async with self.lock:
            return [pairing for pairing in self.pairings.values() if pairing.status != "terminated" and not pairing.is_expired()]

    async def terminate_all_pairings(self, reason: str) -> list[PairingCode]:
        async with self.lock:
            pairings = [pairing for pairing in self.pairings.values() if pairing.status != "terminated"]

        terminated_pairings: list[PairingCode] = []
        for pairing in pairings:
            terminated_pairings.append(await self.terminate_pairing(pairing.id, reason))

        return terminated_pairings

    async def cleanup_expired(self) -> int:
        """Periodic cleanup of expired pairings"""
        async with self.lock:
            expired_ids = [pid for pid, p in self.pairings.items() if p.is_expired()]
            for pid in expired_ids:
                logger.info(f"Cleaning up expired pairing {pid}")
                self.pause_pairing(pid)
            if expired_ids:
                self._save_pairings()
            return len(expired_ids)

    async def subscribe_to_pairing_updates(self, pairing_id: str) -> asyncio.Queue:
        """Subscribe to SSE updates for a pairing"""
        async with self.lock:
            queue: asyncio.Queue = asyncio.Queue()
            if pairing_id not in self.sse_queues:
                self.sse_queues[pairing_id] = set()
            self.sse_queues[pairing_id].add(queue)
            return queue

    async def broadcast_pairing_update(self, pairing: PairingCode) -> None:
        """Broadcast pairing status update to all SSE subscribers"""
        update_data = {
            "type": "pairing_update",
            "data": pairing.to_dict()
        }
        payload = json.dumps(update_data)

        async with self.lock:
            queues = list(self.sse_queues.get(pairing.id, set()))

        for queue in queues:
            try:
                # Don't wait for subscribers to consume, just put in each queue
                await asyncio.wait_for(queue.put(payload), timeout=0.1)
            except asyncio.TimeoutError:
                # Queue is full, skip this subscriber update
                pass

    async def unsubscribe_from_pairing_updates(self, pairing_id: str, queue: asyncio.Queue) -> None:
        """Unsubscribe from SSE updates for a pairing"""
        async with self.lock:
            queues = self.sse_queues.get(pairing_id)
            if not queues or queue not in queues:
                return

            queues.remove(queue)
            if not queues:
                self.sse_queues.pop(pairing_id, None)


class ConnectionManager:
    """Manage WebRTC signaling and WebSocket connections between paired devices"""

    def __init__(self):
        self.signaling_messages: dict[str, list[dict]] = {}  # pairing_id -> list of signaling messages
        self.websocket_connections: dict[str, dict[str, WebSocket]] = {}  # pairing_id -> {device_id -> WebSocket}
        self.lock = asyncio.Lock()

    async def connect(self, pairing_id: str, device_id: str, websocket: WebSocket) -> None:
        """Register a WebSocket connection for a device in a pairing"""
        await websocket.accept()
        async with self.lock:
            if pairing_id not in self.websocket_connections:
                self.websocket_connections[pairing_id] = {}
            self.websocket_connections[pairing_id][device_id] = websocket
            logger.info(f"Connected device {device_id} to pairing {pairing_id}")

    async def disconnect(self, pairing_id: str, device_id: str) -> None:
        """Unregister a WebSocket connection for a device"""
        async with self.lock:
            if pairing_id in self.websocket_connections:
                self.websocket_connections[pairing_id].pop(device_id, None)
                # Clean up empty pairing entries
                if not self.websocket_connections[pairing_id]:
                    self.websocket_connections.pop(pairing_id, None)
            logger.info(f"Disconnected device {device_id} from pairing {pairing_id}")

    async def disconnect_pairing(self, pairing_id: str, reason: str = "admin_disconnect") -> int:
        async with self.lock:
            connections = self.websocket_connections.pop(pairing_id, {})

        for websocket in connections.values():
            try:
                await websocket.close(code=4001, reason=reason)
            except Exception:
                pass

        return len(connections)

    async def disconnect_device(self, pairing_id: str, device_id: str, reason: str = "admin_disconnect") -> bool:
        async with self.lock:
            websocket = self.websocket_connections.get(pairing_id, {}).pop(device_id, None)
            if self.websocket_connections.get(pairing_id) == {}:
                self.websocket_connections.pop(pairing_id, None)

        if websocket is None:
            return False

        try:
            await websocket.close(code=4001, reason=reason)
        except Exception:
            pass
        return True

    async def disconnect_all_pairings(self, reason: str = "admin_shutdown") -> int:
        async with self.lock:
            connections = self.websocket_connections
            self.websocket_connections = {}

        closed_count = 0
        for pairing_connections in connections.values():
            for websocket in pairing_connections.values():
                try:
                    await websocket.close(code=4001, reason=reason)
                    closed_count += 1
                except Exception:
                    pass

        return closed_count

    async def send_to_peer(self, pairing_id: str, sender_device_id: str, message: dict[str, Any]) -> bool:
        """Broadcast message to all peer devices (except sender)"""
        message_type = message.get("type")
        if message_type == "text" and not runtime_settings.feature_flags.messaging:
            return False
        if message_type == "text" and message.get("isCode") and not runtime_settings.feature_flags.code_sharing:
            return False
        if message_type in {"file_init", "file_chunk", "file_end", "file_shared"} and not runtime_settings.feature_flags.file_transfer:
            return False

        peer_device_ids = []
        websockets = []
        
        async with self.lock:
            if pairing_id not in self.websocket_connections:
                logger.warning(f"Pairing {pairing_id} has no active connections")
                return False
            
            # Get all devices in this pairing except the sender
            devices = self.websocket_connections[pairing_id]
            for device_id, websocket in devices.items():
                if device_id != sender_device_id:
                    peer_device_ids.append(device_id)
                    websockets.append((device_id, websocket))
            
            if not peer_device_ids:
                logger.warning(f"No peers found for device {sender_device_id} in pairing {pairing_id}")
                return False
        
        # Send to all peers outside of lock to avoid blocking
        success_count = 0
        failed_devices = []
        
        for device_id, websocket in websockets:
            try:
                await websocket.send_json(message)
                logger.info(f"Sent message to device {device_id} in pairing {pairing_id}")
                success_count += 1
            except Exception as e:
                logger.error(f"Failed to send message to peer {device_id}: {e}")
                failed_devices.append(device_id)
        
        # Remove failed websockets
        if failed_devices:
            async with self.lock:
                if pairing_id in self.websocket_connections:
                    for device_id in failed_devices:
                        self.websocket_connections[pairing_id].pop(device_id, None)
                    # Clean up empty pairing entries
                    if not self.websocket_connections[pairing_id]:
                        self.websocket_connections.pop(pairing_id, None)
        
        return success_count > 0

    async def store_signaling_message(self, pairing_id: str, message: dict[str, Any]) -> None:
        """Store a signaling message for the peer to retrieve"""
        async with self.lock:
            if pairing_id not in self.signaling_messages:
                self.signaling_messages[pairing_id] = []
            self.signaling_messages[pairing_id].append(message)
            logger.info(f"Stored signaling message for pairing {pairing_id}: {message['type']}")

    async def get_signaling_messages(
        self,
        pairing_id: str,
        device_id: str,
        sender_device_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get signaling messages targeted to a specific device in a pairing."""
        async with self.lock:
            messages = self.signaling_messages.get(pairing_id, [])
            matched_messages = [
                message for message in messages
                if (message.get("target_device_id") == device_id or message.get("target_device_id") == "*")
                and (sender_device_id is None or message.get("sender_device_id") == sender_device_id)
            ]
            # Keep only messages that were not consumed by this device yet.
            self.signaling_messages[pairing_id] = [
                message for message in messages
                if message not in matched_messages
            ]
            logger.info(
                f"Retrieved {len(matched_messages)} signaling messages for pairing {pairing_id} and device {device_id}"
            )
            return matched_messages


pairing_manager = PairingManager()
connection_manager = ConnectionManager()


def _request_client_key(request: Request) -> str:
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


async def _get_admin_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme)) -> AdminUserOut:
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=401, detail="Admin authentication required")

    try:
        return await admin_store.authenticate_token(credentials.credentials)
    except ValueError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error


def _require_admin_role(minimum_role: str):
    async def dependency(user: AdminUserOut = Depends(_get_admin_user)) -> AdminUserOut:
        role_order = {"viewer": 0, "operator": 1, "admin": 2, "owner": 3}
        if role_order.get(user.role, 0) < role_order.get(minimum_role, 0):
            raise HTTPException(status_code=403, detail="Insufficient admin role")
        return user

    return dependency


# ============================================================================
# REST API Endpoints
# ============================================================================


@app.get("/api/runtime-config", response_model=PublicConfigOut)
async def get_runtime_config() -> PublicConfigOut:
    global runtime_settings
    runtime_settings = await admin_store.get_runtime_settings()
    return runtime_settings


import urllib.request

@app.get("/api/credentials")
async def get_turn_credentials():
    try:
        req = urllib.request.Request("https://filesync.app/api/credentials", method="GET")
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode())
            return data
    except Exception as e:
        logger.error(f"Failed to fetch TURN credentials from filesync: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve TURN credentials")




@app.post("/api/admin/login", response_model=AdminLoginResponse)
async def admin_login(body: AdminLoginRequest, request: Request) -> AdminLoginResponse:
    await admin_rate_limiter.check(f"login:{_request_client_key(request)}", runtime_settings.policy.admin_rate_limit_per_minute)

    try:
        token, user, expires_at = await admin_store.authenticate(body)
    except ValueError as error:
        await admin_store.append_audit_log(
            actor=AdminUserOut(username=body.email, email=body.email, role="viewer"),
            action="login",
            target=body.email,
            status="denied",
            metadata={"error": str(error)},
        )
        raise HTTPException(status_code=401, detail="Invalid admin credentials") from error

    return AdminLoginResponse(
        token=token,
        user=user,
        expires_at=expires_at,
        settings=await admin_store.get_runtime_settings(),
    )


async def _resolve_admin_username(*, username: str | None = None, email: str | None = None) -> str | None:
    if username:
        return username

    if email:
        user = await admin_store.get_user_by_email(email)
        if user:
            return user.username

    return None


@app.post("/api/admin/logout")
async def admin_logout(user: AdminUserOut = Depends(_get_admin_user), credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme)) -> dict[str, str]:
    if credentials is not None and credentials.credentials:
        await admin_store.revoke_token(credentials.credentials)

    await admin_store.append_audit_log(actor=user, action="logout", target=user.username)
    return {"status": "ok"}


@app.get("/api/admin/dashboard", response_model=AdminDashboardOut)
async def admin_dashboard(user: AdminUserOut = Depends(_get_admin_user)) -> AdminDashboardOut:
    sessions = await pairing_manager.list_active_pairings()
    audit_log = await admin_store.list_audit_log(limit=25)
    await admin_store.append_audit_log(actor=user, action="view_dashboard", target="admin_dashboard")
    return AdminDashboardOut(
        current_user=user,
        settings=await admin_store.get_runtime_settings(),
        sessions=[pairing.to_dict() for pairing in sessions],
        audit_log=audit_log,
    )


@app.get("/api/admin/sessions", response_model=list[PairingCodeOut])
async def admin_sessions(user: AdminUserOut = Depends(_get_admin_user)) -> list[dict[str, Any]]:
    sessions = await pairing_manager.list_active_pairings()
    await admin_store.append_audit_log(actor=user, action="list_sessions", target="active_sessions")
    return [pairing.to_dict() for pairing in sessions]


@app.post("/api/admin/sessions/{pairing_id}/disconnect")
async def admin_disconnect_session(
    pairing_id: str,
    request: Request,
    user: AdminUserOut = Depends(_require_admin_role("operator")),
) -> dict[str, Any]:
    disconnected_count = await connection_manager.disconnect_pairing(pairing_id, reason="admin_disconnect")
    pairing = await pairing_manager.terminate_pairing(pairing_id, "admin_disconnect")
    await admin_store.append_audit_log(
        actor=user,
        action="disconnect_session",
        target=pairing_id,
        metadata={"device_count": pairing.device_count, "closed_connections": disconnected_count, "client": _request_client_key(request)},
    )
    return {"status": "ok", "pairing_id": pairing_id, "closed_connections": disconnected_count}


@app.post("/api/admin/sessions/{pairing_id}/devices/{device_id}/disconnect")
async def admin_disconnect_device(
    pairing_id: str,
    device_id: str,
    request: Request,
    user: AdminUserOut = Depends(_require_admin_role("operator")),
) -> dict[str, Any]:
    pairing = await pairing_manager.get_pairing_by_id(pairing_id)

    if device_id == pairing.initiator.identifier:
        disconnected = await connection_manager.disconnect_pairing(pairing_id, reason="admin_disconnect")
        pairing = await pairing_manager.terminate_pairing(pairing_id, "admin_disconnect")
    else:
        disconnected = await connection_manager.disconnect_device(pairing_id, device_id, reason="admin_disconnect")
        pairing = await pairing_manager.remove_peer(pairing_id, device_id)

    await admin_store.append_audit_log(
        actor=user,
        action="disconnect_device",
        target=f"{pairing_id}:{device_id}",
        metadata={"disconnected": disconnected, "remaining_devices": pairing.device_count, "client": _request_client_key(request)},
    )
    return {"status": "ok", "pairing_id": pairing_id, "device_id": device_id, "disconnected": disconnected}


@app.get("/api/admin/audit-log", response_model=list[AuditLogEntry])
async def admin_audit_log(user: AdminUserOut = Depends(_get_admin_user)) -> list[AuditLogEntry]:
    await admin_store.append_audit_log(actor=user, action="view_audit_log", target="audit_log")
    return await admin_store.list_audit_log(limit=100)


@app.put("/api/admin/settings", response_model=PublicConfigOut)
async def update_admin_settings(
    body: AdminSettingsUpdate,
    user: AdminUserOut = Depends(_require_admin_role("admin")),
) -> PublicConfigOut:
    global runtime_settings

    new_settings = RuntimeSettings.model_validate(body.model_dump(mode="python"))
    previous_settings = runtime_settings
    runtime_settings = await admin_store.update_runtime_settings(user, new_settings, metadata={"source": "admin_settings_update"})

    if previous_settings.maintenance_mode != "shutdown" and runtime_settings.maintenance_mode == "shutdown":
        active_pairings = await pairing_manager.list_active_pairings()
        for pairing in active_pairings:
            await connection_manager.disconnect_pairing(pairing.id, reason="maintenance_shutdown")
        await pairing_manager.terminate_all_pairings("maintenance_shutdown")

    await admin_store.append_audit_log(
        actor=user,
        action="update_settings",
        target="runtime_settings",
        before=previous_settings.model_dump(mode="python"),
        after=runtime_settings.model_dump(mode="python"),
        metadata={"client": user.username},
    )

    return runtime_settings


@app.post("/api/pairing/initiate", response_model=PairingCodeOut)
async def initiate_pairing(body: PairingCodeCreate, request: Request) -> dict[str, Any]:
    """Device A initiates a pairing and gets a code"""
    pairing = await pairing_manager.create_pairing(body.device, _request_client_key(request))
    logger.info(f"Pairing initiated: {pairing.code} by {body.device.identifier}, ID: {pairing.id}")
    return pairing.to_dict()


@app.post("/api/pairing/join/{code}", response_model=PairingCodeOut)
async def join_pairing(code: str, body: PairingCodeJoin, request: Request) -> dict[str, Any]:
    """Device B joins a pairing using the code"""
    logger.info(f"Join request for code: {code}")
    pairing = await pairing_manager.join_pairing(code, body.device, _request_client_key(request))
    logger.info(
        f"Pairing joined: {code} by {body.device.identifier}. "
        f"Pairing ID: {pairing.id}, Status: {pairing.status}")
    return pairing.to_dict()


@app.post("/api/pairing/{pairing_id}/leave/{device_id}", response_model=PairingCodeOut)
async def leave_pairing(pairing_id: str, device_id: str) -> dict[str, Any]:
    """Remove a device from a pairing when it disconnects."""
    pairing = await pairing_manager.remove_peer(pairing_id, device_id)
    logger.info(f"Device {device_id} left pairing {pairing_id}")
    return pairing.to_dict()


@app.get("/api/pairing/{code}", response_model=PairingCodeOut)
async def get_pairing_status(code: str) -> dict[str, Any]:
    """Check pairing status"""
    pairing = await pairing_manager.get_pairing(code)
    return pairing.to_dict()


@app.get("/api/pairing/{code}/events")
async def pairing_events(code: str):
    """Server-Sent Events endpoint for real-time pairing updates"""
    pairing = await pairing_manager.get_pairing(code)

    async def event_generator():
        queue = await pairing_manager.subscribe_to_pairing_updates(pairing.id)
        try:
            # Send initial state
            initial_data = {
                "type": "pairing_update",
                "data": pairing.to_dict()
            }
            yield f"data: {json.dumps(initial_data)}\n\n"

            while True:
                try:
                    # Wait for updates with timeout
                    update_json = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield f"data: {update_json}\n\n"
                except asyncio.TimeoutError:
                    # Send heartbeat to keep connection alive
                    yield ": heartbeat\n\n"
        except asyncio.CancelledError:
            logger.info(f"SSE client disconnected for pairing {pairing.id}")
        except GeneratorExit:
            logger.info(f"SSE generator closed for pairing {pairing.id}")
        except Exception as e:
            logger.error(f"SSE error for pairing {pairing.id}: {e}")
        finally:
            await pairing_manager.unsubscribe_from_pairing_updates(pairing.id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Cache-Control",
        }
    )


@app.get("/api/pairing/{code}/qrcode")
async def generate_qrcode(code: str, request: Request) -> dict[str, Any]:
    """Generate QR code for pairing code"""
    # Verify pairing code exists
    pairing = await pairing_manager.get_pairing(code)

    join_url = _build_pairing_join_url(request, pairing.code)
    
    # Generate QR code
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    
    # Encode the full join URL so a scan opens the app directly.
    qr.add_data(join_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    
    # Convert to base64 data URL
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    img_base64 = base64.b64encode(buffer.getvalue()).decode()
    data_url = f"data:image/png;base64,{img_base64}"
    
    logger.info(f"QR code generated for pairing code: {code}")
    return {"code": code, "url": join_url, "qrcode": data_url}


# ============================================================================
# WebRTC Signaling Endpoints
# ============================================================================

from .schemas import SignalingMessage

@app.post("/api/pairing/{pairing_id}/signaling")
async def send_signaling_message(pairing_id: str, message: SignalingMessage) -> dict[str, Any]:
    """Send a WebRTC signaling message (offer, answer, or ICE candidate)"""
    pairing = await pairing_manager.get_pairing_by_id(pairing_id)

    await connection_manager.store_signaling_message(pairing_id, message.model_dump())
    logger.info(f"Signaling message stored for pairing {pairing_id}: {message.type}")
    return {"status": "stored"}


@app.get("/api/pairing/{pairing_id}/signaling")
async def get_signaling_messages(pairing_id: str, device_id: str, sender_device_id: str | None = None) -> list[dict[str, Any]]:
    """Get pending WebRTC signaling messages"""
    pairing = await pairing_manager.get_pairing_by_id(pairing_id)

    messages = await connection_manager.get_signaling_messages(pairing_id, device_id, sender_device_id)
    return messages


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

        if runtime_settings.maintenance_mode == "shutdown":
            await ws.close(code=4001, reason="Maintenance mode shutdown")
            return
        
        # Allow connections for pending, active, and connected states (for multi-device scenarios)
        if pairing.status not in ["pending", "active", "connected"]:
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
            if msg_type in ["text", "file_init", "file_chunk", "file_end", "file_shared", "snippet", "transfer_approval_request", "transfer_approval_response", "transfer_approval_cancel"]:
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
# Forgot Password Endpoints
# ============================================================================


@app.post("/api/admin/forgot-password")
async def admin_forgot_password(
    body: AdminForgotPasswordRequest,
    request: Request,
) -> dict[str, str]:
    """Request a password reset OTP"""
    await admin_rate_limiter.check(f"forgot_password:{_request_client_key(request)}", runtime_settings.policy.admin_rate_limit_per_minute)

    username = await _resolve_admin_username(username=body.username, email=body.email)
    if not username:
        logger.info(f"Forgot password request for identifier: {body.email or body.username or 'unknown'}")
        return {"message": "If the user exists, an OTP has been sent"}

    user = await admin_store.get_user(username)
    if not user:
        logger.info(f"Forgot password request for user: {username}")
        return {"message": "If the user exists, an OTP has been sent"}

    otp = await admin_store.generate_otp(username)
    await admin_store.send_forgot_password_email(username, otp)
    
    await admin_store.append_audit_log(
        actor=AdminUserOut(username=username, email=user.email, role="viewer"),
        action="forgot_password",
        target=username,
        status="success",
        metadata={"source": "admin_forgot_password", "client": _request_client_key(request)},
    )
    
    return {"message": "If the user exists, an OTP has been sent"}


@app.post("/api/admin/verify-otp")
async def admin_verify_otp(
    body: AdminForgotPasswordVerifyRequest,
    request: Request,
) -> dict[str, str]:
    """Verify the OTP for password reset"""
    await admin_rate_limiter.check(f"verify_otp:{_request_client_key(request)}", runtime_settings.policy.admin_rate_limit_per_minute)

    username = await _resolve_admin_username(username=body.username, email=body.email)
    if not username:
        await admin_store.append_audit_log(
            actor=AdminUserOut(username=body.email or body.username or "unknown", email=body.email, role="viewer"),
            action="verify_otp",
            target=body.email or body.username,
            status="denied",
            metadata={"source": "admin_verify_otp", "client": _request_client_key(request)},
        )
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")

    is_valid = await admin_store.verify_otp(username, body.otp, consume=False)
    
    if not is_valid:
        await admin_store.append_audit_log(
            actor=AdminUserOut(username=username, email=body.email, role="viewer"),
            action="verify_otp",
            target=body.email or username,
            status="denied",
            metadata={"source": "admin_verify_otp", "client": _request_client_key(request)},
        )
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")
    
    await admin_store.append_audit_log(
        actor=AdminUserOut(username=username, email=body.email, role="viewer"),
        action="verify_otp",
        target=body.email or username,
        status="success",
        metadata={"source": "admin_verify_otp", "client": _request_client_key(request)},
    )
    
    return {"message": "OTP verified successfully"}


@app.post("/api/admin/reset-password")
async def admin_reset_password(
    body: AdminResetPasswordRequest,
    request: Request,
) -> dict[str, str]:
    """Reset password after OTP verification"""
    await admin_rate_limiter.check(f"reset_password:{_request_client_key(request)}", runtime_settings.policy.admin_rate_limit_per_minute)

    username = await _resolve_admin_username(username=body.username, email=body.email)
    if not username:
        await admin_store.append_audit_log(
            actor=AdminUserOut(username=body.email or body.username or "unknown", email=body.email, role="viewer"),
            action="reset_password",
            target=body.email or body.username,
            status="denied",
            metadata={"source": "admin_reset_password", "client": _request_client_key(request)},
        )
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")

    # Consume OTP only when the password reset is finalized.
    is_valid = await admin_store.verify_otp(username, body.otp, consume=True)
    
    if not is_valid:
        await admin_store.append_audit_log(
            actor=AdminUserOut(username=username, email=body.email, role="viewer"),
            action="reset_password",
            target=body.email or username,
            status="denied",
            metadata={"source": "admin_reset_password", "client": _request_client_key(request)},
        )
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")
    
    # Reset password
    success = await admin_store.reset_password(username, body.new_password)
    
    if not success:
        await admin_store.append_audit_log(
            actor=AdminUserOut(username=username, email=body.email, role="viewer"),
            action="reset_password",
            target=body.email or username,
            status="denied",
            metadata={"source": "admin_reset_password", "client": _request_client_key(request)},
        )
        raise HTTPException(status_code=404, detail="User not found")
    
    await admin_store.append_audit_log(
        actor=AdminUserOut(username=username, email=body.email, role="viewer"),
        action="reset_password",
        target=body.email or username,
        status="success",
        metadata={"source": "admin_reset_password", "client": _request_client_key(request)},
    )
    
    return {"message": "Password reset successfully"}


# ============================================================================
# Admin User Management Endpoints
# ============================================================================


@app.get("/api/admin/users", response_model=list[AdminUserOut])
async def list_admin_users(
    user: AdminUserOut = Depends(_get_admin_user),
) -> list[AdminUserOut]:
    """List all admin users (only owner can access)"""
    if user.role != "owner":
        raise HTTPException(status_code=403, detail="Insufficient permissions. Only owner can list users.")
    
    users = await admin_store.list_users()
    return [admin_store._user_to_public(user) for user in users]


@app.post("/api/admin/users", response_model=AdminUserOut)
async def create_admin_user(
    body: AdminUserCreate,
    request: Request,
    user: AdminUserOut = Depends(_get_admin_user),
) -> AdminUserOut:
    """Create a new admin user (only owner can do this)"""
    if user.role != "owner":
        raise HTTPException(status_code=403, detail="Insufficient permissions. Only owner can create users.")
    
    await admin_rate_limiter.check(f"create_user:{_request_client_key(request)}", runtime_settings.policy.admin_rate_limit_per_minute)
    
    try:
        new_user = await admin_store.create_user(
            username=body.username,
            email=body.email,
            password=body.password,
            role=body.role,
            display_name=body.display_name,
        )
        
        await admin_store.append_audit_log(
            actor=user,
            action="create_user",
            target=body.username,
            status="success",
            metadata={
                "email": body.email,
                "role": body.role,
                "display_name": body.display_name,
                "client": _request_client_key(request),
            },
        )
        
        return admin_store._user_to_public(new_user)
    except ValueError as e:
        await admin_store.append_audit_log(
            actor=user,
            action="create_user",
            target=body.username,
            status="denied",
            metadata={"error": str(e), "client": _request_client_key(request)},
        )
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/api/admin/users/{username}", response_model=AdminUserOut)
async def update_admin_user(
    username: str,
    body: AdminUserUpdate,
    request: Request,
    user: AdminUserOut = Depends(_get_admin_user),
) -> AdminUserOut:
    """Update an admin user (only owner can do this)"""
    if user.role != "owner":
        raise HTTPException(status_code=403, detail="Insufficient permissions. Only owner can update users.")
    
    await admin_rate_limiter.check(f"update_user:{_request_client_key(request)}", runtime_settings.policy.admin_rate_limit_per_minute)
    
    updated_user = await admin_store.update_user(
        username=username,
        email=body.email,
        role=body.role,
        display_name=body.display_name,
    )
    
    if not updated_user:
        await admin_store.append_audit_log(
            actor=user,
            action="update_user",
            target=username,
            status="denied",
            metadata={"error": "User not found", "client": _request_client_key(request)},
        )
        raise HTTPException(status_code=404, detail="User not found")
    
    await admin_store.append_audit_log(
        actor=user,
        action="update_user",
        target=username,
        status="success",
        metadata={
            "email": body.email,
            "role": body.role,
            "display_name": body.display_name,
            "client": _request_client_key(request),
        },
    )
    
    return admin_store._user_to_public(updated_user)


@app.delete("/api/admin/users/{username}")
async def delete_admin_user(
    username: str,
    request: Request,
    user: AdminUserOut = Depends(_get_admin_user),
) -> dict[str, str]:
    """Delete an admin user (only owner can do this)"""
    if user.role != "owner":
        raise HTTPException(status_code=403, detail="Insufficient permissions. Only owner can delete users.")
    
    await admin_rate_limiter.check(f"delete_user:{_request_client_key(request)}", runtime_settings.policy.admin_rate_limit_per_minute)
    
    try:
        success = await admin_store.delete_user(username)
    except ValueError as e:
        await admin_store.append_audit_log(
            actor=user,
            action="delete_user",
            target=username,
            status="denied",
            metadata={"error": str(e), "client": _request_client_key(request)},
        )
        raise HTTPException(status_code=400, detail=str(e))
    
    if not success:
        await admin_store.append_audit_log(
            actor=user,
            action="delete_user",
            target=username,
            status="denied",
            metadata={"error": "User not found", "client": _request_client_key(request)},
        )
        raise HTTPException(status_code=404, detail="User not found")
    
    await admin_store.append_audit_log(
        actor=user,
        action="delete_user",
        target=username,
        status="success",
        metadata={"client": _request_client_key(request)},
    )
    
    return {"message": f"User '{username}' deleted successfully"}


# ============================================================================
# Cleanup Task
# ============================================================================


@app.on_event("startup")
async def startup_cleanup():
    """Start periodic cleanup of expired pairings"""
    global runtime_settings

    await admin_store.initialize()
    runtime_settings = await admin_store.get_runtime_settings()

    if runtime_settings.maintenance_mode == "shutdown":
        await connection_manager.disconnect_all_pairings("maintenance_shutdown")
        await pairing_manager.terminate_all_pairings("maintenance_shutdown")

    async def cleanup_task():
        while True:
            await asyncio.sleep(60)  # Run every minute
            logger.info("Running pairing cleanup...")
            expired_count = await pairing_manager.cleanup_expired()
            if expired_count > 0:
                logger.info(f"Cleaned up {expired_count} expired pairings")

    asyncio.create_task(cleanup_task())

