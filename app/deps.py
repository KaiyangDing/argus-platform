from typing import Annotated

from arq.connections import ArqRedis
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from langchain_core.language_models import BaseChatModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.llm import make_chat
from app.models import User
from app.security import decode_token

_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None:
        raise unauthorized
    user_id = decode_token(credentials.credentials, "access")
    if user_id is None:
        raise unauthorized
    user = await session.get(User, user_id)
    if user is None:
        raise unauthorized
    return user


def get_arq(request: Request) -> ArqRedis:
    return request.app.state.arq


def get_chat_model() -> BaseChatModel:
    """对话模型注入缝：产品用 make_chat()，端点测试 override 成 Fake。"""
    return make_chat()
