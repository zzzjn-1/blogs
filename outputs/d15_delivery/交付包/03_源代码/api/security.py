# -*- coding: utf-8 -*-
"""口令哈希与 JWT 签发/校验（计划书 4.7 鉴权列、4.7.1 媒体鉴权、10.2 隐私）。

## 两个刻意的选择

**① 直接用 `bcrypt`，不用 `passlib`。**
`requirements-app.txt` 列了 `passlib[bcrypt]`，但实测 passlib 1.7.4 与 bcrypt 4.2.1 组合时，
每次 `CryptContext.hash()` 都会往 stderr 打一段 `AttributeError: module 'bcrypt' has no
attribute '__about__'` 的回溯（passlib 读版本号的老写法，失败后回退）。功能不受影响，
但服务日志会被这段回溯刷屏 —— 而本项目恰恰要求「告警要能看见真问题」（R14 同一条取向）。
passlib 只做了一层薄封装，直接调 bcrypt 代码更短、噪声为零。

**② 口令先过一轮 sha256 → base64 再交给 bcrypt。**
bcrypt 只认口令的**前 72 字节**。中文口令一个字 3 字节，即 24 个汉字就撞上限：
「二十四个相同汉字 + 任意后缀」的两个不同口令会被判为同一个。
sha256 摘要经 base64 后恒为 44 字节，任何长度的口令都落在 bcrypt 的有效区间内，
从根上消除这个歧义（也是 passlib 的 `bcrypt_sha256` 方案所做的事）。
"""
from __future__ import annotations

import base64
import hashlib
import logging
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from api.config import Settings

log = logging.getLogger(__name__)

#: 生产用 12；测试夹具用 4 加速（4 轮 ≈ 1 ms，12 轮 ≈ 250 ms）
DEFAULT_ROUNDS = 12
TEST_ROUNDS = 4


class AuthError(Exception):
    """鉴权类错误的基类。"""


class TokenError(AuthError):
    """令牌缺失/非法/过期。"""


class AuthConfigError(AuthError):
    """服务端鉴权配置缺失（如 JWT_SECRET 为空）。"""


# --------------------------------------------------------------------------- #
# 口令
# --------------------------------------------------------------------------- #

def prepare_password(password: str) -> bytes:
    """把任意长度口令压成 44 字节（见模块 docstring ②）。"""
    raw = (password or "").encode("utf-8")
    return base64.b64encode(hashlib.sha256(raw).digest())


def hash_password(password: str, *, rounds: int = DEFAULT_ROUNDS) -> str:
    return bcrypt.hashpw(prepare_password(password),
                         bcrypt.gensalt(rounds=rounds)).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    """校验口令。哈希串损坏/为空时返回 False 而不是抛异常 —— 让调用方只处理布尔。"""
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(prepare_password(password), password_hash.encode("ascii"))
    except (ValueError, TypeError):
        log.warning("口令哈希串格式非法，按校验失败处理")
        return False


# --------------------------------------------------------------------------- #
# JWT
# --------------------------------------------------------------------------- #

def create_access_token(user_id: int, settings: Settings) -> tuple[str, int]:
    """签发访问令牌，返回 `(token, 有效期秒数)`。

    `sub` 必须是字符串：PyJWT 2.10 对非字符串 sub 会在解码侧做类型校验，
    写成 int 时 `jwt.decode` 抛 `InvalidSubjectError`（实测踩过）。
    """
    if not (settings.jwt_secret or "").strip():
        raise AuthConfigError(
            "JWT_SECRET 未配置，拒绝签发令牌（请写入 .env；生产环境必须为随机长串）")
    expire_s = max(60, int(settings.jwt_expire_minutes) * 60)
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=expire_s)).timestamp()),
    }
    token = jwt.encode(payload, settings.jwt_secret,
                       algorithm=settings.jwt_algorithm or "HS256")
    return token, expire_s


def decode_access_token(token: str, settings: Settings) -> int:
    """校验令牌并返回 user_id。任何问题一律抛 `TokenError`（不泄漏具体原因给客户端）。"""
    if not token:
        raise TokenError("缺少令牌")
    if not (settings.jwt_secret or "").strip():
        raise AuthConfigError("JWT_SECRET 未配置，无法校验令牌")
    try:
        payload = jwt.decode(token, settings.jwt_secret,
                             algorithms=[settings.jwt_algorithm or "HS256"])
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("令牌已过期") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("令牌非法") from exc

    sub = payload.get("sub")
    try:
        return int(sub)
    except (TypeError, ValueError) as exc:
        raise TokenError("令牌缺少有效的 sub") from exc
