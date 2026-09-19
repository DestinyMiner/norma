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
    offset: int = Field(
        0, ge=0,
        description="从第几个字符开始读（0 起算）。接着上次读到的地方继续，就把它设成已读字符数",
    )
    limit: int = Field(
        MAX_RESULT_CHARS, ge=1,
        description=(
            f"最多返回多少字符（默认且最大 {MAX_RESULT_CHARS}；开头的进度标记也算在内）。"
            "没有调大它的办法，想多读就往后翻页"
        ),
    )


def _decode_or_none(data: bytes) -> str | None:
    """解码 UTF-8；解不开就退掉末尾残片再试，还是不行则返回 None（= 二进制）。

    末尾残片是**读取上限切出来的**：`_READ_CAP_BYTES` 可能正好落在多字节字符中间
    （中文每字 3 字节，这很常见）。它是我们造成的，该丢掉而不是把整份中文误报成二进制。

    **只在该位置确实是"切出来的半个字符"时才退**：`UnicodeDecodeError` 会告诉我们
    出错区间，只有它顶到缓冲区末尾（`end == len(data)`）才说明后面还缺字节；
    中间出现的坏字节是真的坏（`b"abc\\xff\\xfe"` 退两个字节会得到 `abc`，那是把
    二进制当文本悄悄返回——v1.0.1 特意保住的判断，别丢）。
    一个字符最多 4 字节，所以残片最多 3 字节——"够"由
    `test_every_byte_cut_of_a_four_byte_character_decodes` 逐个切点枚举证明。

    只让调用方（`_read_file`）关心"是不是二进制"：返回 None 而不是一句现成的
    错误文案，否则调用方只能靠字符串前缀去认结果，那种耦合迟早出错。
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.end != len(data):
            return None                     # 坏在中间：文件本身就不是文本
        for backoff in range(1, min(exc.end - exc.start, 3) + 1):
            if len(data) <= backoff:
                break
            try:
                return data[:-backoff].decode("utf-8")
            except UnicodeDecodeError:
                continue
    return None


_PAGE_TAIL = "，后面还有"

# 页标记里"文件只读了开头 N 字节"那一句——空串表示不写（见 _page_marker）
_SIZE_NOTE = f"（文件共 {{size}} 字节，只读了开头 {_READ_CAP_BYTES} 字节）"


def _page_marker(offset: int, shown: int, total: int, size_note: str = "") -> str:
    """区间读取的进度标记。

    标记回答模型两个问题：这是第几到第几个字符、后面还有没有。

    `size_note` 非空表示"文件比读取上限长，我们只读了开头"：这时 `total` 只是**读到的那段**
    的长度，必须同时报出文件真实字节数，否则模型会把 `total` 当成整个文件的长度
    ——"工具说了不真的话"，正是 v1.0.1 要消灭的那类毛病（终局审查抓到过它的另一种形态：
    标记写在末尾被 truncate 砍掉，等于没写）。

    **调用方必须把它放在结果开头**：结果还要过 `Agent` 那层只保留前 8000 字符的截断。
    """
    head = f"…[第 {offset + 1}-{offset + shown} 字符，共 {total} 字符{size_note}"
    if offset + shown < total:
        head += _PAGE_TAIL
    return head + "]"


def _size_note(size: int) -> str:
    return _SIZE_NOTE.format(size=size)


async def _read_file(
    path: str, offset: int = 0, limit: int = MAX_RESULT_CHARS
) -> str:
    """读文件的**一个区间**。offset/limit 都按字符数（不是字节）。

    为什么要能翻页：实验里模型为 16848 字符的文件调了 3 次 `read_file`，够了却只读到
    开头一半——因为每次都从第 0 个字符开始。**线性读取没有游标时，重试同一个工具
    永远在原地打转。**

    实现是"整块解码 + 字符切片"，不是增量分块：字符语义的 offset 必须按字符数数过去，
    而 UTF-8 是变长的，真分块就得处理"块首块尾各残留半个字符"——那是解码回退逻辑的
    两倍复杂度，而 64 KiB 对本章节规模的文件是毫秒级。

    ponytail: 整块读进内存再切片；若真出现"读 1 GB 文件的第 900 MB 处"的需求，
              再改成流式解码 + 字节游标（那时才值得付分块解码的复杂度）。
    """
    target = Path(path).expanduser()
    if not target.is_file():
        return f"文件不存在：{target}"

    size = target.stat().st_size
    with target.open("rb") as handle:
        data = handle.read(_READ_CAP_BYTES)

    text = _decode_or_none(data)
    if text is None or (not text and data):
        # 后者只在"整块都是半个字符"时成立（文件小到连一个完整字符都装不下）。
        return f"无法按 UTF-8 解码（可能是二进制文件）：{target}"

    total = len(text)
    if offset >= total:
        # 翻过头了。超过内部上限的文件要在这里也说明"只读到开头"——否则模型翻到
        # 最后一页时看到的是干巴巴一句"共 24000 字符"，会以为**文件就这么长**，
        # 而实际上后面还有读不到的部分（页标记里有说明，这条消息里没有）。
        beyond = f"；文件共 {size} 字节，只读了开头 {_READ_CAP_BYTES} 字节" \
            if size > _READ_CAP_BYTES else ""
        return f"偏移 {offset} 超出文件长度（可读部分共 {total} 字符{beyond}）：{target}"

    limit = min(limit, MAX_RESULT_CHARS)   # 可以往小调，不能往大调
    note = _size_note(size) if size > _READ_CAP_BYTES else ""

    if not note and offset == 0 and total <= limit:
        # 整个文件一次给完了——没有"是哪一段""后面还有没有"可说的。
        # 这时候不加标记：标记只回答区间读取带来的疑问，不该给每一份小文件都套一层壳。
        return text

    # 标记**占额度**，而且额度从**总上限**里扣、不从模型要的 limit 里扣：
    #   * 从 limit 里扣的话，`limit=5` 会被标记吃光、返回空内容——模型要 5 个字就给它 5 个字；
    #   * 但总额度必须守住 MAX_RESULT_CHARS，因为 `Agent` 那层 `truncate()` 只保留这么多字符，
    #     超出的部分会被它砍掉，而页标记说的"到第 N 字符"就成了假话。
    #
    # 先按最长的那版标记估一个正文长度（不必迭代求解），再按真实标记修正一次：
    # 估算偏长会白白少给几个字，而**页的边界必须正好**——模型是拿标记里的数字
    # 去算下一个 offset 的，少给一个字就少读一个字（`test_the_page_marker_...` 守着这条）。
    longest = _page_marker(offset, total, total, note)
    budget = max(1, MAX_RESULT_CHARS - len(longest) - 1)          # -1 是标记后那个换行
    chunk = text[offset:offset + min(limit, budget)]
    if len(chunk) < limit:                                        # 还有额度就补满
        room = MAX_RESULT_CHARS - len(_page_marker(offset, len(chunk), total, note)) - 1
        chunk = text[offset:offset + min(limit, max(1, room))]
    result = f"{_page_marker(offset, len(chunk), total, note)}\n{chunk}"
    return result[:MAX_RESULT_CHARS]


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
            # 措辞与 agent.py 的 DEFAULT_SYSTEM_PROMPT **必须一致**：模型在两个不同时刻
            # 读到它们（描述每轮随 schema 下发，prompt 在对话最前面），说法不同就等着被
            # 误导。实测过一次真实的翻车：这里写"上次读到的字符数"（= 上一页的页宽）、
            # prompt 写"已读的字符数"（= 累计），模型照前者算就会**永远重读第二页**。
            description=(
                "读取一个文本文件的内容，可以指定读哪一段。offset 是起始字符位置"
                "（0 起算），limit 是最多返回的字符数（默认且最大 8000，开头的进度"
                "标记也算在内）。返回内容开头会标明这是第几到第几个字符、后面还有没有"
                "——文件比一次能读的长时，把 offset 设成已读的字符数接着往下读，"
                "不要重复读同一段；也没有能把上限调大的参数。"
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
