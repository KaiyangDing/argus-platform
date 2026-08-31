import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.limits import LOGIN_PER_MIN, REFRESH_PER_MIN, REGISTER_PER_MIN, rate_limit
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.deps import get_current_user
from app.domain.models import User
from app.domain.schemas import LoginIn, RefreshIn, RegisterIn, TokenPair, UserOut

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _token_pair(user_id: uuid.UUID) -> TokenPair:
    return TokenPair(
        access_token=create_access_token(user_id),
        refresh_token=create_refresh_token(user_id),
    )


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("register", REGISTER_PER_MIN))],
)
async def register(
    body: RegisterIn, session: Annotated[AsyncSession, Depends(get_session)]
) -> TokenPair:
    user = User(email=body.email.lower(), password_hash=hash_password(body.password))
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email already registered"
        ) from exc
    return _token_pair(user.id)


@router.post("/login", dependencies=[Depends(rate_limit("login", LOGIN_PER_MIN))])
async def login(
    body: LoginIn, session: Annotated[AsyncSession, Depends(get_session)]
) -> TokenPair:
    res = await session.execute(select(User).where(User.email == body.email.lower()))
    user = res.scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password"
        )
    return _token_pair(user.id)


@router.post("/refresh", dependencies=[Depends(rate_limit("refresh", REFRESH_PER_MIN))])
async def refresh(body: RefreshIn) -> TokenPair:
    user_id = decode_token(body.refresh_token, "refresh")
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        )
    return _token_pair(user_id)


@router.get("/me")
async def me(user: Annotated[User, Depends(get_current_user)]) -> UserOut:
    return UserOut.model_validate(user)
