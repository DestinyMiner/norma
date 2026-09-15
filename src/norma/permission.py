"""权限闸门：缝留好，策略最简。

策略与机制分家。以后把「终端 y/n」换成「规则文件」或「手机远程确认」，
改的是本文件，``agent.py`` 一行不动。

**最终判断必须在本地。** 云端可以分发默认策略模板，但不得做裁决——
否则断网助手就瘫痪，且用户无法审计自己的安全边界。
"""
from __future__ import annotations

from typing import Awaitable, Callable

from .tools import Risk, Tool

AskPermission = Callable[[Tool, dict], Awaitable[bool]]

POLICY: dict[Risk, bool] = {  # True = 执行前必须问
    Risk.READ: False,
    Risk.WRITE: True,
    Risk.EXEC: True,
}


async def check(tool: Tool, args: dict, ask: AskPermission) -> str | None:
    """None = 放行；字符串 = 拒绝理由（会回填给模型）。"""
    if not POLICY[tool.risk]:
        return None
    if await ask(tool, args):
        return None
    return "用户拒绝了这次操作"
