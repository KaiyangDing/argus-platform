"""用量与配额端点：前端用量条与「为什么被 429」读同一份数据。"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import get_current_user
from app.models import User
from app.schemas import UsageOut
from app.usage import WINDOW_HOURS, quota_status

router = APIRouter(prefix="/api", tags=["usage"])


@router.get("/usage")
async def get_usage(
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UsageOut:
    st = await quota_status(session, user.id)
    return UsageOut(
        window_hours=WINDOW_HOURS,
        spend_cny=float(st.spend_cny),
        budget_cny=float(st.budget_cny),
        input_tokens=st.input_tokens,
        output_tokens=st.output_tokens,
        running_tasks=st.running_tasks,
        max_running=st.max_running,
    )
