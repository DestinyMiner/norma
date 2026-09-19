import logging

import pytest

from pydantic import BaseModel, Field

from norma.agent import DEFAULT_SYSTEM_PROMPT, Agent, ExecResult
from norma.events import Failed, Finished, TextDelta, ToolCallStarted, ToolResult
from norma.llm import Reply, ToolCall
from norma.tools import MAX_RESULT_CHARS, TOOLS, Risk, Tool


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
    assert "用户拒绝了这次 echo 调用" in result.content
    assert "改用其他工具" in result.content


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


def scripted(script, tools=None, ask=allow, max_steps=25, system_prompt="") -> Agent:
    """照剧本跑的 agent。

    默认 `system_prompt=""`（内核关掉 system 消息）：下面绝大多数用例断言的是
    `messages` 的**对话形状**，插一条 system 消息只会把每个索引往后推一位、
    给每条断言加噪音。system prompt 自己的测试在文件末尾，那里显式开它。
    """
    agent = Agent(llm=FakeLLM(script), tools=tools or make_tools(),
                  ask_permission=ask, max_steps=max_steps,
                  system_prompt=system_prompt)
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


async def test_tool_result_is_backfilled_after_the_system_message():
    """system prompt 只改 `messages` 的开头，回填的**顺序**不受影响。

    与上一条分开写：上一条守着"回填顺序"（关掉 system 消息看对话形状），
    这一条守着"开着 system 消息时顺序照样对"——索引整体后移一位这件事
    由它钉住，而不是让上一条的断言跟着一起漂。
    """
    agent = scripted([
        reply("", [call(name="echo", args={"text": "abc"})]),
        reply("完成"),
    ], system_prompt=DEFAULT_SYSTEM_PROMPT)

    [e async for e in agent.run("hi")]

    assert [m["role"] for m in agent.messages] == [
        "system", "user", "assistant", "tool", "assistant"]
    assert agent.messages[3]["tool_call_id"] == "c1"
    assert agent.messages[3]["content"] == "echo:abc"


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
    # 回填文本会说清是哪个工具被拒、并指向别的办法——见 permission.check 的注释
    assert "用户拒绝了这次 echo 调用" in agent.messages[2]["content"]


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


async def test_max_steps_counts_assistant_rounds_not_tool_calls():
    """`max_steps` 计的是**模型回合**，不是工具调用次数。

    一轮里提两个工具调用，`max_steps=1` 就该在这一轮之后结束：本轮的工具照常执行、
    结果照常回填，然后因为用完了回合数而 Failed。按工具调用计数的实现会在这里红
    ——它会在第一个工具调用之后就把剩余的那个丢掉。
    """
    agent = scripted(
        [reply("", [call("c1"), call("c2")]), reply("不该走到这里")],
        max_steps=1,
    )
    events = [e async for e in agent.run("hi")]

    assert isinstance(events[-1], Failed)
    results = [e for e in events if isinstance(e, ToolResult)]
    assert [e.call_id for e in results] == ["c1", "c2"]   # 一轮里的两个都执行了
    assert [m["role"] for m in agent.messages] == [
        "user", "assistant", "tool", "tool"]


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


# ---------- system prompt ----------

async def test_default_agent_puts_the_system_prompt_first():
    """不做任何设置时，`messages[0]` 就该是 system 消息。

    这是内核提供的**默认**行为，不是客户端可选的装饰：默认值放在客户端的话，
    下一个客户端会再次漏掉它，"中文提问先用英文答"（backlog 摩擦表 2026-09-16）原样复现。
    """
    agent = scripted([reply("你好")], system_prompt=None)

    [e async for e in agent.run("hi")]

    assert agent.messages[0]["role"] == "system"
    assert agent.messages[0]["content"] == DEFAULT_SYSTEM_PROMPT


async def test_system_prompt_reaches_the_first_model_call():
    """钉住"契约真的发出去了"，而不只是"self.messages 里有一条"。

    只看 `agent.messages` 的话，一个把它插在**第一次 llm.chat 之后**的实现也能全绿
    ——那样第一轮回答依然没有语种约束，缺陷照旧。
    """
    agent = scripted([reply("你好")], system_prompt=None)
    [e async for e in agent.run("hi")]

    first_request = agent.llm.seen_messages[0]
    assert first_request[0] == {"role": "system", "content": DEFAULT_SYSTEM_PROMPT}
    assert first_request[-1]["role"] == "user"


