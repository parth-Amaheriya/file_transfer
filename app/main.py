from __future__ import annotations

import asyncio
import errno
import logging
import secrets
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiofiles
from fastapi import (FastAPI, File, Form, HTTPException, Request, UploadFile,
                     WebSocket, WebSocketDisconnect)
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pymongo import ReturnDocument

from .config import get_settings
from .db import get_collection
from .schemas import (DeviceDescriptor, FileMeta, LogEntry, PairingKeyCreate,
                      PairingKeyJoin, PairingKeyOut, SessionCreate, SessionOut)

settings = get_settings()
app = FastAPI(title="Transfer Hub", version="0.1.0")

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
static_dir = APP_DIR / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")

logger = logging.getLogger("transfer_hub")


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

_sessions = get_collection("sessions")
_logs = get_collection("logs")
_pairings = get_collection("pairings")

PAIRING_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PAIRING_CODE_LENGTH = 6


class ConnectionManager:
    def __init__(self) -> None:
        self._rooms: dict[str, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, session_id: str, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._rooms.setdefault(session_id, set()).add(ws)

    async def disconnect(self, session_id: str, ws: WebSocket) -> None:
        async with self._lock:
            sockets = self._rooms.get(session_id)
            if not sockets:
                return
            sockets.discard(ws)
            if not sockets:
                self._rooms.pop(session_id, None)

    async def broadcast(self, session_id: str, payload: dict[str, Any]) -> None:
        sockets = list(self._rooms.get(session_id, set()))
        for ws in sockets:
            try:
                await ws.send_json(payload)
            except RuntimeError:
                await self.disconnect(session_id, ws)


manager = ConnectionManager()


async def _generate_pairing_code() -> str:
    while True:
        code = "".join(secrets.choice(PAIRING_CODE_ALPHABET) for _ in range(PAIRING_CODE_LENGTH))
        exists = await _pairings.count_documents({"code": code}, limit=1)
        if not exists:
            return code


def normalize_pairing_code(code: str) -> str:
    return code.strip().upper()


def serialize_pairing(doc: dict[str, Any]) -> PairingKeyOut:
    initiator = DeviceDescriptor.model_validate(doc["initiator"])
    peer_raw = doc.get("peer")
    peer = DeviceDescriptor.model_validate(peer_raw) if peer_raw else None
    return PairingKeyOut(
        id=doc["_id"],
        code=doc["code"],
        status=doc.get("status", "pending"),
        initiator=initiator,
        peer=peer,
        created_at=doc["created_at"],
        connected_at=doc.get("connected_at"),
    )


async def insert_pairing(device: DeviceDescriptor) -> dict[str, Any]:
    doc = {
        "_id": uuid4().hex,
        "code": await _generate_pairing_code(),
        "status": "pending",
        "initiator": device.model_dump(),
        "peer": None,
        "created_at": datetime.utcnow(),
        "connected_at": None,
    }
    await _pairings.insert_one(doc)
    return doc


async def connect_pairing(code: str, device: DeviceDescriptor) -> dict[str, Any]:
    normalized_code = normalize_pairing_code(code)
    doc = await _pairings.find_one_and_update(
        {"code": normalized_code, "status": "pending"},
        {
            "$set": {
                "peer": device.model_dump(),
                "status": "connected",
                "connected_at": datetime.utcnow(),
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if doc:
        return doc

    existing = await _pairings.find_one({"code": normalized_code})
    if not existing:
        raise HTTPException(status_code=404, detail="Pairing key not found")
    if existing.get("status") == "connected":
        raise HTTPException(status_code=409, detail="Pairing key already used")
    raise HTTPException(status_code=410, detail="Pairing key expired or invalid")


async def ensure_session(session_id: str) -> dict[str, Any]:
    session = await _sessions.find_one({"_id": session_id})
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


def serialize_session(doc: dict[str, Any]) -> SessionOut:
    return SessionOut(id=doc["_id"], label=doc.get("label"), created_at=doc["created_at"])


async def append_log(session_id: str, kind: str, message: str, payload: dict | None = None) -> LogEntry:
    entry_id = uuid4().hex
    doc = {
        "_id": entry_id,
        "session_id": session_id,
        "kind": kind,
        "message": message,
        "payload": payload,
        "created_at": datetime.utcnow(),
    }
    await _logs.insert_one(doc)
    log_entry = LogEntry(
        id=entry_id,
        session_id=session_id,
        kind=kind,  # type: ignore[arg-type]
        message=message,
        payload=payload,
        created_at=doc["created_at"],
    )
    await manager.broadcast(session_id, {"type": "log", "data": log_entry.model_dump()})
    return log_entry


async def insert_session(label: str | None) -> dict[str, Any]:
    session_id = uuid4().hex
    doc = {
        "_id": session_id,
        "label": label,
        "created_at": datetime.utcnow(),
    }
    await _sessions.insert_one(doc)
    await append_log(session_id, "info", "Session initialized", {"label": label})
    return doc


async def load_logs(session_id: str) -> list[LogEntry]:
    await ensure_session(session_id)
    cursor = _logs.find({"session_id": session_id}).sort("created_at", 1)
    results: list[LogEntry] = []
    async for doc in cursor:
        results.append(
            LogEntry(
                id=doc["_id"],
                session_id=session_id,
                kind=doc.get("kind", "info"),  # type: ignore[arg-type]
                message=doc.get("message", ""),
                payload=doc.get("payload"),
                created_at=doc["created_at"],
            )
        )
    return results


async def save_uploaded_file(session_id: str, file: UploadFile) -> FileMeta:
    await ensure_session(session_id)
    uploads_dir = uploads_root / session_id
    uploads_dir.mkdir(parents=True, exist_ok=True)
    target_path = uploads_dir / file.filename
    size = 0
    async with aiofiles.open(target_path, "wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            await out.write(chunk)
    meta = FileMeta(name=file.filename, size=size, mime_type=file.content_type)
    await append_log(session_id, "info", "File stored", meta.model_dump())
    return meta


def _format_size(num: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(num)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PB"


def list_session_files(session_id: str) -> list[dict[str, Any]]:
    directory = uploads_root / session_id
    if not directory.exists():
        return []
    files = []
    for path in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
        if path.is_file():
            size = path.stat().st_size
            files.append({"name": path.name, "size": size, "display_size": _format_size(size)})
    return files


@app.post("/api/sessions", response_model=SessionOut)
async def create_session(body: SessionCreate) -> SessionOut:
    doc = await insert_session(body.label)
    return serialize_session(doc)


@app.get("/api/sessions", response_model=list[SessionOut])
async def list_sessions() -> list[SessionOut]:
    cursor = _sessions.find().sort("created_at", -1)
    return [serialize_session(doc) async for doc in cursor]


@app.get("/api/sessions/{session_id}", response_model=SessionOut)
async def get_session(session_id: str) -> SessionOut:
    doc = await ensure_session(session_id)
    return serialize_session(doc)


@app.get("/api/sessions/{session_id}/logs", response_model=list[LogEntry])
async def fetch_logs(session_id: str) -> list[LogEntry]:
    return await load_logs(session_id)


@app.post("/api/pairings", response_model=PairingKeyOut)
async def create_pairing_key(body: PairingKeyCreate) -> PairingKeyOut:
    doc = await insert_pairing(body.device)
    return serialize_pairing(doc)


@app.post("/api/pairings/{code}/join", response_model=PairingKeyOut)
async def join_pairing_key(code: str, body: PairingKeyJoin) -> PairingKeyOut:
    doc = await connect_pairing(code, body.device)
    return serialize_pairing(doc)


@app.get("/api/pairings/{code}", response_model=PairingKeyOut)
async def get_pairing_info(code: str) -> PairingKeyOut:
    normalized_code = normalize_pairing_code(code)
    doc = await _pairings.find_one({"code": normalized_code})
    if not doc:
        raise HTTPException(status_code=404, detail="Pairing key not found")
    return serialize_pairing(doc)


@app.post("/api/sessions/{session_id}/files")
async def upload_file(session_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    meta = await save_uploaded_file(session_id, file)
    return {"status": "stored", "file": meta.model_dump()}


@app.get("/api/sessions/{session_id}/files/{filename}")
async def download_file(session_id: str, filename: str):
    await ensure_session(session_id)
    path = uploads_root / session_id / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    async def iterfile():
        async with aiofiles.open(path, "rb") as stream:
            while chunk := await stream.read(1024 * 512):
                yield chunk

    await append_log(session_id, "info", "File downloaded", {"name": filename})
    return StreamingResponse(iterfile(), media_type="application/octet-stream")


@app.get("/")
async def dashboard(request: Request):
    cursor = _sessions.find().sort("created_at", -1)
    sessions = [serialize_session(doc) async for doc in cursor]
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "sessions": sessions,
        },
    )


@app.post("/ui/sessions")
async def create_session_from_form(label: str = Form("")):
    doc = await insert_session(label.strip() or None)
    return RedirectResponse(url=f"/sessions/{doc['_id']}", status_code=303)


@app.get("/sessions/{session_id}")
async def session_details(request: Request, session_id: str):
    doc = await ensure_session(session_id)
    session = serialize_session(doc)
    logs = await load_logs(session_id)
    files = list_session_files(session_id)
    return templates.TemplateResponse(
        "session_detail.html",
        {
            "request": request,
            "session": session,
            "logs": logs,
            "files": files,
        },
    )


@app.post("/sessions/{session_id}/upload")
async def upload_from_form(session_id: str, file: UploadFile = File(...)):
    await save_uploaded_file(session_id, file)
    return RedirectResponse(url=f"/sessions/{session_id}", status_code=303)


@app.websocket("/ws/logs/{session_id}")
async def log_socket(session_id: str, ws: WebSocket):
    await ensure_session(session_id)
    await manager.connect(session_id, ws)
    try:
        while True:
            await ws.receive_text()  # keep the socket alive even if client is passive
    except WebSocketDisconnect:
        await manager.disconnect(session_id, ws)
