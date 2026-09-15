"""配置：只从环境读，不含逻辑。"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_MAX_STEPS = 25


@dataclass(frozen=True)
class Config:
    base_url: str
    api_key: str
    model: str
    max_steps: int = DEFAULT_MAX_STEPS

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        api_key = os.environ.get("NORMA_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "缺少 NORMA_API_KEY。请复制 .env.example 为 .env 并填入密钥。"
            )
        return cls(
            base_url=os.environ.get("NORMA_BASE_URL", "").strip() or DEFAULT_BASE_URL,
            api_key=api_key,
            model=os.environ.get("NORMA_MODEL", "").strip() or DEFAULT_MODEL,
            max_steps=_parse_max_steps(os.environ.get("NORMA_MAX_STEPS", "")),
        )


def _parse_max_steps(raw: str) -> int:
    """NORMA_MAX_STEPS 的解析与校验。

    配置写错是日常事件，报错必须说人话——裸的 int() ValueError 是一串英文
    traceback，而约束要求面向用户的文案是中文。
    """
    raw = raw.strip()
    if not raw:
        return DEFAULT_MAX_STEPS
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(
            f"NORMA_MAX_STEPS 必须是整数，当前值是：{raw!r}"
        ) from None
    if value < 1:
        raise RuntimeError(f"NORMA_MAX_STEPS 必须大于 0，当前值是：{value}")
    return value
