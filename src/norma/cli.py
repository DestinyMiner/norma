"""最笨的客户端：渲染事件流，并实现 ask_permission。

它不知道循环内部如何工作。以后 Electron 接上来时，把 render 里的 print
换成 websocket.send 即可，内核一行不动。
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading

from .agent import Agent
from .config import Config
from .events import Event, Failed, Finished, TextDelta, ToolCallStarted, ToolResult
from .llm import LLM
from .tools import Tool

LOG_PATH = "norma.log"


def setup_logging() -> None:
    logging.basicConfig(
        filename=LOG_PATH,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )
    # stdout 被重定向（`norma > out.txt`）时，Windows 按本地代码页（本机 GBK）编码，
    # 于是 ✓/✗/⚠ 以及工具输出里任何超出 GBK 的字符（emoji 文件名之类）都会让 print
    # 直接抛 UnicodeEncodeError 把程序打崩。显式改成 UTF-8 并允许替换：
    #   * 交互式终端：Python 本来就用 UTF-8 + WriteConsoleW，这里是空操作
    #   * 重定向：输出为 UTF-8，不再崩，符号也保留
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass  # 流被换成不支持 reconfigure 的对象时跳过，不能因此影响主流程


def _finish(future: asyncio.Future, value: object) -> None:
    # 主任务可能已被取消（future 已 done），此时静默丢弃即可。
    if not future.done():
        future.set_result(value)


async def _read_line(prompt: str) -> str:
    """在一个**守护线程**里读一行输入。

    这里不能用 ``asyncio.to_thread``：默认执行器的工作线程会被事件循环在关闭时
    join（Python 3.12+ 有 300 秒上限，3.11 无上限），而该线程卡在 ``input()`` 里
    等用户回车——于是 Ctrl-C 之后终端会僵住直到用户随手敲一下回车。
    实测：park 4 秒的工作线程会让 ``asyncio.run`` 的退出也拖满 4 秒。

    守护线程在解释器退出时被直接丢弃，不参与 join，因此 Ctrl-C 能立刻退出。
    代价是那次 Ctrl-C 会**中止本轮对话**（CancelledError 按取消语义向上传播，
    这是正确的），而不是把它变成一次"拒绝"——后者需要吞掉 CancelledError，
    那会破坏取消语义，不值得。stdin 被关闭（EOF）这类由工作线程自身抛出的异常
    仍会被 ask_in_terminal 转成拒绝。
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()

    def worker() -> None:
        try:
            line = input(prompt)
        except BaseException as exc:  # noqa: BLE001 — 原样转交等待方判定
            loop.call_soon_threadsafe(_finish, future, exc)
        else:
            loop.call_soon_threadsafe(_finish, future, line)

    threading.Thread(target=worker, daemon=True).start()

    result = await future
    if isinstance(result, BaseException):
        raise result
    return result


async def ask_in_terminal(tool: Tool, args: dict) -> bool:
    prompt = f"\n⚠ 允许执行 [{tool.risk}] {tool.name} 吗？\n  参数：{args}\n  输入 y 允许："
    try:
        answer = await _read_line(prompt)
    except (EOFError, KeyboardInterrupt):
        # stdin 被关闭或工作线程自身收到中断时按**拒绝**处理：不批准，
        # 也不让异常把整个会话带走。
        print()
        return False
    return answer.strip().lower() == "y"


def render(event: Event) -> None:
    match event:
        case TextDelta(text=text):
            print(text, end="", flush=True)
        case ToolCallStarted(name=name):
            print(f"\n[调用 {name}]", flush=True)
        case ToolResult(name=name, ok=ok, content=content):
            mark = "✓" if ok else "✗"
            first_line = content.strip().splitlines()[0] if content.strip() else ""
            print(f"[{mark} {name}] {first_line[:200]}", flush=True)
        case Finished():
            # 正文已随 TextDelta 流式打印过，这里只收尾
            print()
        case Failed(reason=reason):
            print(f"\n[失败] {reason}", file=sys.stderr)


async def repl(agent: Agent) -> None:
    print(f"norma 已就绪（审计日志：{LOG_PATH}）。直接输入内容开始对话，空行退出。")
    while True:
        try:
            line = (await asyncio.to_thread(input, "\n你> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            return
        async for event in agent.run(line):
            render(event)


def main() -> None:
    setup_logging()

    try:
        config = Config.from_env()
    except RuntimeError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    agent = Agent(
        llm=LLM(config),
        ask_permission=ask_in_terminal,
        max_steps=config.max_steps,
    )

    try:
        asyncio.run(repl(agent))
    except KeyboardInterrupt:
        print()
