"""最笨的客户端：渲染事件流，并实现 ask_permission。

它不知道循环内部如何工作。以后 Electron 接上来时，把 render 里的 print
换成 websocket.send 即可，内核一行不动。
"""
from __future__ import annotations

import asyncio
import logging
import sys

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


async def ask_in_terminal(tool: Tool, args: dict) -> bool:
    prompt = f"\n⚠ 允许执行 [{tool.risk}] {tool.name} 吗？\n  参数：{args}\n  输入 y 允许："
    try:
        answer = await asyncio.to_thread(input, prompt)
    except (EOFError, KeyboardInterrupt):
        # Ctrl-C（或 stdin 关闭）落在提问上时按**拒绝**处理，理由有三：
        #   1. 不批准——权限闸门的默认必须是拒绝，不是放行；
        #   2. KeyboardInterrupt 是 BaseException，会穿透内核所有 except Exception
        #      把整个会话带走，而用户的本意只是"这次不要执行"；
        #   3. to_thread 的工作线程仍卡在 input() 里，事件循环关闭时会 join 它，
        #      进程于是挂住直到用户再按一次回车。
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
