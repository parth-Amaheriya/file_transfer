from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


MaintenanceMode = Literal["off", "block_new", "shutdown"]
AdminRole = Literal["viewer", "operator", "admin", "owner"]


class FeatureFlags(BaseModel):
    file_transfer: bool = True
    messaging: bool = True
    code_sharing: bool = True
    emoji_support: bool = True
    mentions: bool = True


class PolicySettings(BaseModel):
    max_file_size_bytes: int = Field(default=25 * 1024 * 1024, ge=1)
    pairing_ttl_seconds: int = Field(default=3600, ge=60)
    max_devices_per_session: int = Field(default=4, ge=1)
    pairing_rate_limit_per_minute: int = Field(default=30, ge=1)
    admin_rate_limit_per_minute: int = Field(default=60, ge=1)


class RuntimeSettings(BaseModel):
    maintenance_mode: MaintenanceMode = "off"
    feature_flags: FeatureFlags = Field(default_factory=FeatureFlags)
    policy: PolicySettings = Field(default_factory=PolicySettings)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class AdminStoredUser(BaseModel):
    username: str = Field(..., min_length=1, max_length=80)
    role: AdminRole = "viewer"
    password_hash: str = Field(..., min_length=16)
    display_name: Optional[str] = Field(None, max_length=120)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_login_at: Optional[datetime] = None


class AdminUserOut(BaseModel):
    username: str
    role: AdminRole
    display_name: Optional[str] = None
    last_login_at: Optional[datetime] = None


class AdminLoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=80)
    password: str = Field(..., min_length=1, max_length=200)


class AdminLoginResponse(BaseModel):
    token: str
    token_type: Literal["bearer"] = "bearer"
    user: AdminUserOut
    expires_at: datetime
    settings: RuntimeSettings


class AuditLogEntry(BaseModel):
    id: str
    timestamp: datetime
    actor: str
    role: AdminRole
    action: str
    target: Optional[str] = None
    status: Literal["success", "blocked", "denied"] = "success"
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AdminState(RuntimeSettings):
    users: list[AdminStoredUser] = Field(default_factory=list)
    audit_log: list[AuditLogEntry] = Field(default_factory=list)


class AdminDashboardOut(BaseModel):
    current_user: AdminUserOut
    settings: RuntimeSettings
    sessions: list["PairingCodeOut"] = Field(default_factory=list)
    audit_log: list[AuditLogEntry] = Field(default_factory=list)


class AdminSettingsUpdate(BaseModel):
    maintenance_mode: MaintenanceMode = "off"
    feature_flags: FeatureFlags = Field(default_factory=FeatureFlags)
    policy: PolicySettings = Field(default_factory=PolicySettings)


class PublicConfigOut(RuntimeSettings):
    pass


class DeviceDescriptor(BaseModel):
    identifier: str = Field(..., max_length=120)  # Device ID (UUID)
    label: Optional[str] = Field(None, max_length=120)  # Device name
    metadata: dict[str, Any] | None = None


class PairingCodeCreate(BaseModel):
    device: DeviceDescriptor


class PairingCodeJoin(BaseModel):
    device: DeviceDescriptor
    # Note: code is passed in the URL path, not in the request body


class PairingCodeOut(BaseModel):
    id: str
    code: str
    status: Literal["pending", "connected", "expired", "terminated"] = "pending"
    initiator: DeviceDescriptor
    peers: list[DeviceDescriptor] = Field(default_factory=list)
    peer_count: int = 0
    device_count: int = 1
    created_at: datetime
    connected_at: Optional[datetime] = None
    expires_at: datetime
    terminated_at: Optional[datetime] = None
    termination_reason: Optional[str] = None


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


class SignalingMessage(BaseModel):
    type: Literal["offer", "answer", "ice_candidate"]
    data: dict[str, Any]  # SDP or ICE candidate data
    sender_device_id: str
    target_device_id: str
