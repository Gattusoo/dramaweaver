"""字段级对称加密工具（Fernet，密钥由 settings.SECRET_KEY 派生）。

用于敏感配置（如 UserProviderCredential.encrypted_key）的落库加密：
- 密钥派生：HKDF-SHA256(secret_key, salt=_SALT, info=model 标签)
  → 32 字节 → urlsafe_base64 → Fernet key
- 同一 SECRET_KEY 不同用途派生不同密钥（HKDF info 字段隔离）
- SECRET_KEY 泄漏已意味着全盘失控，作加密密钥安全等价（无需独立密钥管理）

用法：
    from apps.core.utils.encryption import encrypt_value, decrypt_value

    cipher = encrypt_value("sk-xxx", purpose="user_provider_credential")
    plain = decrypt_value(cipher, purpose="user_provider_credential")

格式：密文为 str，Fernet token 的 ASCII 形式；前缀 "v1:" 标记算法/版本，便于未来轮换。
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

_VERSION_PREFIX = "v1:"
# 注意：此盐值承自原 dvadmin 代码，已被既有密文依赖——勿改，否则历史数据不可解。
_SALT = b"dvadmin-field-encryption"
_KEY_CACHE: dict[str, bytes] = {}


class EncryptionError(ValueError):
    """加解密失败（密文损坏、密钥不匹配、版本不支持等）。"""


def _derive_key(purpose: str) -> bytes:
    """HKDF 从 SECRET_KEY 派生 32 字节，按 purpose 隔离；结果缓存。"""
    if purpose in _KEY_CACHE:
        return _KEY_CACHE[purpose]
    secret = getattr(settings, "SECRET_KEY", None)
    if not secret:
        raise ImproperlyConfigured("settings.SECRET_KEY 未配置，无法派生字段加密密钥")
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_SALT,
        info=purpose.encode("utf-8"),
    )
    raw = hkdf.derive(secret.encode("utf-8"))
    fernet_key = base64.urlsafe_b64encode(raw)
    _KEY_CACHE[purpose] = fernet_key
    return fernet_key


def encrypt_value(plain: str, *, purpose: str) -> str:
    """加密字符串，返回带版本前缀的密文。空字符串原样返回（不加密空值）。"""
    if not plain:
        return ""
    f = Fernet(_derive_key(purpose))
    token = f.encrypt(plain.encode("utf-8")).decode("ascii")
    return _VERSION_PREFIX + token


def decrypt_value(cipher: str, *, purpose: str) -> str:
    """解密 encrypt_value 产物；版本/密钥不匹配抛 EncryptionError。空字符串原样返回。"""
    if not cipher:
        return ""
    if not cipher.startswith(_VERSION_PREFIX):
        raise EncryptionError(
            f"不支持的密文格式（缺少 {_VERSION_PREFIX} 前缀）；"
            "若是历史明文/旧格式数据，需先迁移"
        )
    token = cipher[len(_VERSION_PREFIX):]
    f = Fernet(_derive_key(purpose))
    try:
        return f.decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise EncryptionError("解密失败：密文损坏或密钥不匹配") from exc


def fingerprint(plain: str) -> str:
    """返回明文的不可逆指纹（用于日志/审计的安全标识，等价"末 4 位"场景）。"""
    if not plain:
        return ""
    if len(plain) <= 4:
        return plain
    return plain[-4:]


def hash_for_compare(plain: str, *, purpose: str) -> str:
    """HMAC-SHA256(plain, purpose-key) 的 hex，用于"不存明文但可判等"的场景。

    与 encrypt_value 不同：单向，无法解密；相同明文+相同 purpose 输出稳定。
    """
    if not plain:
        return ""
    key = _derive_key(purpose)
    return hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), key, 1).hex()
