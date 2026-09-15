"""工具定义、注册表，以及 v1 的本机工具实现。"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
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


def tools_schema(registry: dict[str, Tool] | None = None) -> list[dict]:
    """生成模型可见的 tool 定义。

    传 registry 时以它为准——注入自定义工具集的调用方必须让模型看到**那一份**，
    否则模型会被展示它根本调不到的工具。注意用 `is None` 而非真值判断：
    空注册表是合法输入，不该悄悄回落到全局 TOOLS。
    """
    tools = TOOLS if registry is None else registry
    return [openai_schema(t) for t in tools.values()]


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

    # 只在**确实是我们截断的**时候才允许回退：否则一个末字节恰好非法的二进制文件
    # 会被当成文本悄悄返回。st_size 比较是精确条件，"len(data) == max_bytes"
    # 在文件恰好等于 max_bytes 时会判错。
    truncated = target.stat().st_size > max_bytes
    with target.open("rb") as handle:
        data = handle.read(max_bytes)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        if not truncated:
            return f"无法按 UTF-8 解码（可能是二进制文件）：{target}"
        # 按字节截断可能正好切在多字节字符中间——中文每字 3 字节，这很常见。
        # 回退到最后一个完整字符边界，而不是把整份中文文本误报成二进制文件。
        for backoff in (1, 2, 3):
            if len(data) <= backoff:
                break
            try:
                return data[:-backoff].decode("utf-8")
            except UnicodeDecodeError:
                continue
        return f"无法按 UTF-8 解码（可能是二进制文件）：{target}"


# ---------- write_file ----------

class WriteFileParams(BaseModel):
    path: str = Field(..., description="要写入的文件路径")
    content: str = Field(..., description="文件内容（覆盖写入）")


async def _write_file(path: str, content: str) -> str:
    target = Path(path).expanduser()
    if target.is_dir():
        return f"不是文件（是一个目录）：{target}"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        # 磁盘满、ACL 拒绝、父路径与已存在的文件同名……都归为"一次失败的写入"
        # 而不是异常：与 _list_dir / _read_file 的约定一致。
        return f"写入失败：{target}（{exc}）"
    return f"已写入 {target}（{len(content)} 字符）"


# ---------- run_powershell ----------

# PowerShell 在中文 Windows 上默认按 GBK/UTF-16 输出，直接按 UTF-8 解码会糊。
_SHELL = shutil.which("pwsh") or shutil.which("powershell") or "powershell"

# 强制子进程按 UTF-8 输出，否则中文文件名与中文输出全是乱码。
_UTF8_PREAMBLE = "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"

# ponytail: _UTF8_PREAMBLE 只设了 [Console]::OutputEncoding，它覆盖 PowerShell
#           cmdlet 与 .NET 的写入（Console.Error 与 Console.Out 共用该编码），
#           所以本文件的两个编码测试能过。但**原生命令**（如 cmd /c dir）走
#           自己的代码页，中文仍可能糊。真遇到时再让命令显式输出 UTF-8，
#           或按 [Text.Encoding]::GetEncoding(936) 解码。


class RunPowershellParams(BaseModel):
    command: str = Field(..., description="要执行的 PowerShell 命令")
    timeout_s: int = Field(60, description="超时秒数，超时会终止进程")


def _child_env() -> dict[str, str]:
    """给子进程一份剔除 NORMA_* 的环境。

    config.py 的 load_dotenv() 会把 .env 写进 os.environ，而子进程默认继承——
    于是 `Get-ChildItem env:` 或任何一句 echo 调试都可能把 API key 打进 ToolResult，
    再顺着模型上下文、CLI 渲染和 norma.log 扩散出去。密钥不该出现在被执行命令的环境里。
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("NORMA_")}


async def _run_powershell(command: str, timeout_s: int = 60) -> str:
    proc = await asyncio.create_subprocess_exec(
        _SHELL, "-NoProfile", "-NonInteractive", "-Command",
        f"{_UTF8_PREAMBLE} {command}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_child_env(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return f"命令超时（{timeout_s} 秒）已终止：{command}"

    parts = []
    text = stdout.decode("utf-8", "replace").rstrip()
    if text:
        parts.append(text)
    errtext = stderr.decode("utf-8", "replace").rstrip()
    if errtext:
        parts.append(f"[stderr]\n{errtext}")
    if proc.returncode:
        parts.append(f"[退出码 {proc.returncode}]")
    return "\n".join(parts) if parts else "（无输出）"


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
        Tool(
            name="write_file",
            description="写入文本文件，覆盖已有内容，父目录会自动创建。",
            params=WriteFileParams,
            risk=Risk.WRITE,
            fn=_write_file,
        ),
        Tool(
            name="run_powershell",
            description="在这台 Windows 电脑上执行 PowerShell 命令并返回输出。",
            params=RunPowershellParams,
            risk=Risk.EXEC,
            fn=_run_powershell,
        ),
    ]
}
