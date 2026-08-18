"""模型接入的单一事实源：ChatOpenAI + dashscope OpenAI 兼容端点。

设计源自研究仓撞墙记录①：ChatTongyi 无标准 usage_metadata 且
community 集成整包日落，官方兼容端点 + langchain-openai 是正路。
"""

from langchain_openai import ChatOpenAI

from app.config import get_settings

DASHSCOPE_COMPAT_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
CHAT_MODEL = "qwen-flash"
EMBED_MODEL = "text-embedding-v4"


def make_chat(model: str = CHAT_MODEL, temperature: float = 0.0) -> ChatOpenAI:
    return ChatOpenAI(
        model=model,
        base_url=DASHSCOPE_COMPAT_BASE,
        api_key=get_settings().dashscope_api_key,
        temperature=temperature,
    )
