from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class SessionCreate(BaseModel):
    label: Optional[str] = Field(None, max_length=80)


class SessionOut(BaseModel):
    id: str
    label: Optional[str]
    created_at: datetime


class LogEntry(BaseModel):
    id: str
    session_id: str
    kind: Literal["info", "warning", "error"] = "info"
    message: str
    payload: dict | None = None
    created_at: datetime


class FileMeta(BaseModel):
    name: str
    size: int
    mime_type: Optional[str]


class DeviceDescriptor(BaseModel):
    identifier: str = Field(..., max_length=120)
    label: Optional[str] = Field(None, max_length=120)
    metadata: dict[str, Any] | None = None


class PairingKeyCreate(BaseModel):
    device: DeviceDescriptor


class PairingKeyJoin(BaseModel):
    device: DeviceDescriptor


class PairingKeyOut(BaseModel):
    id: str
    code: str
    status: Literal["pending", "connected", "expired"] = "pending"
    initiator: DeviceDescriptor
    peer: Optional[DeviceDescriptor] = None
    created_at: datetime
    connected_at: Optional[datetime] = None
