from __future__ import annotations

import asyncio
import base64
import urllib.error
import urllib.request
import hashlib
import hmac
import json
import logging
import re
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
        self._otp_store: dict[str, dict[str, Any]] = {}

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
                email=settings.admin_owner_email,
                role="owner",
                password_hash=AdminStore._hash_password(settings.admin_owner_password),
                display_name="Owner",
            ),
        ]

    @staticmethod
    def _normalize_email(email: str) -> str:
        return email.strip().lower()

    @staticmethod
    def _is_email_delivery_network_error(error: BaseException) -> bool:
        current_error: BaseException | None = error

        while current_error is not None:
            if isinstance(current_error, OSError) and getattr(current_error, "errno", None) in {101, 111, 113, 10051, 10060}:
                return True
            if isinstance(current_error, urllib.error.URLError):
                return True
            current_error = current_error.__cause__ or current_error.__context__

        return False

    async def _admin_notification_recipients(self) -> list[str]:
        recipients: dict[str, str] = {}

        for user in await self.list_users():
            if user.email and self._role_rank(user.role) >= self._role_rank("operator"):
                normalized_email = self._normalize_email(user.email)
                if normalized_email:
                    recipients[normalized_email] = normalized_email

        return list(recipients.values())

    async def _send_emailjs_message(self, *, template_id: str, template_params: dict[str, Any]) -> None:
        if not self.settings.emailjs_service_id or not self.settings.emailjs_public_key:
            raise RuntimeError("EmailJS is not configured")

        payload = {
            "service_id": self.settings.emailjs_service_id,
            "template_id": template_id,
            "user_id": self.settings.emailjs_public_key,
            "template_params": template_params,
        }
        if self.settings.emailjs_access_token:
            payload["accessToken"] = self.settings.emailjs_access_token

        def send_sync() -> dict[str, Any]:
            request = urllib.request.Request(
                self.settings.emailjs_base_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            with urllib.request.urlopen(request, timeout=20) as response:
                response_body = response.read().decode("utf-8")
                if not response_body:
                    return {}
                try:
                    return json.loads(response_body)
                except json.JSONDecodeError:
                    return {"raw": response_body}

        try:
            await asyncio.to_thread(send_sync)
        except Exception as error:
            if self._is_email_delivery_network_error(error):
                logger.warning("EmailJS delivery failed due to connectivity issue: %s", error)
            else:
                logger.warning("EmailJS delivery failed: %s", error, exc_info=True)
            raise

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
            email=user.email,
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

    async def get_user_by_email(self, email: str) -> Optional[AdminStoredUser]:
        normalized_email = self._normalize_email(email)

        if self._mongo_enabled and self._db is not None:
            document = await self._db.admin_users.find_one(
                {"email": {"$regex": f"^{re.escape(normalized_email)}$", "$options": "i"}}
            )
            if not document:
                return None
            return AdminStoredUser.model_validate(document)

        for user in self._memory_users.values():
            if user.email and self._normalize_email(user.email) == normalized_email:
                return user

        return None

    async def authenticate(self, login: AdminLoginRequest) -> tuple[str, AdminUserOut, datetime]:
        user = await self.get_user_by_email(login.email)
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

    async def _send_admin_notification(self, entry: AuditLogEntry) -> None:
        """Send a security notification for a mutating admin action."""
        logger.info(f"Starting to send admin notification for action: {entry.action}")
        recipients = await self._admin_notification_recipients()
        logger.info(
            "EmailJS Config - service_id: %s, admin_template_id: %s, recipients: %s",
            self.settings.emailjs_service_id,
            self.settings.emailjs_admin_notification_template_id,
            recipients,
        )
        if not self.settings.emailjs_service_id or not self.settings.emailjs_public_key or not self.settings.emailjs_admin_notification_template_id or not recipients:
            logger.debug("Skipping admin notification email: EmailJS or recipients not configured")
            return

        try:
            action_title = entry.action.replace("_", " ").title()
            timestamp_str = entry.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")

            text_body = (
                f"Nexdrop Administrative Update\n"
                f"============================\n"
                f"Action:    {action_title}\n"
                f"Actor:     {entry.actor} ({entry.role})\n"
                f"Target:    {entry.target or 'N/A'}\n"
                f"Status:    {entry.status.upper()}\n"
                f"Timestamp: {timestamp_str}\n"
            )
            if entry.before or entry.after:
                text_body += "\nSettings Change:\n"
                if entry.before:
                    text_body += f"BEFORE:\n{json.dumps(entry.before, indent=2, default=str)}\n"
                if entry.after:
                    text_body += f"AFTER:\n{json.dumps(entry.after, indent=2, default=str)}\n"
            if entry.metadata:
                text_body += f"\nMetadata:\n{json.dumps(entry.metadata, indent=2)}\n"

            logger.info("Sending admin notification email to %s", recipients)
            await self._send_emailjs_message(
                template_id=self.settings.emailjs_admin_notification_template_id,
                template_params={
                    "to_email": ", ".join(recipients),
                    "subject": f"Nexdrop Security Alert: {action_title.upper()}",
                    "action": action_title,
                    "actor": entry.actor,
                    "role": entry.role,
                    "target": entry.target or "N/A",
                    "status": entry.status.upper(),
                    "timestamp": timestamp_str,
                    "message": text_body,
                    "recipient_count": len(recipients),
                },
            )
            logger.info("Admin notification email sent successfully for action: %s", entry.action)
        except Exception as error:
            logger.error("Failed to send admin notification email: %s", error, exc_info=True)

    def _handle_email_task_result(self, task: asyncio.Task, entry: AuditLogEntry) -> None:
        """Callback to handle task results and log exceptions"""
        try:
            task.result()  # This will raise if there was an exception
        except Exception as e:
            logger.error(f"Email task failed for action {entry.action}: {e}", exc_info=True)

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

        # Send notification for admin panel changes and destructive actions
        notification_actions = {
            "update_settings",
            "disconnect_session",
            "disconnect_device",
            "create_user",
            "update_user",
            "delete_user",
        }
        if status == "success" and action in notification_actions:
            logger.info(f"Scheduling admin notification email for action: {action}")
            task = asyncio.create_task(self._send_admin_notification(entry))
            # Add callback to log any exceptions
            task.add_done_callback(lambda t: self._handle_email_task_result(t, entry))

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

    async def generate_otp(self, username: str) -> str:
        """Generate a 6-digit OTP and store it with expiration"""
        import secrets
        from datetime import timedelta

        otp = "".join(secrets.choice("0123456789") for _ in range(6))
        expires_at = self._now() + timedelta(minutes=10)  # OTP valid for 10 minutes

        async with self._lock:
            self._otp_store[username] = {
                "otp": otp,
                "expires_at": expires_at,
                "attempts": 0,
            }

        logger.info(f"Generated OTP for user {username}: {otp} (expires in 10 minutes)")
        return otp

    async def verify_otp(self, username: str, otp: str, *, consume: bool = True) -> bool:
        """Verify the OTP for a user."""
        async with self._lock:
            stored = self._otp_store.get(username)
            if not stored:
                logger.warning(f"OTP not found for user {username}")
                return False

            if stored["expires_at"] < self._now():
                del self._otp_store[username]
                logger.warning(f"OTP expired for user {username}")
                return False

            if stored["attempts"] >= 3:
                del self._otp_store[username]
                logger.warning(f"Too many attempts for user {username}")
                return False

            if stored["otp"] != otp:
                stored["attempts"] += 1
                logger.warning(f"Invalid OTP for user {username}, attempt {stored['attempts']}/3")
                return False

            if consume:
                del self._otp_store[username]
            logger.info(f"OTP verified successfully for user {username}")
            return True

    async def send_forgot_password_email(self, username: str, otp: str) -> None:
        """Send forgot password email with OTP to user's personal email"""
        user = await self.get_user(username)
        if not user or not user.email:
            logger.warning(f"No email found for user {username}, cannot send OTP")
            return

        try:
            text_body = (
                "Nexdrop Password Reset\n"
                "======================\n\n"
                f"Username: {username}\n\n"
                f"Your OTP for password reset is: {otp}\n\n"
                "This OTP is valid for 10 minutes.\n\n"
                "If you did not request this, please ignore this email.\n"
            )

            if not self.settings.emailjs_service_id or not self.settings.emailjs_public_key or not self.settings.emailjs_forgot_password_template_id:
                logger.debug("Skipping forgot password email: EmailJS is not configured")
                return

            logger.info(f"Sending forgot password email to {user.email} for user {username}")
            try:
                await self._send_emailjs_message(
                    template_id=self.settings.emailjs_forgot_password_template_id,
                    template_params={
                        "to_email": user.email,
                        "subject": "Nexdrop - Password Reset OTP",
                        "username": username,
                        "otp": otp,
                        "message": text_body,
                    },
                )
            except Exception as error:
                if self._is_email_delivery_network_error(error):
                    logger.warning("Skipping forgot password email for user %s: EmailJS is unreachable (%s)", username, error)
                    return
                raise
            logger.info("Forgot password email sent successfully")
        except Exception as e:
            logger.error("Failed to send forgot password email: %s", e, exc_info=True)

    async def reset_password(self, username: str, new_password: str) -> bool:
        """Reset password for a user"""
        user = await self.get_user(username)
        if not user:
            return False

        new_hash = self._hash_password(new_password)

        if self._mongo_enabled and self._db is not None:
            await self._db.admin_users.update_one(
                {"username": username},
                {"$set": {"password_hash": new_hash}},
            )
        else:
            self._memory_users[username].password_hash = new_hash

        logger.info(f"Password reset for user {username}")
        return True

    async def create_user(
        self,
        username: str,
        email: str,
        password: str,
        role: str = "viewer",
        display_name: Optional[str] = None,
    ) -> AdminStoredUser:
        """Create a new admin user (only owner can do this)"""
        # Check if user already exists
        existing = await self.get_user(username)
        if existing:
            raise ValueError(f"User '{username}' already exists")

        normalized_email = self._normalize_email(email)
        
        password_hash = self._hash_password(password)
        
        new_user = AdminStoredUser(
            username=username,
            email=normalized_email,
            role=role,
            password_hash=password_hash,
            display_name=display_name,
        )
        
        if self._mongo_enabled and self._db is not None:
            await self._db.admin_users.insert_one(new_user.model_dump(mode="python"))
        else:
            self._memory_users[username] = new_user
        
        logger.info(f"User created: {username} (email: {normalized_email}, role: {role})")
        return new_user

    async def update_user(
        self,
        username: str,
        email: Optional[str] = None,
        role: Optional[str] = None,
        display_name: Optional[str] = None,
    ) -> Optional[AdminStoredUser]:
        """Update an existing user"""
        user = await self.get_user(username)
        if not user:
            return None
        
        update_data = {}
        if email is not None:
            update_data["email"] = self._normalize_email(email)
        if role is not None:
            update_data["role"] = role
        if display_name is not None:
            update_data["display_name"] = display_name
        
        if self._mongo_enabled and self._db is not None:
            await self._db.admin_users.update_one(
                {"username": username},
                {"$set": update_data},
            )
            return await self.get_user(username)
        else:
            if email:
                user.email = self._normalize_email(email)
            if role:
                user.role = role
            if display_name:
                user.display_name = display_name
            self._memory_users[username] = user
            return user

    async def delete_user(self, username: str) -> bool:
        """Delete an admin user"""
        if username == self.settings.admin_owner_username:
            raise ValueError("Cannot delete the owner user")
        
        if self._mongo_enabled and self._db is not None:
            result = await self._db.admin_users.delete_one({"username": username})
            return result.deleted_count > 0
        else:
            if username in self._memory_users:
                del self._memory_users[username]
                return True
            return False


__all__ = ["AdminStore", "ROLE_ORDER"]
