"""压测数据 seed（P3.3 线 B，ADR-011）：直连 DB / MinIO 铺压测用户、公司、
ready 文档与检索索引。

不走 API 铺数据：注册与上传各有限流、入库要过 worker 队列——用它们铺数据
既慢，又把压测目标当成了铺路工具。这里复用生产管线函数（split_pages /
embed_chunks / store_chunks / hash_password / create_access_token），产物
形态与真实入库零差别；幂等可重跑：用户/公司/文档按唯一键复用，chunks
按 (company_id, chunk_id) ON CONFLICT 跳过。

跑法（仓根，compose 三件套已 up）：

    uv run python scripts/loadtest/seed_loadtest.py --users 20

产出 scripts/loadtest/users.json：[{email, token, company_id}]，locustfile
直接读。access token 有效期本次签发临时拉到 240 分钟（只改本进程的
settings 单例，服务端只验签名与 exp，不用动）。

⚠️ seed 语料的向量是 DeterministicFakeEmbedding：只配 ARGUS_FAKE_LLM=1
的服务端（线 B）用。真模型对这些公司发问，向量空间不匹配，检索无意义。
"""

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
OUT_PATH = HERE / "users.json"

PASSWORD = "loadtest-password"  # 登录不走 API（token 直签），密码只为字段完整
EMBED_DIM = 1024  # 与 text-embedding-v4 / app.engine.ingest 的向量维度一致

# 财报风格合成语料：词面覆盖 locustfile 追问集与 fake 研究查询
# （营业收入 / 毛利率 / 主营业务 / 风险 / 现金流……），BM25 与向量两路都有命中。
CORPUS_PAGES = [
    (
        "公司2024年度经营情况概述。报告期内公司实现营业收入十二亿三千四百万元，"
        "同比增长百分之十二点五；归属于母公司股东的净利润一亿八千六百万元，"
        "同比增长百分之九点八。综合毛利率为百分之三十八点二，较上年同期上升"
        "零点六个百分点，盈利质量保持稳定。经营活动产生的现金流量净额二亿零一百万元，"
        "与净利润基本匹配，现金流状况健康。资产负债率为百分之四十一点三，"
        "负债结构以经营性负债为主，有息负债占比持续下降，偿债能力充足。"
    ),
    (
        "主营业务构成与经营分析。公司主营业务分为软件产品、技术服务与运维服务"
        "三个板块：软件产品收入七亿二千万元，占比约百分之五十八；技术服务收入"
        "三亿六千万元，占比约百分之二十九；运维服务收入一亿五千四百万元，占比约"
        "百分之十三。研发投入一亿四千七百万元，占营业收入比例为百分之十一点九，"
        "主要投向平台架构升级与智能化模块。前五大客户收入占比百分之二十七，"
        "客户集中度较上年下降，收入结构的抗风险能力有所增强。"
    ),
    (
        "风险因素与展望。公司面临的主要经营风险包括：应收账款回收风险，报告期末"
        "应收账款余额四亿一千万元，账龄一年以内占比百分之八十六；人力成本上升风险，"
        "技术人员薪酬支出同比增长百分之十五；市场竞争加剧风险，行业价格竞争可能"
        "压缩毛利率空间。公司将通过提高交付标准化程度、优化人员结构与深耕存量客户"
        "应对上述风险。展望下一报告期，公司预计营业收入保持百分之十左右的增长。"
    ),
]


def _pdf_lines(tag: str) -> list[str]:
    return [
        "Argus loadtest seed document",
        f"tag {tag}",
        "Revenue 2024: 1,234 million CNY, up 12.5 percent YoY.",
        "Gross margin: 38.2 percent, stable.",
        "Operating cash flow: 201 million CNY.",
    ]


