from pydantic import BaseModel

from norma.permission import check
from norma.tools import Risk, Tool


class NoParams(BaseModel):
    pass


async def _noop() -> str:
    return ""


def make_tool(risk: Risk, name: str = "t") -> Tool:
    return Tool(name, "测试工具", NoParams, risk, _noop)


def recorder(answer: bool):
    """返回一个假的 ask，以及记录它被调用次数的列表。"""
    seen: list[str] = []

    async def ask(tool: Tool, args: dict) -> bool:
        seen.append(tool.name)
        return answer

    return ask, seen


async def test_read_is_allowed_without_asking():
    ask, seen = recorder(True)
    assert await check(make_tool(Risk.READ), {}, ask) is None
    assert seen == []


async def test_write_is_allowed_after_approval():
    ask, seen = recorder(True)
    assert await check(make_tool(Risk.WRITE), {}, ask) is None
    assert seen == ["t"]


async def test_write_is_denied_with_a_reason():
    ask, _ = recorder(False)
    assert await check(make_tool(Risk.WRITE, "run_powershell"), {}, ask) == (
        "用户拒绝了这次 run_powershell 调用。"
        "这不是工具坏了或环境不可用，而是用户这一次不允许。"
        "请改用其他工具或换个思路完成任务。"
    )


async def test_denial_reason_names_the_tool_and_offers_a_way_out():
    """拒绝理由的三个要素。

    实测教训：只说「用户拒绝了这次操作」时，模型理解成"这个工具/这台机器坏了"，
    连试 4 种命令、最后拿 `echo test` 试探环境——4 步烧在确认环境上。
    """
    ask, _ = recorder(False)
    reason = await check(make_tool(Risk.EXEC, "run_powershell"), {}, ask)

    assert "run_powershell" in reason            # ① 拒绝的确切对象
    assert "不是工具坏了或环境不可用" in reason    # ② 掐掉"环境故障"这个推理
    assert "改用其他工具" in reason               # ③ 给出去处，否则它会原地重试


async def test_exec_asks_too():
    ask, seen = recorder(True)
    assert await check(make_tool(Risk.EXEC), {}, ask) is None
    assert seen == ["t"]


async def test_ask_receives_tool_and_args():
    received = {}

    async def ask(tool: Tool, args: dict) -> bool:
        received["name"] = tool.name
        received["args"] = args
        return True

    await check(make_tool(Risk.WRITE), {"path": "a.txt"}, ask)
    assert received == {"name": "t", "args": {"path": "a.txt"}}
