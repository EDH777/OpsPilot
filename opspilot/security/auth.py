"""演示级认证：静态用户 + scrypt + HMAC 签名 Cookie。

这不是生产级 IdP：
- 没有 users/auth_sessions 表，没有服务端吊销，没有登录限流；
- 密码在启动时从 OPSPILOT_DEMO_PASSWORD 派生哈希；
- Cookie 里只有 {sub, exp}，由服务端 HMAC 签名。

生产替换点：把 current_principal 换成 OIDC/JWT 校验即可，
下游的 Principal / ROLES / 数据权限完全不用动。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request

from opspilot.config import get_settings
from opspilot.security.model import ROLES, Principal

log = logging.getLogger(__name__)

COOKIE_NAME = "opspilot_session"
SESSION_TTL_S = 8 * 3600


@dataclass(frozen=True)
class DemoUser:
    user_id: str
    display_name: str
    role: str
    workshops: frozenset[str] | None
    password_hash: str


def _hash_password(password: str, salt: bytes) -> str:
    dk = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32
    )
    return f"{base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def hash_password(password: str) -> str:
    """供测试/脚本生成演示密码哈希。"""
    return _hash_password(password, secrets.token_bytes(16))


def verify_password(password: str, encoded: str) -> bool:
    try:
        salt_b64, dk_b64 = encoded.split("$", 1)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=len(expected)
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


_DEMO_PASSWORD = get_settings().demo_password
_DEMO_USERS = {
    "boss": ("张启明", "executive", None),
    "zhaolei": ("赵磊", "workshop_manager", frozenset({"WS-03"})),
    "sunhao": ("孙浩", "workshop_manager", frozenset({"WS-05"})),
}

USERS: dict[str, DemoUser] = {
    name: DemoUser(
        user_id=f"u_{name}",
        display_name=display,
        role=role,
        workshops=workshops,
        password_hash=hash_password(_DEMO_PASSWORD),
    )
    for name, (display, role, workshops) in _DEMO_USERS.items()
}


def authenticate(username: str, password: str) -> DemoUser | None:
    user = USERS.get(username)
    return user if user and verify_password(password, user.password_hash) else None


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


_EPHEMERAL_SECRET: str | None = None


def _session_secret() -> str:
    configured = get_settings().session_secret
    if configured:
        return configured
    # 单机 demo 的兜底：每次启动随机，重启后旧 Cookie 失效。
    # 生产必须配置 OPSPILOT_SESSION_SECRET，否则多进程/重启会导致会话不可用。
    global _EPHEMERAL_SECRET
    if _EPHEMERAL_SECRET is None:
        _EPHEMERAL_SECRET = secrets.token_urlsafe(32)
        log.warning("OPSPILOT_SESSION_SECRET 未配置，使用进程内随机密钥（重启后需重新登录）")
    return _EPHEMERAL_SECRET


def sign_session(user_id: str, secret: str | None = None, ttl_s: int = SESSION_TTL_S) -> str:
    payload = _b64(
        json.dumps(
            {"sub": user_id, "exp": int(time.time()) + ttl_s},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    )
    mac = hmac.new(
        (secret or _session_secret()).encode(), payload.encode(), hashlib.sha256
    ).digest()
    return f"{payload}.{_b64(mac)}"


def read_session(token: str | None, secret: str | None = None) -> str | None:
    if not token or "." not in token:
        return None
    payload, sig = token.split(".", 1)
    expected = _b64(
        hmac.new(
            (secret or _session_secret()).encode(), payload.encode(), hashlib.sha256
        ).digest()
    )
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, json.JSONDecodeError):
        return None
    if int(data.get("exp", 0)) < time.time():
        return None
    return str(data.get("sub") or "") or None


def principal_for(name: str) -> Principal:
    """CLI / 评测 / 测试用：从演示用户构造 Principal。"""
    user = USERS[name]
    return Principal(
        user_id=user.user_id,
        display_name=user.display_name,
        role=user.role,
        workshops=user.workshops,
    )


async def current_principal(request: Request) -> Principal:
    user_id = read_session(request.cookies.get(COOKIE_NAME))
    if not user_id:
        raise HTTPException(401, "未登录")
    user = USERS.get(user_id.removeprefix("u_"))
    if not user or user.role not in ROLES:
        raise HTTPException(401, "登录状态无效")
    return Principal(
        user_id=user.user_id,
        display_name=user.display_name,
        role=user.role,
        workshops=user.workshops,
    )
