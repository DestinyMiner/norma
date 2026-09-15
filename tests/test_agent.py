import logging

import pytest
from pydantic import BaseModel, Field

from norma.agent import Agent, ExecResult
from norma.llm import ToolCall
from norma.tools import Risk, Tool


class EchoParams(BaseModel):
    text: str = Field("hi", description="要回显的文本")


async def _echo(text: str = "hi") -> str:
    return f"echo:{text}"


class NoParams(BaseModel):
    pass


async def _boom() -> str:
    raise RuntimeError("炸了")


def make_tools(risk: Risk = Risk.READ) -> dict[str, Tool]:
    return {
        "echo": Tool("echo", "回声", EchoParams, risk, _echo),
        "boom": Tool("boom", "会炸的工具", NoParams, Risk.READ, _boom),
    }


async def allow(tool: Tool, args: dict) -> bool:
    return True


async def deny(tool: Tool, args: dict) -> bool:
    return False


def make_agent(tools=None, ask=allow, max_steps=25):
    return Agent(llm=None, tools=tools or make_tools(), ask_permission=ask,
                 max_steps=max_steps)


async def test_execute_success():
    result = await make_agent().execute(ToolCall("c1", "echo", {"text": "abc"}))
    assert result == ExecResult(True, "echo:abc")


async def test_execute_uses_param_defaults():
    result = await make_agent().execute(ToolCall("c1", "echo", {}))
    assert result == ExecResult(True, "echo:hi")


async def test_execute_unknown_tool_does_not_raise():
    result = await make_agent().execute(ToolCall("c1", "move_file", {"a": 1}))
    assert result.ok is False
    assert "没有名为 move_file 的工具" in result.content


async def test_execute_malformed_args_json_does_not_raise():
    call = ToolCall("c1", "echo", {}, args_error="arguments 不是合法 JSON")
    result = await make_agent().execute(call)
    assert result.ok is False
    assert "参数错误" in result.content


async def test_execute_malformed_args_skips_permission_prompt():
    """参数都不成立，没有让用户去批准一次注定失败的调用的道理。"""
    asked = []

    async def ask(tool: Tool, args: dict) -> bool:
        asked.append(tool.name)
        return True

    call = ToolCall("c1", "echo", {}, args_error="坏 JSON")
    await make_agent(tools=make_tools(Risk.WRITE), ask=ask).execute(call)
    assert asked == []


async def test_execute_invalid_params_does_not_raise():
    result = await make_agent().execute(ToolCall("c1", "echo", {"text": 123}))
    assert result.ok is False
    assert "参数错误" in result.content


async def test_execute_tool_exception_does_not_raise():
    result = await make_agent().execute(ToolCall("c1", "boom", {}))
    assert result.ok is False
    assert "工具报错" in result.content
    assert "炸了" in result.content


async def test_execute_permission_denied():
    result = await make_agent(tools=make_tools(Risk.WRITE), ask=deny).execute(
        ToolCall("c1", "echo", {"text": "x"}))
    assert result.ok is False
    assert result.content == "用户拒绝了这次操作"


async def test_execute_permission_check_failure_fails_closed():
    """远端 ask 断线、CLI 读到 EOF 都会走这条路——默认必须是拒绝。"""
    async def broken_ask(tool: Tool, args: dict) -> bool:
        raise ConnectionError("远端断了")

    result = await make_agent(
        tools=make_tools(Risk.WRITE), ask=broken_ask
    ).execute(ToolCall("c1", "echo", {}))
    assert result.ok is False
    assert "按拒绝处理" in result.content
    assert "远端断了" in result.content


async def test_execute_read_tool_never_asks():
    asked = []

    async def ask(tool: Tool, args: dict) -> bool:
        asked.append(tool.name)
        return True

    await make_agent(tools=make_tools(Risk.READ), ask=ask).execute(
        ToolCall("c1", "echo", {}))
    assert asked == []


async def test_execute_truncates_long_output():
    class BigParams(BaseModel):
        pass

    async def big() -> str:
        return "x" * 9000

    tools = {"big": Tool("big", "大输出", BigParams, Risk.READ, big)}
    result = await make_agent(tools=tools).execute(ToolCall("c1", "big", {}))
    assert result.ok is True
    assert len(result.content) < 9000
    assert "已截断" in result.content


async def test_execute_audits_every_outcome(caplog):
    with caplog.at_level(logging.INFO, logger="norma.audit"):
        await make_agent().execute(ToolCall("c1", "echo", {"text": "a"}))
        await make_agent().execute(ToolCall("c2", "nope", {}))
        await make_agent(tools=make_tools(Risk.WRITE), ask=deny).execute(
            ToolCall("c3", "echo", {}))

    assert caplog.text.count("tool=") == 3
