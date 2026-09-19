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
    """None = 放行；字符串 = 拒绝理由（会回填给模型）。

    拒绝理由必须**指名是哪个工具**并**给出路**。实测教训（`_norma-experiment`）：
    回填一句「用户拒绝了这次操作」时，模型把它理解成"PowerShell 在这台机器上不可用"，
    于是连试 4 种命令、最后拿 `echo test` 去试探环境——**4 步烧在确认环境坏没坏上**。
    三个要素缺一不可：拒绝的确切对象、这不是环境故障、以及换个办法。
    """
    if not POLICY[tool.risk]:
        return None
    if await ask(tool, args):
        return None
    return (
        f"用户拒绝了这次 {tool.name} 调用。"
        f"这不是工具坏了或环境不可用，而是用户这一次不允许。"
        f"请改用其他工具或换个思路完成任务。"
    )
