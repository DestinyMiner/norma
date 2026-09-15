"""工具定义、注册表，以及 v1 的本机工具实现。"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Awaitable, Callable

from pydantic import BaseModel, Field

MAX_RESULT_CHARS = 8000

log = logging.getLogger("norma.audit")


class Risk(StrEnum):
    READ = "read"
    WRITE = "write"
    EXEC = "exec"


@dataclass
class Tool:
    name: str
    description: str
    params: type[BaseModel]
    risk: Risk
    fn: Callable[..., Awaitable[str]]


def openai_schema(tool: Tool) -> dict:
    """pydantic 模型直接变成模型可用的 tool 定义，零手写。"""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.params.model_json_schema(),
        },
    }


def tools_schema() -> list[dict]:
    return [openai_schema(t) for t in TOOLS.values()]


def truncate(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    """工具输出会原样进入模型上下文，必须设硬上限。"""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[已截断，原文 {len(text)} 字符]"


def audit(name: str, args: dict, ok: bool, content: str) -> None:
    """一个能在你机器上执行命令的助手，必须能事后查它干了什么。"""
    brief = content[:200].replace("\n", " ")
    log.info("tool=%s ok=%s args=%s result=%s", name, ok, args, brief)


# ---------- list_dir ----------

class ListDirParams(BaseModel):
    path: str = Field(".", description="要列出的目录路径")


async def _list_dir(path: str = ".") -> str:
    target = Path(path).expanduser()
    if not target.exists():
        return f"路径不存在：{target}"
    if not target.is_dir():
        return f"不是目录：{target}"
    lines = []
    for child in sorted(target.iterdir(), key=lambda c: (c.is_file(), c.name.lower())):
        if child.is_dir():
            lines.append(f"[目录] {child.name}/")
        else:
            lines.append(f"[文件] {child.name}  {child.stat().st_size} 字节")
    return "\n".join(lines) if lines else "（空目录）"


# ---------- read_file ----------

class ReadFileParams(BaseModel):
    path: str = Field(..., description="要读取的文件路径")
    max_bytes: int = Field(64000, description="最多读取的字节数")


async def _read_file(path: str, max_bytes: int = 64000) -> str:
    target = Path(path).expanduser()
    if not target.is_file():
        return f"文件不存在：{target}"
    data = target.read_bytes()[:max_bytes]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        # 按字节截断可能正好切在多字节字符中间——中文文本里这很常见（每字 3 字节）。
        # 回退到最后一个完整字符边界，而不是把整份中文文本误报成二进制文件：
        # 错误的诊断比含糊的诊断更糟，模型会据此放弃这个文件。
        for backoff in (1, 2, 3):
            if len(data) <= backoff:
                break
            try:
                return data[:-backoff].decode("utf-8")
            except UnicodeDecodeError:
                continue
        return f"无法按 UTF-8 解码（可能是二进制文件）：{target}"


TOOLS: dict[str, Tool] = {
    tool.name: tool
    for tool in [
        Tool(
            name="list_dir",
            description="列出目录下的文件和子目录。",
            params=ListDirParams,
            risk=Risk.READ,
            fn=_list_dir,
        ),
        Tool(
            name="read_file",
            description="读取一个文本文件的内容。",
            params=ReadFileParams,
            risk=Risk.READ,
            fn=_read_file,
        ),
    ]
}