async def test_system_prompt_is_inserted_once_across_many_runs():
    """多轮对话里只该有一条 system 消息，而且一直在最前面。

    它跟着 `messages` 一起重放，不该每轮插一条——那会让上下文线性膨胀，
    而 prefix 缓存也就废了。
    """
    agent = scripted([reply("一"), reply("二")], system_prompt=None)

    [e async for e in agent.run("hi")]
    [e async for e in agent.run("再来")]

    roles = [m["role"] for m in agent.messages]
    assert roles.count("system") == 1
    assert roles[0] == "system"
    assert roles == ["system", "user", "assistant", "user", "assistant"]


async def test_an_empty_system_prompt_really_means_none():
    """`""` 是"不要 system 消息"这个明确意图，`None` 才是"没表态、用默认"。

    两者混为一谈的话，调用方就没法关掉它——而"关掉"是测试与自定义客户端都会要的。
    """
    agent = scripted([reply("你好")], system_prompt="")

    [e async for e in agent.run("hi")]

    assert [m["role"] for m in agent.messages] == ["user", "assistant"]


async def test_a_custom_agent_system_prompt_is_used_instead_of_the_default():
    agent = scripted([reply("好")], system_prompt="只说苏州话。")

    [e async for e in agent.run("hi")]

    assert agent.messages[0]["content"] == "只说苏州话。"


async def test_system_message_is_pinned_to_index_zero_even_with_history():
    """客户端先垫了历史时，system 消息要插在**最前面**，而且历史一个字不动。

    这条守的是"判据看位置、不看存在"：`messages[0]` 是这条不变量本身。
    按"有没有 system 消息"来判断的实现，会在这里把默认 prompt 整个跳掉
    ——而中文提问先用英文答的缺陷正是靠这条默认值治的（将来 v2 恢复会话历史时
    就会撞上这个场景：加载的历史里没有 system 消息）。

    变异证明：把 `insert(0, ...)` 改成 `append(...)`，只有这条测试会红——
    空 `messages` 时两者碰巧一样，所以没有这条用例，这个变异能全绿存活。
    （调用**时机**在这条路径上不可观测：空列表上 `insert(0, ...)` 与先 append
    user 再 insert 得到同一条消息序列。别为它编一条测不出来的断言。）
    """
    agent = scripted([reply("三")], system_prompt=None)
    agent.messages = [
        {"role": "user", "content": "一"},
        {"role": "assistant", "content": "二"},
    ]

    [e async for e in agent.run("三")]

    assert [m["role"] for m in agent.messages] == [
        "system", "user", "assistant", "user", "assistant"]
    assert agent.messages[0]["content"] == DEFAULT_SYSTEM_PROMPT
    assert agent.messages[1]["content"] == "一"      # 历史没被动过
    assert agent.messages[-1]["content"] == "三"


async def test_a_client_supplied_system_message_is_not_overwritten():
    """`messages` 是唯一事实来源：客户端自己带的 system 消息优先，我们不再插默认的。

    带一条**空的**也算数——那是明确的"我自己管"。要关掉默认 prompt，正路是
    `Agent(system_prompt="")`，但客户端自己垫一条同样该生效。
    """
    agent = scripted([reply("好")], system_prompt=None)
    agent.messages = [{"role": "system", "content": "客户端规则"}]

    [e async for e in agent.run("hi")]

    assert [m["role"] for m in agent.messages] == ["system", "user", "assistant"]
    assert agent.messages[0]["content"] == "客户端规则"


# ---------- 跨模块：区间读取要真的能翻到第二页 ----------

def _read_agent(tools=None):
    return Agent(
        llm=None,
        tools=tools or {"read_file": TOOLS["read_file"]},
        ask_permission=allow,
        system_prompt="",
    )


async def test_read_file_size_marker_survives_the_truncation_layer(tmp_path):
    """**集成测试**：页标记必须真的到得了模型眼前。

    只测工具函数是不够的——工具输出还要过 `Agent` 那层 `truncate()`，它只保留前
    8000 字符。标记若拼在正文**末尾**，就永远被砍掉，于是模型看的还是
    "原文 64000 字符"并把它当成文件大小——这个功能只在它唯一该起作用的那种情况下失效。
    （v1.0.1 的终局审查抓到过这个：四个逐任务审查都只调了工具函数，没走 Agent。）

    这里走完整的 `Agent.execute`，断言模型**收到**的那条 tool 消息。
    """
    big = tmp_path / "big.txt"
    big.write_bytes(("x" * 100 + "\n").encode("utf-8") * 40000)   # 4 MB 量级

    result = await _read_agent().execute(
        ToolCall("c1", "read_file", {"path": str(big)}))

    assert result.ok is True
    assert result.content.startswith("…[第 1-")               # 标记在开头，活过了截断
    assert "字节" in result.content.split("\n", 1)[0]          # 且报的是文件真实大小


