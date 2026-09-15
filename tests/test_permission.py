from pydantic import BaseModel

from norma.permission import check
from norma.tools import Risk, Tool


class NoParams(BaseModel):
    pass


async def _noop() -> str:
    return ""


def make_tool(risk: Risk) -> Tool:
    return Tool("t", "测试工具", NoParams, risk, _noop)


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
    assert await check(make_tool(Risk.WRITE), {}, ask) == "用户拒绝了这次操作"


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
