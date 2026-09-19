"""最笨的客户端：渲染事件流，并实现 ask_permission。

它不知道循环内部如何工作。以后 Electron 接上来时，把 render 里的 print
换成 websocket.send 即可，内核一行不动。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from pathlib import Path

from .agent import Agent
from .config import Config
from .events import Event, Failed, Finished, TextDelta, ToolCallStarted, ToolResult
from .llm import LLM
from .tools import Tool

# 审计日志的固定位置。**绝不能写进 CWD**：那会污染用户的工作目录，而且日志文件会
# 出现在 `list_dir` 的结果里被模型当成内容读（实测白烧一步）。
# 用 `~/.norma/` 而不是 Windows 专有的 `%APPDATA%`：两个平台同一条路径，代码里
# 不必出现平台分支（Path.home() 在 Windows 上就是 C:\Users\<name>）。
DEFAULT_LOG_PATH = Path.home() / ".norma" / "audit.log"


def log_path() -> Path:
    """解析审计日志路径。`NORMA_LOG_PATH` 是逃生舱，供测试与调试隔离用。

    在**调用时**读环境变量（不在 import 时），这样测试可以直接设它，
    不必重载模块。
    """
    override = os.environ.get("NORMA_LOG_PATH", "").strip()
    return Path(override) if override else DEFAULT_LOG_PATH


def setup_logging() -> Path:
    """配置审计日志，返回实际写入的路径（调用方要把它显示给用户）。

    不用 `logging.basicConfig()`：它只要看到根 logger 上已有 handler 就**整个跳过**，
    连 `level=` 一并忽略——于是行为取决于"别的库有没有先配过 logging"。
    显式构造 handler 让这件事变确定。
    """
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

    root = logging.getLogger()
    root.addHandler(handler)
    # 根日志只留 WARNING 以上：httpx2 会为**每个** HTTP 请求打一条 INFO，
    # 落进审计日志就会把"助手到底做了什么"淹在传输层噪音里——
    # 而查这份日志正是这个文件存在的理由。
    root.setLevel(logging.WARNING)
    # 我们自己的审计日志要 INFO，显式打开，否则会被上面的根级别一并挡掉。
    logging.getLogger("norma.audit").setLevel(logging.INFO)

    # stdin 也在内：输入被重定向（`echo "帮我整理目录" | norma`）时，Windows 按本地
    # 代码页（本机 GBK）解码并启用 surrogateescape，中文字节会变成 \udc95 这类代理转义，
    # 随后在 JSON 编码时炸掉——而且报出来是"模型调用失败"，把人支去找网络和密钥，
    # 实际病因在本地编码。stdout/stderr 的理由见下。
    # stdout 被重定向（`norma > out.txt`）时，Windows 按本地代码页（本机 GBK）编码，
    # 于是 ✓/✗/⚠ 以及工具输出里任何超出 GBK 的字符（emoji 文件名之类）都会让 print
    # 直接抛 UnicodeEncodeError 把程序打崩。显式改成 UTF-8 并允许替换：
    #   * 交互式终端：Python 本来就用 UTF-8 + WriteConsoleW，这里是空操作
    #   * 重定向：输出为 UTF-8，不再崩，符号也保留
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass  # 流被换成不支持 reconfigure 的对象时跳过，不能因此影响主流程
    return path


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

    被取消后工作线程仍卡在 ``input()`` 里；它**迟到**返回时，等待方早已不存在，
    这时的回答被静默丢弃，而不是在线程里抛异常（见 ``deliver``）。
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()

    def deliver(value: object) -> None:
        try:
            loop.call_soon_threadsafe(_finish, future, value)
        except RuntimeError:
            # 事件循环已关闭：没有等待方了，静默丢弃。
            # 触发场景是"调用方取消了 _read_line 的 await 但进程继续存活"，
            # 正是将来远端客户端取消权限询问时的形态。
            pass

    def worker() -> None:
        try:
            line = input(prompt)
        except BaseException as exc:  # noqa: BLE001 — 原样转交等待方判定
            deliver(exc)
        else:
            deliver(line)

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


async def repl(agent: Agent, log_file: Path) -> None:
    print(f"norma 已就绪（审计日志：{log_file}）。直接输入内容开始对话，空行退出。")
    while True:
        try:
            line = (await _read_line("\n你> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            return
        async for event in agent.run(line):
            render(event)


def main() -> None:
    log_file = setup_logging()

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
        asyncio.run(repl(agent, log_file))
    except KeyboardInterrupt:
        print()
