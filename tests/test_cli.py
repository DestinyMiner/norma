"""CLI 的权限询问。

cli.py 大部分是胶水不需要测试，但 ask_in_terminal 的异常语义不是胶水：
工作线程自身抛出的 EOF / KeyboardInterrupt（stdin 被关闭之类）必须变成一次
"拒绝"，而不是杀掉整个会话；而真正的交互式 Ctrl-C 必须立刻退出、不能挂住
——挂住的原因与修法见 _read_line 的 docstring。
"""

from norma.cli import ask_in_terminal
from norma.tools import TOOLS


async def test_keyboard_interrupt_denies_instead_of_killing_the_session(monkeypatch):
    def boom(prompt: str) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", boom)
    assert await ask_in_terminal(TOOLS["write_file"], {"path": "x"}) is False


async def test_eof_denies(monkeypatch):
    def boom(prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", boom)
    assert await ask_in_terminal(TOOLS["run_powershell"], {"command": "x"}) is False


async def test_plain_y_approves(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: " y ")
    assert await ask_in_terminal(TOOLS["write_file"], {}) is True


async def test_read_line_uses_a_daemon_thread(monkeypatch):
    """守护线程不参与解释器退出的 join——这正是 Ctrl-C 后不再挂住的原因。

    asyncio.to_thread 用的是默认执行器的**非守护**线程（3.9 起），事件循环关闭时
    会 join 它们；线程卡在 input() 里时，进程就跟着卡住。
    """
    import threading

    seen: dict = {}

    def fake_input(prompt: str) -> str:
        seen["daemon"] = threading.current_thread().daemon
        return "y"

    monkeypatch.setattr("builtins.input", fake_input)

    from norma.cli import _read_line

    assert await _read_line("> ") == "y"
    assert seen["daemon"] is True


async def test_read_line_discards_a_late_answer_after_the_loop_is_gone(monkeypatch):
    """调用方取消后进程继续存活时，迟到的回答应当被静默丢弃。

    没有这层保护，被取消的提示符在工作线程返回时会在事件循环已关闭的
    loop 上 call_soon_threadsafe，Python 于是打印
    "RuntimeError: Event loop is closed" 的线程异常栈。
    """
    import asyncio
    import threading as _threading

    released = _threading.Event()
    entered = _threading.Event()

    def slow_input(prompt: str) -> str:
        entered.set()
        released.wait(5)
        return "y"

    monkeypatch.setattr("builtins.input", slow_input)

    from norma.cli import _read_line

    task = asyncio.ensure_future(_read_line("> "))
    await asyncio.to_thread(entered.wait, 5)   # 等 worker 真正进入 input()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # 循环仍然活着，但 future 已被取消——worker 稍后返回时必须不抛异常。
    released.set()
    await asyncio.sleep(0.3)

    assert task.cancelled()


def test_read_line_drops_a_late_answer_after_the_loop_is_closed(monkeypatch):
    """循环**已关闭**后，被取消的提示符迟到返回时不能在线程里抛异常。

    上一条测试里事件循环仍然活着，_finish 的 done 判断就足以吞掉迟到的回答——
    也就是说它守不住 deliver 的 try/except。真正会炸的是这条路径：循环已关闭，
    call_soon_threadsafe 直接抛 RuntimeError，工作线程于是打印线程异常栈。
    这正是将来远端客户端取消权限询问、而进程继续存活时的形态。
    """
    import asyncio
    import threading as _threading
    import time

    released = _threading.Event()
    entered = _threading.Event()

    def slow_input(prompt: str) -> str:
        entered.set()
        released.wait(5)
        return "y"

    monkeypatch.setattr("builtins.input", slow_input)

    unhandled: list = []
    monkeypatch.setattr(_threading, "excepthook", unhandled.append)

    from norma.cli import _read_line

    loop = asyncio.new_event_loop()
    try:
        task = loop.create_task(_read_line("> "))
        loop.run_until_complete(asyncio.sleep(0))  # 让协程把 worker 线程启动起来
        assert entered.wait(5)                     # worker 确已进入 input()
        task.cancel()
        try:
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            pass
    finally:
        loop.close()          # 关闭循环时 worker 仍卡在 input() 里

    released.set()            # worker 现在返回一个无人等待的回答
    time.sleep(0.3)

    assert unhandled == [], f"工作线程里抛出了未处理异常：{unhandled}"


def test_third_party_info_does_not_pollute_the_audit_log(tmp_path, monkeypatch):
    """httpx2 为每个 HTTP 请求打一条 INFO；根级别若为 INFO，审计日志就被传输层噪音淹没。

    用户查 norma.log 是为了知道助手到底做了什么，所以这条不是洁癖。
    """
    import logging

    from norma import cli

    monkeypatch.chdir(tmp_path)
    cli.setup_logging()

    assert logging.getLogger().level == logging.WARNING
    assert logging.getLogger("norma.audit").isEnabledFor(logging.INFO)
    assert not logging.getLogger("httpx2").isEnabledFor(logging.INFO)


def test_setup_logging_really_raises_the_root_level(tmp_path, monkeypatch):
    """上一条测试在 pytest 里会"因为错误的理由通过"——这条守的才是真正的行为。

    logging.basicConfig() 只要看到根 logger 上已有 handler 就**整个跳过**，
    level= 一并忽略；而 pytest 自己会往根上挂 handler（实测 4 个），根 logger 的
    默认级别又本来就是 WARNING。两者叠加的结果是：把 level 改回 INFO，上一条测试
    仍然全绿——它测的是 Python 的默认值，不是我们的配置。

    这里把根 handler 暂时摘掉，让 basicConfig 真正生效，也就是真实 CLI 进程里的
    情形（那里根上没有任何 handler）。用完在 finally 里原样恢复。
    """
    import logging

    from norma import cli

    monkeypatch.chdir(tmp_path)

    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    saved_audit = logging.getLogger("norma.audit").level

    root.handlers[:] = []          # 否则 basicConfig 直接跳过
    root.setLevel(logging.NOTSET)  # 清掉 WARNING 默认值，逼出真实配置
    try:
        cli.setup_logging()

        assert root.level == logging.WARNING
        assert logging.getLogger("norma.audit").isEnabledFor(logging.INFO)
        assert not logging.getLogger("httpx2").isEnabledFor(logging.INFO)
    finally:
        for handler in root.handlers[:]:
            root.removeHandler(handler)
            handler.close()
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        logging.getLogger("norma.audit").setLevel(saved_audit)


def test_piped_chinese_reaches_the_model_through_a_real_subprocess(tmp_path):
    """`echo "帮我整理目录" | norma` 必须真的把中文送到模型，而不是炸在编码上。

    这条**只能**起真子进程：pytest 里 sys.stdin 是 DontReadFromInput，没有
    reconfigure，任何单元断言都只会走到 except 分支，对真实路径什么也证明不了。

    缺陷形态：stdin 被重定向时 Python 按本地代码页（本机 GBK）+ surrogateescape
    解码，中文字节变成 \\udc95 这类代理转义，随后在 JSON 编码时抛
    UnicodeEncodeError——报出来却是"模型调用失败"，把人支去找网络和密钥，
    而实际上**一个请求都没发出去**。

    用本地 stub 端点（127.0.0.1，不出网、不需要真密钥）接住请求，断言请求真的
    发出去了，且消息里的中文原样到达。
    """
    import http.server
    import json
    import os
    import subprocess
    import sys
    import threading

    captured: list[bytes] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            captured.append(self.rfile.read(length))
            payload = json.dumps({
                "id": "1", "object": "chat.completion.chunk", "created": 0,
                "model": "m",
                "choices": [{"index": 0, "delta": {"content": "好的"},
                             "finish_reason": None}],
            })
            data = f"data: {payload}\n\ndata: [DONE]\n\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    env = dict(os.environ)
    env.pop("PYTHONIOENCODING", None)  # 不许靠这个拐杖蒙混过关
    env.update({
        "NORMA_API_KEY": "sk-fake",
        "NORMA_BASE_URL": f"http://127.0.0.1:{server.server_address[1]}/v1",
        "NORMA_MODEL": "m",
    })

    try:
        proc = subprocess.run(
            [sys.executable, "-c", "from norma.cli import main; main()"],
            input="列一下当前目录\n\n".encode("utf-8"),
            capture_output=True, env=env, cwd=tmp_path, timeout=60,
        )
    finally:
        server.shutdown()

    stderr = proc.stderr.decode("utf-8", "replace")
    assert "surrogates not allowed" not in stderr
    assert captured, f"一个请求都没发出去：{stderr}"
    assert json.loads(captured[0])["messages"][-1]["content"] == "列一下当前目录"
