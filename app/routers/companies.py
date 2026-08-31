import hashlib
import uuid
from typing import Annotated

import structlog
from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.deps import get_arq, get_current_user
from app.limits import UPLOAD_PER_MIN, rate_limit
from app.models import Company, Document, User
from app.schemas import CompanyIn, CompanyOut, DocumentOut
from app.storage import put_pdf

router = APIRouter(prefix="/api/companies", tags=["companies"])
log = structlog.get_logger()

_CHUNK_SIZE = 1024 * 1024


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_company(
    body: CompanyIn,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CompanyOut:
    company = Company(owner_id=user.id, name=body.name)
    session.add(company)
    try:
        await session.commit()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Company already exists"
        ) from exc
    await session.refresh(company)
    return CompanyOut.model_validate(company)


@router.get("")
async def list_companies(
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[CompanyOut]:
    res = await session.execute(
        select(Company)
        .where(Company.owner_id == user.id)
        .order_by(Company.created_at.desc())
    )
    return [CompanyOut.model_validate(c) for c in res.scalars()]


async def _get_own_company(
    company_id: uuid.UUID, user: User, session: AsyncSession
) -> Company:
    company = await session.get(Company, company_id)
    if company is None or company.owner_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Company not found"
        )
    return company


@router.get("/{company_id}/documents")
async def list_documents(
    company_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[DocumentOut]:
    company = await _get_own_company(company_id, user, session)
    res = await session.execute(
        select(Document)
        .where(Document.company_id == company.id)
        .order_by(Document.created_at.desc())
    )
    return [DocumentOut.model_validate(d) for d in res.scalars()]


@router.post(
    "/{company_id}/documents",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("upload", UPLOAD_PER_MIN))],
)
async def upload_document(
    company_id: uuid.UUID,
    file: UploadFile,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    arq: Annotated[ArqRedis, Depends(get_arq)],
) -> DocumentOut:
    company = await _get_own_company(company_id, user, session)

    max_bytes = get_settings().max_upload_mb * 1024 * 1024
    hasher = hashlib.sha256()
    chunks: list[bytes] = []
    size = 0
    while chunk := await file.read(_CHUNK_SIZE):
        size += len(chunk)
        if size > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"File exceeds {get_settings().max_upload_mb} MB limit",
            )
        hasher.update(chunk)
        chunks.append(chunk)

    data = b"".join(chunks)
    if not data.startswith(b"%PDF-"):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Not a PDF file",
        )

    sha256 = hasher.hexdigest()
    document = Document(
        owner_id=user.id,
        company_id=company.id,
        filename=file.filename or "unnamed.pdf",
        object_key=f"{user.id}/{company.id}/{sha256}.pdf",
        sha256=sha256,
        size_bytes=size,
        status="queued",
    )
    session.add(document)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This file was already uploaded to this company",
        ) from exc
    await put_pdf(document.object_key, data)
    await session.commit()
    try:
        await arq.enqueue_job(
            "ingest_document", str(document.id), _job_id=f"ingest:{document.id}"
        )
    except Exception:
        log.exception("ingest_enqueue_failed", document_id=str(document.id))
        document.status = "failed"
        document.error = "任务入队失败，请点重试"
        await session.commit()
    await session.refresh(document)
    return DocumentOut.model_validate(document)


@router.post("/{company_id}/documents/{document_id}/retry")
async def retry_document(
    company_id: uuid.UUID,
    document_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    arq: Annotated[ArqRedis, Depends(get_arq)],
) -> DocumentOut:
    company = await _get_own_company(company_id, user, session)
    doc = await session.get(Document, document_id)
    if doc is None or doc.company_id != company.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document not found"
        )
    if doc.status not in ("failed", "queued"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="只有 failed 或卡在 queued 的文档可以重新入队",
        )
    doc.status = "queued"
    doc.error = None
    await session.commit()
    try:
        await arq.enqueue_job(
            "ingest_document", str(doc.id), _job_id=f"ingest:{doc.id}"
        )
    except Exception:
        log.exception("ingest_enqueue_failed", document_id=str(doc.id))
        doc.status = "failed"
        doc.error = "任务入队失败，请点重试"
        await session.commit()
    await session.refresh(doc)
    return DocumentOut.model_validate(doc)
