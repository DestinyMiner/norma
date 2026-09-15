import logging

import pytest

from pydantic import BaseModel, Field

from norma.agent import Agent, ExecResult
from norma.events import Failed, Finished, TextDelta, ToolCallStarted, ToolResult
from norma.llm import Reply, ToolCall
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


class FakeLLM:
    """照剧本走的假 LLM。script 里每个元素是某一轮的输出序列。"""

    def __init__(self, script):
        self.script = list(script)
        self.seen_messages: list[list[dict]] = []
        self.seen_tools: list[list[dict]] = []

    async def chat(self, messages, tools):
        self.seen_messages.append([dict(m) for m in messages])
        self.seen_tools.append(list(tools))
        for item in self.script.pop(0):
            yield item


def reply(text="", calls=()) -> list:
    items = [TextDelta(text)] if text else []
    items.append(Reply(content=text, tool_calls=list(calls)))
    return items


def call(call_id="c1", name="echo", args=None, args_error=""):
    return ToolCall(id=call_id, name=name, args=args or {}, args_error=args_error)


def scripted(script, tools=None, ask=allow, max_steps=25) -> Agent:
    agent = Agent(llm=FakeLLM(script), tools=tools or make_tools(),
                  ask_permission=ask, max_steps=max_steps)
    return agent


async def test_run_without_tool_calls_finishes():
    agent = scripted([reply("你好")])
    events = [e async for e in agent.run("hi")]

    assert isinstance(events[-1], Finished)
    assert events[-1].text == "你好"
    assert any(isinstance(e, TextDelta) and e.text == "你好" for e in events)


async def test_run_offers_the_injected_registry_to_the_model():
    """注入的 tools 必须真的进入模型可见的 schema——否则这个接口在骗人。

    没有这条断言，一个只查 self.tools、却把全局 TOOLS 的 schema 递给模型的实现
    会让全部测试照样通过，而模型被展示了它根本调不到的工具。
    """
    agent = scripted([reply("好")], tools=make_tools(Risk.READ))
    [event async for event in agent.run("hi")]

    offered = {schema["function"]["name"] for schema in agent.llm.seen_tools[0]}
    assert offered == {"echo", "boom"}


async def test_tool_result_is_backfilled_into_messages():
    agent = scripted([
        reply("", [call(name="echo", args={"text": "abc"})]),
        reply("完成"),
    ])

    events = [e async for e in agent.run("hi")]

    assert isinstance(events[-1], Finished)
    assert [m["role"] for m in agent.messages] == ["user", "assistant", "tool", "assistant"]
    tool_msg = agent.messages[2]
    assert tool_msg["tool_call_id"] == "c1"
    assert tool_msg["content"] == "echo:abc"


async def test_assistant_message_is_appended_before_tool_result():
    """不追加含 tool_calls 的 assistant 消息，模型下一轮会重复调用同一工具。"""
    agent = scripted([
        reply("", [call()]),
        reply("完成"),
    ])
    [e async for e in agent.run("hi")]

    assistant = agent.messages[1]
    assert assistant["role"] == "assistant"
    assert assistant["tool_calls"][0]["function"]["name"] == "echo"


async def test_tool_call_started_and_result_are_paired():
    agent = scripted([
        reply("", [call("c1"), call("c2")]),
        reply("完成"),
    ])
    events = [e async for e in agent.run("hi")]

    started = [e.call_id for e in events if isinstance(e, ToolCallStarted)]
    results = [e.call_id for e in events if isinstance(e, ToolResult)]
    assert started == ["c1", "c2"]
    assert results == started


async def test_failed_tool_call_still_gets_a_paired_result():
    agent = scripted([reply("", [call(name="nope")]), reply("好吧")])
    events = [e async for e in agent.run("hi")]

    started = [e for e in events if isinstance(e, ToolCallStarted)]
    results = [e for e in events if isinstance(e, ToolResult)]
    assert len(started) == len(results) == 1
    assert results[0].ok is False


