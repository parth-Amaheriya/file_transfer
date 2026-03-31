from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class DeviceDescriptor(BaseModel):
    identifier: str = Field(..., max_length=120)  # Device ID (UUID)
    label: Optional[str] = Field(None, max_length=120)  # Device name
    metadata: dict[str, Any] | None = None


class PairingCodeCreate(BaseModel):
    device: DeviceDescriptor


class PairingCodeJoin(BaseModel):
    device: DeviceDescriptor
    code: str


class PairingCodeOut(BaseModel):
    id: str
    code: str
    status: Literal["pending", "connected", "expired"] = "pending"
    initiator: DeviceDescriptor
    peer: Optional[DeviceDescriptor] = None
    created_at: datetime
    connected_at: Optional[datetime] = None
    expires_at: datetime


class FileTransferMessage(BaseModel):
    type: Literal["file_init", "file_chunk", "file_end"]
    file_name: str
    file_size: Optional[int] = None
    chunk_data: Optional[str] = None  # base64 encoded
    chunk_size: Optional[int] = None
    mime_type: Optional[str] = None


class TextMessage(BaseModel):
    type: Literal["text"]
    content: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
