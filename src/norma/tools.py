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

# read_file 的内部读取上限。**不是承诺，是缓冲区大小**——真正决定模型看见多少的
# 是上面那道 8000 字符的结果截断（它管所有工具，见 agent.py 的 truncate 调用）。
# 这个常量只干一件事：把文件按字节切开之后，才能判断"末尾那个非法字节是不是我们自己
# 切的"——那是下面二进制回退逻辑的前提。顺带让一个 200MB 的文件不会被整个读进内存。
_READ_CAP_BYTES = 64000

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


def decode_text(data: bytes, *, truncated: bool) -> str | None:
    """把读进来的字节解码成文本；`None` 表示这不是 UTF-8 文本。

    `truncated=True` 只表示**这批字节是被读文件时切短的**，于是末尾残留的半个字符
    是我们自己造成的，该丢掉重试；`truncated=False` 表示文件本身就没解完，
    那它多半是二进制，**不能**靠回退把它当文本悄悄返回。

    （"是不是二进制"的判断留在调用方——这里返回 None 而不是一句现成的错误文案，
    否则调用方只能靠字符串前缀去认结果，那种耦合迟早出错。）

    抽成独立函数是为了可测：原实现靠 `max_bytes` 参数构造"切在字中间"的输入，
    参数删掉后，这里直接喂裸字节——覆盖不丢，也不用造 64000 字节的测试文件。
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        if not truncated:
            return None
        # 一个字符最多 4 字节，所以切点离字符边界最多 3 字节，退到 3 就够。
        for backoff in (1, 2, 3):
            if len(data) <= backoff:
                continue
            try:
                return data[:-backoff].decode("utf-8")
            except UnicodeDecodeError:
                continue
    return None


async def _read_file(path: str) -> str:
    target = Path(path).expanduser()
    if not target.is_file():
        return f"文件不存在：{target}"

    size = target.stat().st_size
    truncated = size > _READ_CAP_BYTES
    with target.open("rb") as handle:
        data = handle.read(_READ_CAP_BYTES)
    decoded = decode_text(data, truncated=truncated)
    if decoded is None:
        return f"无法按 UTF-8 解码（可能是二进制文件）：{target}"
    if truncated:
        # 这道标记是必须的：不加的话，下面 truncate() 那层标注的"原文 N 字符"
        # 报的是**我们读进来的那一段**，模型会把它当成整个文件的长度——
        # 又是一次"工具说了不真的话"。这里报的 size 才是文件真实大小。
        return f"{decoded}\n…[文件共 {size} 字节，这里只读了开头 {_READ_CAP_BYTES} 字节]"
    return decoded


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
    再顺着模型上下文、CLI 渲染和审计日志扩散出去。密钥不该出现在被执行命令的环境里。
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
            description=(
                "读取一个文本文件的内容。一次最多返回 8000 字符；超出会截断并标注"
                "本次读到的长度——没有可以调大这个上限的参数，不要为此重试更大的值。"
                "文件比这更大时会另有说明。"
            ),
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
