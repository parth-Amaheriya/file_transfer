from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta
from typing import Any, Optional

try:
    from motor.motor_asyncio import AsyncIOMotorClient
except Exception:  # pragma: no cover - optional dependency for local/dev fallback
    AsyncIOMotorClient = None

from .config import Settings
from .schemas import (
    AdminLoginRequest,
    AdminRole,
    AdminState,
    AdminStoredUser,
    AdminUserOut,
    AuditLogEntry,
    RuntimeSettings,
)

logger = logging.getLogger("p2p_transfer")

ROLE_ORDER: dict[str, int] = {
    "viewer": 0,
    "operator": 1,
    "admin": 2,
    "owner": 3,
}


class AdminStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = asyncio.Lock()
        self._runtime_settings = RuntimeSettings()
        self._memory_users: dict[str, AdminStoredUser] = {}
        self._memory_sessions: dict[str, dict[str, Any]] = {}
        self._memory_audit_log: list[AuditLogEntry] = []
        self._mongo_enabled = False
        self._mongo_client: Any = None
        self._db: Any = None

        if settings.mongo_url and AsyncIOMotorClient is not None:
            try:
                self._mongo_client = AsyncIOMotorClient(settings.mongo_url, serverSelectionTimeoutMS=1500)
                self._db = self._mongo_client[settings.mongo_db_name]
                self._mongo_enabled = True
            except Exception as error:  # pragma: no cover - connection bootstrap path
                logger.warning("MongoDB connection could not be initialized, using in-memory admin state: %s", error)
                self._mongo_client = None
                self._db = None
                self._mongo_enabled = False

    @staticmethod
    def _role_rank(role: str) -> int:
        return ROLE_ORDER.get(role, 0)

    @staticmethod
    def _now() -> datetime:
        return datetime.utcnow()

    @staticmethod
    def _hash_password(password: str, salt: Optional[str] = None) -> str:
        salt_value = salt or base64.urlsafe_b64encode(secrets.token_bytes(16)).decode().rstrip("=")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_value.encode("utf-8"), 120_000)
        encoded = base64.urlsafe_b64encode(digest).decode().rstrip("=")
        return f"pbkdf2_sha256$120000${salt_value}${encoded}"

    @staticmethod
    def _verify_password(password: str, stored_hash: str) -> bool:
        try:
            algorithm, rounds, salt, digest = stored_hash.split("$", 3)
            if algorithm != "pbkdf2_sha256":
                return False
            expected = AdminStore._hash_password(password, salt)
            return hmac.compare_digest(expected, stored_hash)
        except Exception:
            return False

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _default_users(settings: Settings) -> list[AdminStoredUser]:
        return [
            AdminStoredUser(
                username=settings.admin_owner_username,
                role="owner",
                password_hash=AdminStore._hash_password(settings.admin_owner_password),
                display_name="Owner",
            ),
            AdminStoredUser(
                username=settings.admin_admin_username,
                role="admin",
                password_hash=AdminStore._hash_password(settings.admin_admin_password),
                display_name="Administrator",
            ),
            AdminStoredUser(
                username=settings.admin_operator_username,
                role="operator",
                password_hash=AdminStore._hash_password(settings.admin_operator_password),
                display_name="Operator",
            ),
            AdminStoredUser(
                username=settings.admin_viewer_username,
                role="viewer",
                password_hash=AdminStore._hash_password(settings.admin_viewer_password),
                display_name="Viewer",
            ),
        ]

    async def initialize(self) -> None:
        async with self._lock:
            if self._mongo_enabled and self._db is not None:
                try:
                    await self._mongo_client.admin.command("ping")
                    await self._db.admin_users.create_index("username", unique=True)
                    await self._db.admin_sessions.create_index("token_hash", unique=True)
                    await self._db.admin_sessions.create_index("expires_at", expireAfterSeconds=0)
                    await self._db.audit_log.create_index("timestamp")
                    await self._db.runtime_state.create_index("key", unique=True)
                    await self._seed_defaults()
                    self._runtime_settings = await self._load_runtime_settings()
                    return
                except Exception as error:
                    logger.warning("MongoDB admin store unavailable, using in-memory fallback: %s", error)
                    self._mongo_enabled = False
                    self._mongo_client = None
                    self._db = None

            if not self._memory_users:
                for user in self._default_users(self.settings):
                    self._memory_users[user.username] = user
            self._runtime_settings = RuntimeSettings()

    async def _seed_defaults(self) -> None:
        assert self._db is not None
        existing_count = await self._db.admin_users.count_documents({})
        if existing_count > 0:
            return

        default_users = self._default_users(self.settings)
        await self._db.admin_users.insert_many([user.model_dump(mode="python") for user in default_users])

        if await self._db.runtime_state.count_documents({}) == 0:
            await self._db.runtime_state.insert_one({"key": "runtime", "value": self._runtime_settings.model_dump(mode="python")})

    def _user_to_public(self, user: AdminStoredUser) -> AdminUserOut:
        return AdminUserOut(
            username=user.username,
            role=user.role,
            display_name=user.display_name,
            last_login_at=user.last_login_at,
        )

    async def _load_runtime_settings(self) -> RuntimeSettings:
        if not self._mongo_enabled or self._db is None:
            return self._runtime_settings

        document = await self._db.runtime_state.find_one({"key": "runtime"})
        if not document:
            self._runtime_settings = RuntimeSettings()
            await self._db.runtime_state.insert_one({"key": "runtime", "value": self._runtime_settings.model_dump(mode="python")})
            return self._runtime_settings

        value = document.get("value") or {}
        self._runtime_settings = RuntimeSettings.model_validate(value)
        return self._runtime_settings

    async def get_runtime_settings(self) -> RuntimeSettings:
        async with self._lock:
            if self._mongo_enabled:
                return await self._load_runtime_settings()
            return self._runtime_settings

    async def update_runtime_settings(
        self,
        actor: AdminUserOut,
        settings_update: RuntimeSettings,
        *,
        target: str = "runtime_settings",
        metadata: Optional[dict[str, Any]] = None,
    ) -> RuntimeSettings:
        async with self._lock:
            settings_update.updated_at = self._now()
            self._runtime_settings = settings_update

            if self._mongo_enabled and self._db is not None:
                await self._db.runtime_state.update_one(
                    {"key": "runtime"},
                    {"$set": {"value": settings_update.model_dump(mode="python")}},
                    upsert=True,
                )

            return self._runtime_settings

    async def get_user(self, username: str) -> Optional[AdminStoredUser]:
        if self._mongo_enabled and self._db is not None:
            document = await self._db.admin_users.find_one({"username": username})
            if not document:
                return None
            return AdminStoredUser.model_validate(document)

        return self._memory_users.get(username)

    async def authenticate(self, login: AdminLoginRequest) -> tuple[str, AdminUserOut, datetime]:
        user = await self.get_user(login.username)
        if not user or not self._verify_password(login.password, user.password_hash):
            raise ValueError("Invalid admin credentials")

        public_user = self._user_to_public(user)
        expires_at = self._now() + timedelta(seconds=self.settings.admin_token_ttl_seconds)
        token = secrets.token_urlsafe(32)
        token_hash = self._token_hash(token)

        if self._mongo_enabled and self._db is not None:
            await self._db.admin_users.update_one(
                {"username": user.username},
                {"$set": {"last_login_at": self._now()}},
            )
            await self._db.admin_sessions.insert_one(
                {
                    "token_hash": token_hash,
                    "username": user.username,
                    "role": user.role,
                    "display_name": user.display_name,
                    "expires_at": expires_at,
                    "created_at": self._now(),
                    "last_used_at": self._now(),
                }
            )
        else:
            updated_user = user.model_copy(update={"last_login_at": self._now()})
            self._memory_users[user.username] = updated_user
            self._memory_sessions[token_hash] = {
                "username": user.username,
                "role": user.role,
                "display_name": user.display_name,
                "expires_at": expires_at,
                "created_at": self._now(),
                "last_used_at": self._now(),
            }

        await self.append_audit_log(
            actor=public_user,
            action="login",
            target=public_user.username,
            metadata={"source": "admin_login"},
        )
        return token, public_user, expires_at

    async def revoke_token(self, token: str) -> None:
        token_hash = self._token_hash(token)
        if self._mongo_enabled and self._db is not None:
            await self._db.admin_sessions.delete_one({"token_hash": token_hash})
            return
        self._memory_sessions.pop(token_hash, None)

    async def authenticate_token(self, token: str) -> AdminUserOut:
        token_hash = self._token_hash(token)
        if self._mongo_enabled and self._db is not None:
            session = await self._db.admin_sessions.find_one({"token_hash": token_hash})
            if not session:
                raise ValueError("Invalid admin session")
            if session["expires_at"] < self._now():
                await self._db.admin_sessions.delete_one({"token_hash": token_hash})
                raise ValueError("Admin session expired")
            await self._db.admin_sessions.update_one(
                {"token_hash": token_hash},
                {"$set": {"last_used_at": self._now()}},
            )
            user = await self.get_user(session["username"])
            if not user:
                raise ValueError("Admin user not found")
            return self._user_to_public(user)

        session = self._memory_sessions.get(token_hash)
        if not session:
            raise ValueError("Invalid admin session")
        if session["expires_at"] < self._now():
            self._memory_sessions.pop(token_hash, None)
            raise ValueError("Admin session expired")
        session["last_used_at"] = self._now()
        user = await self.get_user(session["username"])
        if not user:
            raise ValueError("Admin user not found")
        return self._user_to_public(user)

    async def require_role(self, token: str, minimum_role: AdminRole) -> AdminUserOut:
        user = await self.authenticate_token(token)
        if self._role_rank(user.role) < self._role_rank(minimum_role):
            raise PermissionError("Insufficient admin role")
        return user

    async def append_audit_log(
        self,
        *,
        actor: AdminUserOut,
        action: str,
        target: Optional[str] = None,
        status: str = "success",
        before: Optional[dict[str, Any]] = None,
        after: Optional[dict[str, Any]] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> AuditLogEntry:
        entry = AuditLogEntry(
            id=secrets.token_hex(8),
            timestamp=self._now(),
            actor=actor.username,
            role=actor.role,
            action=action,
            target=target,
            status=status,  # type: ignore[arg-type]
            before=before,
            after=after,
            metadata=metadata or {},
        )

        if self._mongo_enabled and self._db is not None:
            await self._db.audit_log.insert_one(entry.model_dump(mode="python"))
        else:
            self._memory_audit_log.append(entry)
            self._memory_audit_log = self._memory_audit_log[-200:]

        return entry

    async def list_audit_log(self, limit: int = 50) -> list[AuditLogEntry]:
        if self._mongo_enabled and self._db is not None:
            cursor = self._db.audit_log.find({}).sort("timestamp", -1).limit(limit)
            documents = await cursor.to_list(length=limit)
            return [AuditLogEntry.model_validate(document) for document in documents]

        return list(reversed(self._memory_audit_log[-limit:]))

    async def get_dashboard_state(self) -> AdminState:
        settings = await self.get_runtime_settings()
        audit_log = await self.list_audit_log(limit=25)
        users: list[AdminStoredUser] = []
        if self._mongo_enabled and self._db is not None:
            cursor = self._db.admin_users.find({})
            users = [AdminStoredUser.model_validate(document) for document in await cursor.to_list(length=100)]
        else:
            users = list(self._memory_users.values())

        return AdminState(
            maintenance_mode=settings.maintenance_mode,
            feature_flags=settings.feature_flags,
            policy=settings.policy,
            updated_at=settings.updated_at,
            users=users,
            audit_log=audit_log,
        )

    async def list_users(self) -> list[AdminStoredUser]:
        if self._mongo_enabled and self._db is not None:
            cursor = self._db.admin_users.find({})
            return [AdminStoredUser.model_validate(document) for document in await cursor.to_list(length=100)]
        return list(self._memory_users.values())


__all__ = ["AdminStore", "ROLE_ORDER"]
