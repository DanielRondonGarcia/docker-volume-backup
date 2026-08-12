import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional


logger = logging.getLogger(__name__)

ROLE_VIEWER = "viewer"
ROLE_OPERATOR = "operator"
ROLE_ADMIN = "admin"
ROLE_ORDER = {
    ROLE_VIEWER: 10,
    ROLE_OPERATOR: 20,
    ROLE_ADMIN: 30,
}


@dataclass
class AuthUser:
    username: str
    password: str
    role: str
    must_change_password: bool = False
    password_scheme: str = "plain"


class AuthService:
    def __init__(
        self,
        users: Dict[str, AuthUser],
        session_secret: bytes,
        session_ttl_seconds: int = 28800,
        users_file_path: Optional[Path] = None,
    ):
        self.users = users
        self.session_secret = session_secret
        self.session_ttl_seconds = session_ttl_seconds
        self.users_file_path = users_file_path

    @classmethod
    def from_runtime(cls):
        users, users_file_path = _load_users()
        secret = _load_or_create_secret(
            env_value=os.environ.get("CONTROL_PLANE_SESSION_KEY"),
            key_file_path=os.environ.get("CONTROL_PLANE_SESSION_KEY_FILE", ".control_plane.session.key"),
        )
        ttl = int(os.environ.get("CONTROL_PLANE_SESSION_TTL_SECONDS", "28800"))
        return cls(
            users=users,
            session_secret=secret,
            session_ttl_seconds=ttl,
            users_file_path=users_file_path,
        )

    def authenticate(self, username: str, password: str) -> Optional[AuthUser]:
        user = self.users.get(username)
        if user is None:
            return None
        if not _verify_password(user, password):
            return None
        return user

    def issue_session_token(self, user: AuthUser) -> str:
        payload = {
            "username": user.username,
            "exp": int(time.time()) + self.session_ttl_seconds,
        }
        payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        payload_b64 = _b64encode(payload_bytes)
        signature = hmac.new(self.session_secret, payload_b64.encode("utf-8"), hashlib.sha256).digest()
        return f"{payload_b64}.{_b64encode(signature)}"

    def parse_session_token(self, token: str) -> Optional[Dict[str, str]]:
        if not token or "." not in token:
            return None
        payload_part, signature_part = token.split(".", 1)
        expected_signature = hmac.new(self.session_secret, payload_part.encode("utf-8"), hashlib.sha256).digest()
        if not secrets.compare_digest(_b64encode(expected_signature), signature_part):
            return None
        try:
            payload = json.loads(_b64decode(payload_part).decode("utf-8"))
        except Exception:
            return None
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        username = payload.get("username")
        user = self.users.get(username)
        if user is None or user.role not in ROLE_ORDER:
            return None
        return {
            "username": user.username,
            "role": user.role,
            "must_change_password": user.must_change_password,
        }

    def can_access(self, actual_role: str, required_role: str) -> bool:
        return ROLE_ORDER.get(actual_role, 0) >= ROLE_ORDER.get(required_role, 999)

    def get_user(self, username: str) -> Optional[AuthUser]:
        return self.users.get(username)

    def is_local_user_management_enabled(self) -> bool:
        return self.users_file_path is not None

    def auth_management_summary(self) -> Dict[str, Optional[str]]:
        return {
            "manageable": self.is_local_user_management_enabled(),
            "source": "file" if self.users_file_path is not None else "environment",
            "users_file": str(self.users_file_path) if self.users_file_path is not None else None,
        }

    def list_users_public(self) -> list[Dict[str, object]]:
        return [
            {
                "username": user.username,
                "role": user.role,
                "must_change_password": user.must_change_password,
                "password_scheme": user.password_scheme,
            }
            for user in sorted(self.users.values(), key=lambda item: item.username)
        ]

    def change_password(self, username: str, current_password: str, new_password: str) -> AuthUser:
        user = self.users.get(username)
        if user is None:
            raise ValueError("user not found")
        if self.users_file_path is None:
            raise RuntimeError("password changes are not supported for users loaded from environment")
        if not _verify_password(user, current_password):
            raise ValueError("invalid current password")
        _validate_new_password(new_password, username=username)
        updated_user = AuthUser(
            username=user.username,
            password=_hash_password(new_password),
            role=user.role,
            must_change_password=False,
            password_scheme="pbkdf2_sha256",
        )
        self.users[user.username] = updated_user
        self._save_users_file()
        return updated_user

    def create_user(self, username: str, password: str, role: str, must_change_password: bool = True) -> AuthUser:
        self._require_file_backed_management()
        normalized_username = _validate_username(username)
        _validate_role(role)
        if normalized_username in self.users:
            raise ValueError("user already exists")
        _validate_new_password(password, username=normalized_username)
        user = AuthUser(
            username=normalized_username,
            password=_hash_password(password),
            role=role,
            must_change_password=must_change_password,
            password_scheme="pbkdf2_sha256",
        )
        self.users[normalized_username] = user
        self._save_users_file()
        return user

    def admin_reset_password(
        self,
        username: str,
        new_password: str,
        must_change_password: bool = True,
        role: Optional[str] = None,
    ) -> AuthUser:
        self._require_file_backed_management()
        normalized_username = _validate_username(username)
        user = self.users.get(normalized_username)
        if user is None:
            raise ValueError("user not found")
        effective_role = role or user.role
        _validate_role(effective_role)
        _validate_new_password(new_password, username=normalized_username)
        updated_user = AuthUser(
            username=user.username,
            password=_hash_password(new_password),
            role=effective_role,
            must_change_password=must_change_password,
            password_scheme="pbkdf2_sha256",
        )
        self.users[normalized_username] = updated_user
        self._save_users_file()
        return updated_user

    def update_user(
        self,
        username: str,
        role: Optional[str] = None,
        must_change_password: Optional[bool] = None,
    ) -> AuthUser:
        self._require_file_backed_management()
        normalized_username = _validate_username(username)
        user = self.users.get(normalized_username)
        if user is None:
            raise ValueError("user not found")
        effective_role = role or user.role
        _validate_role(effective_role)
        updated_user = AuthUser(
            username=user.username,
            password=user.password,
            role=effective_role,
            must_change_password=user.must_change_password if must_change_password is None else must_change_password,
            password_scheme=user.password_scheme,
        )
        self.users[normalized_username] = updated_user
        self._save_users_file()
        return updated_user

    def _require_file_backed_management(self):
        if self.users_file_path is None:
            raise RuntimeError("local user administration requires CONTROL_PLANE_USERS_FILE and is disabled for environment-backed users")

    def _save_users_file(self):
        if self.users_file_path is None:
            return
        payload = [
            {
                "username": user.username,
                "password": user.password,
                "role": user.role,
                "must_change_password": user.must_change_password,
                "password_scheme": user.password_scheme,
            }
            for user in sorted(self.users.values(), key=lambda item: item.username)
        ]
        self.users_file_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.users_file_path.with_suffix(self.users_file_path.suffix + ".tmp")
        temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp_path.replace(self.users_file_path)


