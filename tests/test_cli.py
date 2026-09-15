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
