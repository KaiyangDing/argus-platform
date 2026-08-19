"""security 层单元测试：argon2 往返与 JWT 票据边界。"""

import uuid
from datetime import UTC, datetime, timedelta

import jwt

from app.config import get_settings
from app.security import (
    ALGORITHM,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)


def test_hash_verify_roundtrip() -> None:
    hashed = hash_password("s3cret-password")
    assert hashed != "s3cret-password"
    assert verify_password("s3cret-password", hashed)
    assert not verify_password("wrong-password", hashed)


def test_access_token_roundtrip() -> None:
    uid = uuid.uuid4()
    assert decode_token(create_access_token(uid), "access") == uid


def test_refresh_token_roundtrip() -> None:
    uid = uuid.uuid4()
    assert decode_token(create_refresh_token(uid), "refresh") == uid


def test_token_type_confusion_rejected() -> None:
    uid = uuid.uuid4()
    assert decode_token(create_refresh_token(uid), "access") is None
    assert decode_token(create_access_token(uid), "refresh") is None


def _forge(payload: dict[str, object], secret: str) -> str:
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def test_expired_token_rejected() -> None:
    now = datetime.now(UTC)
    token = _forge(
        {
            "sub": str(uuid.uuid4()),
            "type": "access",
            "iat": now - timedelta(hours=2),
            "exp": now - timedelta(hours=1),
        },
        get_settings().jwt_secret,
    )
    assert decode_token(token, "access") is None


def test_wrong_secret_rejected() -> None:
    now = datetime.now(UTC)
    token = _forge(
        {
            "sub": str(uuid.uuid4()),
            "type": "access",
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        "attacker-secret-0123456789abcdef",  # ≥32 字节：只测「密钥不对」，不连带触发短密钥告警
    )
    assert decode_token(token, "access") is None


def test_tampered_token_rejected() -> None:
    token = create_access_token(uuid.uuid4())
    suffix = "AAAA" if not token.endswith("AAAA") else "BBBB"
    assert decode_token(token[:-4] + suffix, "access") is None