async def test_a_page_continues_exactly_where_the_last_one_stopped(tmp_path):
    """**核心端到端**：第二页的内容必须和第一页不同，而且确实是原文的后半段。

    这条直接对应实验里的失败形态：模型连调 3 次 `read_file`，三次都拿到同一段开头。
    单元测试能证明 `_read_file` 支持 offset，只有走 `Agent` 才能证明
    "模型把 offset 传进来 → 真的拿到新内容"这条链是通的——而且**过完截断层之后**
    仍成立（标记占额度那件事就是在这里才暴露的）。
    """
    text = "".join(f"{i:05d}-" + "字" * 94 + "\n" for i in range(0, 20000, 100))[:20000]
    f = tmp_path / "novel.txt"
    f.write_bytes(text.encode("utf-8"))
    agent = _read_agent()

    first = await agent.execute(ToolCall("c1", "read_file", {"path": str(f)}))
    first_chunk = first.content.split("\n", 1)[1]

    second = await agent.execute(
        ToolCall("c2", "read_file", {"path": str(f), "offset": len(first_chunk)}))
    second_chunk = second.content.split("\n", 1)[1]

    # 用锚点断言，不靠"第一页正好 8000 字"这种脆弱的数字
    assert "00000-" in first_chunk and "08000-" not in first_chunk
    assert "08000-" in second_chunk and "00000-" not in second_chunk
    assert second_chunk != first_chunk
    assert first_chunk + second_chunk == text[:len(first_chunk) + len(second_chunk)]
    assert "后面还有" in second.content.split("\n", 1)[0]


async def test_the_three_page_read_that_failed_in_the_experiment_now_works(tmp_path):
    """实验里的**原样复刻**：16848 字符的文件，按标记翻页三次，拼起来等于原文。

    实验记录（`_norma-experiment/report.txt:113`）：模型调了 3 次 `read_file`
    （3 × 8000 = 24000 > 16848，够覆盖全文），却只读到开头一半——因为三次都从
    第 0 个字符开始。修正后同样 3 次，但每次读的是**下一段**。

    这条和上一条的区别：它读**到底**，并把"拼起来 == 原文"作为断言——
    只证明"第二页不同"是不够的，翻页还可能跳字或重复。
    """
    text = "".join(f"{i:05d}-" + "字" * 94 + "\n" for i in range(0, 16848, 100))[:16848]
    f = tmp_path / "ch01-05.txt"
    f.write_bytes(text.encode("utf-8"))
    agent = _read_agent()

    collected, offset, pages = [], 0, 0
    while offset < len(text):
        result = await agent.execute(
            ToolCall(f"c{pages}", "read_file", {"path": str(f), "offset": offset}))
        chunk = result.content.split("\n", 1)[1]
        assert chunk, "空页会让循环永远转下去"
        collected.append(chunk)
        offset += len(chunk)
        pages += 1
        assert pages <= 5

    assert pages == 3
    assert "".join(collected) == text


async def test_reading_the_same_offset_twice_gives_the_same_page(tmp_path):
    """不带 offset 再读一次 = 又拿第一页——这正是实验里发生的事，现在它是**可解释的**。

    模型上一次不知道该传什么，只能一次次重复读开头。这条把那个行为钉成契约：
    不传 offset 就是从头开始（而不是"接着上次"）——所以它必须靠**标记里的数字**
    自己算出下一个 offset，prompt 里那句"把 offset 设成已读的字符数"就是在教这个。
    """
    text = "abc" * 5000
    f = tmp_path / "same.txt"
    f.write_bytes(text.encode("utf-8"))
    agent = _read_agent()

    first = await agent.execute(ToolCall("c1", "read_file", {"path": str(f)}))
    again = await agent.execute(ToolCall("c2", "read_file", {"path": str(f)}))

    assert first.content == again.content


def test_default_system_prompt_covers_the_observed_defects():
    """默认 prompt 的每一句都在治一个**观察到的**毛病，这条防止它被删空。

    四个观察：中文提问先用英文答、拿 run_powershell 去"找文件"白弹确认框、
    拿 max_bytes 的 6万/20万/40万试探上限、以及它已经做对的"拒绝编造"。
    """
    prompt = DEFAULT_SYSTEM_PROMPT

    assert "中文" in prompt            # ① 语种
    assert "read_file" in prompt       # ② 用对的工具，别拿 exec 去读文件
    assert "run_powershell" in prompt
    assert "8000" in prompt            # ③ 读取上限是硬事实，别再试参数
    assert "本次读到的" in prompt       #    且标记里的长度不是文件大小
    assert "offset" in prompt          # ④ 怎么翻页（v1.1.0 的能力，得让它知道）
    assert "不要重复读同一段" in prompt
    assert "编造" in prompt            # ⑤ 抗幻觉
    # 提示词是给模型读的纯文本，markdown 强调号只会变成两个多余字符
    assert "**" not in prompt