async def seed(n_users: int) -> list[dict[str, str]]:
    # 仓根与脚本目录挂进 sys.path 后才 import（app 包按仓根解析）
    sys.path.insert(0, str(REPO_ROOT))
    sys.path.insert(0, str(HERE))
    from langchain_core.documents import Document as LCDocument
    from langchain_core.embeddings import DeterministicFakeEmbedding
    from pdfgen import make_pdf
    from sqlalchemy import select

    from app.core.config import get_settings
    from app.core.db import SessionFactory, engine
    from app.core.security import create_access_token, hash_password
    from app.core.storage import ensure_bucket, put_bytes
    from app.domain.models import Company, Document, User
    from app.engine.ingest import embed_chunks, make_source_id, split_pages
    from app.worker import store_chunks

    get_settings().jwt_access_ttl_minutes = 240  # 只影响本进程签发的 token
    await ensure_bucket()
    password_hash = hash_password(PASSWORD)  # argon2 慢哈希：一份所有账号共用
    embeddings = DeterministicFakeEmbedding(size=EMBED_DIM)
    entries: list[dict[str, str]] = []

    async with SessionFactory() as session:
        for i in range(n_users):
            email = f"loadtest-{i:03d}@load.test"
            user = (
                await session.execute(select(User).where(User.email == email))
            ).scalar_one_or_none()
            if user is None:
                user = User(email=email, password_hash=password_hash)
                session.add(user)
                await session.commit()
                await session.refresh(user)

            name = f"压测公司-{i:03d}"
            company = (
                await session.execute(
                    select(Company).where(
                        Company.owner_id == user.id, Company.name == name
                    )
                )
            ).scalar_one_or_none()
            if company is None:
                company = Company(owner_id=user.id, name=name)
                session.add(company)
                await session.commit()
                await session.refresh(company)

            pdf = make_pdf(_pdf_lines(f"seed-{i:03d}"))
            sha = hashlib.sha256(pdf).hexdigest()
            filename = f"loadtest-{i:03d}.pdf"
            doc = (
                await session.execute(
                    select(Document).where(
                        Document.company_id == company.id, Document.sha256 == sha
                    )
                )
            ).scalar_one_or_none()
            if doc is None:
                object_key = f"{user.id}/{company.id}/{sha}.pdf"
                put_bytes(object_key, pdf, "application/pdf")
                doc = Document(
                    owner_id=user.id,
                    company_id=company.id,
                    filename=filename,
                    object_key=object_key,
                    sha256=sha,
                    size_bytes=len(pdf),
                    status="ready",
                )
                session.add(doc)
                await session.commit()
                await session.refresh(doc)

            # 索引语料与 PDF 原件解耦是刻意的：原件保证 retry/重跑链路可用，
            # 索引用中文合成语料保证检索有的可命中（原件是 ASCII 占位文本）。
            # P3.4 起入库走 chunks 表（store_chunks 幂等，重跑 ON CONFLICT 跳过）
            source_id = make_source_id(filename, sha)
            company_key = str(company.id)
            pages = [
                LCDocument(
                    page_content=text,
                    metadata={
                        "source_id": source_id,
                        "company": company_key,
                        "page": p,
                    },
                )
                for p, text in enumerate(CORPUS_PAGES, start=1)
            ]
            chunks = split_pages(pages)
            added = await store_chunks(
                user.id, company.id, doc.id, chunks, embed_chunks(chunks, embeddings)
            )

            entries.append(
                {
                    "email": email,
                    "token": create_access_token(user.id),
                    "company_id": company_key,
                }
            )
            print(f"{email}: company={company_key} index+={added}")

    await engine.dispose()
    return entries


def main() -> None:
    parser = argparse.ArgumentParser(
        description="压测数据 seed：用户/公司/ready 文档/索引 + token 直签"
    )
    parser.add_argument("--users", type=int, default=20)
    args = parser.parse_args()
    entries = asyncio.run(seed(args.users))
    OUT_PATH.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"wrote {len(entries)} users -> {OUT_PATH}")


if __name__ == "__main__":
    main()