async def test_unknown_tool_is_backfilled_and_loop_continues():
    agent = scripted([reply("", [call(name="move_file")]), reply("改用别的办法")])
    events = [e async for e in agent.run("hi")]

    assert isinstance(events[-1], Finished)
    assert "没有名为 move_file 的工具" in agent.messages[2]["content"]


async def test_malformed_args_json_is_backfilled():
    agent = scripted([
        reply("", [call(args_error="arguments 不是合法 JSON")]),
        reply("重试"),
    ])
    events = [e async for e in agent.run("hi")]

    assert isinstance(events[-1], Finished)
    assert "参数错误" in agent.messages[2]["content"]


async def test_invalid_params_are_backfilled():
    agent = scripted([
        reply("", [call(args={"text": 123})]),
        reply("重试"),
    ])
    events = [e async for e in agent.run("hi")]

    assert isinstance(events[-1], Finished)
    assert "参数错误" in agent.messages[2]["content"]


async def test_permission_denial_is_backfilled():
    agent = scripted(
        [reply("", [call(args={"text": "x"})]), reply("那算了")],
        tools=make_tools(Risk.WRITE),
        ask=deny,
    )
    events = [e async for e in agent.run("hi")]

    assert isinstance(events[-1], Finished)
    assert agent.messages[2]["content"] == "用户拒绝了这次操作"


async def test_read_tool_does_not_trigger_permission_prompt():
    asked = []

    async def ask(tool: Tool, args: dict) -> bool:
        asked.append(tool.name)
        return True

    agent = scripted([reply("", [call()]), reply("完成")],
                     tools=make_tools(Risk.READ), ask=ask)
    [e async for e in agent.run("hi")]

    assert asked == []


async def test_multiple_rounds_of_tool_calls():
    agent = scripted([
        reply("", [call("c1", args={"text": "一"})]),
        reply("", [call("c2", args={"text": "二"})]),
        reply("都做完了"),
    ])
    events = [e async for e in agent.run("hi")]

    assert isinstance(events[-1], Finished)
    assert [m["role"] for m in agent.messages] == [
        "user", "assistant", "tool", "assistant", "tool", "assistant"]


async def test_exceeding_max_steps_yields_failed():
    # 每轮都调工具，永远不结束
    agent = scripted([reply("", [call(f"c{i}")]) for i in range(10)], max_steps=3)
    events = [e async for e in agent.run("hi")]

    assert isinstance(events[-1], Failed)
    assert "最大步数 3" in events[-1].reason


async def test_llm_failure_yields_failed():
    """spec §7 决定 3：API 失败是预期内失败，必须变成 Failed 事件而不是抛穿。"""

    class ExplodingLLM:
        async def chat(self, messages, tools):
            raise ConnectionError("连不上")
            yield  # 让这个方法成为异步生成器

    agent = Agent(
        llm=ExplodingLLM(),
        ask_permission=allow,
        tools=make_tools(),
        max_steps=3,
    )
    events = [event async for event in agent.run("hi")]

    assert isinstance(events[-1], Failed)
    assert "模型调用失败" in events[-1].reason
    assert "连不上" in events[-1].reason


async def test_finished_text_is_not_duplicated_into_messages_twice():
    agent = scripted([reply("答案")])
    [e async for e in agent.run("hi")]

    assert [m["role"] for m in agent.messages] == ["user", "assistant"]
    assert agent.messages[1]["content"] == "答案"


async def test_schema_generation_failure_raises_instead_of_masquerading():
    """schema 生成是我们自己的代码 bug，必须抛出，不能被伪装成"模型调用失败"。

    伪装会同时造成两件坏事：病史被藏起来，而且 Failed 事件会让调用方以为
    是网络问题去重试。
    """

    class ExplodingParams(BaseModel):
        @classmethod
        def model_json_schema(cls, *args, **kwargs):
            raise RuntimeError("schema 生成失败")

    async def _noop() -> str:
        return ""

    tools = {"bad": Tool("bad", "坏工具", ExplodingParams, Risk.READ, _noop)}
    agent = scripted([reply("好")], tools=tools)

    with pytest.raises(RuntimeError, match="schema 生成失败"):
        [event async for event in agent.run("hi")]