def _load_users() -> tuple[Dict[str, AuthUser], Optional[Path]]:
    raw = os.environ.get("CONTROL_PLANE_USERS_JSON", "").strip()
    if raw:
        return _parse_users_json(raw), None

    users_file_path = Path(os.environ.get("CONTROL_PLANE_USERS_FILE", ".control_plane.users.json"))
    if users_file_path.exists():
        logger.info("Using file-backed local users from %s", users_file_path)
        return _parse_users_json(users_file_path.read_text(encoding="utf-8")), users_file_path

    logger.warning("CONTROL_PLANE_USERS_JSON not set; creating local bootstrap admin/changeme at %s", users_file_path)
    bootstrap_users = {
        "admin": AuthUser(
            username="admin",
            password=_hash_password("changeme"),
            role=ROLE_ADMIN,
            must_change_password=True,
            password_scheme="pbkdf2_sha256",
        )
    }
    users_file_path.parent.mkdir(parents=True, exist_ok=True)
    users_file_path.write_text(
        json.dumps(
            [
                {
                    "username": "admin",
                    "password": bootstrap_users["admin"].password,
                    "role": ROLE_ADMIN,
                    "must_change_password": True,
                    "password_scheme": "pbkdf2_sha256",
                }
            ],
            indent=2,
        ),
        encoding="utf-8",
    )
    return bootstrap_users, users_file_path


def _parse_users_json(raw: str) -> Dict[str, AuthUser]:
    items = json.loads(raw)
    users: Dict[str, AuthUser] = {}
    for item in items:
        username = item["username"]
        role = item.get("role", ROLE_VIEWER)
        users[username] = AuthUser(
            username=username,
            password=item["password"],
            role=role,
            must_change_password=bool(item.get("must_change_password", False)),
            password_scheme=item.get("password_scheme", "plain"),
        )
    return users


def _load_or_create_secret(env_value: Optional[str], key_file_path: str) -> bytes:
    if env_value:
        return env_value.encode("utf-8")
    path = Path(key_file_path)
    if path.exists():
        return path.read_text(encoding="utf-8").strip().encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    generated = secrets.token_urlsafe(48)
    path.write_text(generated, encoding="utf-8")
    return generated.encode("utf-8")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("utf-8")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("utf-8"))


def _hash_password(password: str, iterations: int = 390000) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${iterations}${salt}${digest}".format(
        iterations=iterations,
        salt=_b64encode(salt),
        digest=_b64encode(digest),
    )


def _verify_password(user: AuthUser, candidate_password: str) -> bool:
    if user.password_scheme == "plain":
        return secrets.compare_digest(user.password, candidate_password)
    if user.password_scheme != "pbkdf2_sha256":
        return False
    try:
        _, iterations_raw, salt_raw, digest_raw = user.password.split("$", 3)
        iterations = int(iterations_raw)
        salt = _b64decode(salt_raw)
        expected_digest = _b64decode(digest_raw)
    except Exception:
        return False
    candidate_digest = hashlib.pbkdf2_hmac("sha256", candidate_password.encode("utf-8"), salt, iterations)
    return secrets.compare_digest(candidate_digest, expected_digest)


def _validate_new_password(password: str, username: str):
    if len(password) < 8:
        raise ValueError("new password must contain at least 8 characters")
    if password.lower() in {"changeme", "admin", username.lower()}:
        raise ValueError("choose a stronger password")


def _validate_username(username: str) -> str:
    normalized = username.strip()
    if len(normalized) < 3:
        raise ValueError("username must contain at least 3 characters")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    if any(char not in allowed for char in normalized):
        raise ValueError("username may only contain letters, digits, dot, underscore, or hyphen")
    return normalized


def _validate_role(role: str):
    if role not in ROLE_ORDER:
        raise ValueError("invalid role")
