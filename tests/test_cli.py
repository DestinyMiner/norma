"""CLI 的权限询问。

cli.py 大部分是胶水不需要测试，但 ask_in_terminal 的异常语义不是胶水：
它决定了 Ctrl-C 是"拒绝这一次"还是"杀掉整个会话并让进程挂住"。
"""

import pytest

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
