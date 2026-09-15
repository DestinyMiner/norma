# norma Agent 内核实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 norma 的 agent 内核——把「用户输入 → 模型推理 → 工具调用 → 拦截校验 → 执行 → 结果回填 → 继续推理」这条循环做对、做稳，作为后续助手的底座。

**Architecture:** async generator 事件流内核。`Agent.run()` 是一个产出 `Event` 的异步生成器；LLM 适配、工具注册表、权限闸门全部通过构造参数注入，内核不读环境、不碰网络本身。CLI 只是消费事件流并实现 `ask_permission` 的最笨客户端。

**Tech Stack:** Python 3.11+、`openai` SDK（指向可配置 `base_url`）、`pydantic` v2、`python-dotenv`；测试用 `pytest` + `pytest-asyncio`。

**Spec:** `docs/superpowers/specs/2026-09-15-norma-agent-kernel-design.md`

## Global Constraints

- **Python 版本下限：3.11**（用到 `StrEnum`、`X | None` 语法）
- **依赖只允许这四个**：`openai`、`pydantic`、`python-dotenv`（dev: `pytest`、`pytest-asyncio`）。不得引入 agent 框架、CLI 框架、Schema 库
- **`src/` 布局**，包路径 `src/norma/`
- **内核不得读环境变量、不得直接创建网络客户端**——`Agent` 只接收构造好的 `llm` / `tools` / `ask_permission`
- **工具结果上限 8000 字符**（`MAX_RESULT_CHARS`）
- **`max_steps` 默认 25**，计的是模型回合数
- **六条失败路径（工具不存在 / arguments 非法 JSON / 权限拒绝 / 权限检查自身失败 / 参数校验失败 / 工具报错）一律转为结果回填，绝不中断循环**
- **权限检查失败必须 fail-closed**（按拒绝处理），不得默认放行
- **`ToolCallStarted` 与 `ToolResult` 严格一一配对**（同 `call_id`）
- **事件字段只用 JSON 原生类型**（str / int / float / bool / None / list / dict）
- **权限判断只能发生在本地**，不得设计成远端裁决
- 所有面向用户的报错文案用中文

---

## File Structure

| 文件 | 职责 |
|---|---|
| `pyproject.toml` | 包元数据、依赖、`norma` 入口点、pytest 配置 |
| `.env.example` | 配置模板 |
| `.gitignore` | 排除 `.env`、`norma.log`、缓存 |
| `src/norma/__init__.py` | 版本号 |
| `src/norma/events.py` | **协议**：5 种事件的 dataclass |
| `src/norma/config.py` | 从环境读配置 |
| `src/norma/llm.py` | openai SDK 适配器 + 增量拼接（`assemble`） |
| `src/norma/tools.py` | `Tool` / `Risk` / 注册表 / 4 个工具 / `truncate` / `audit` |
| `src/norma/permission.py` | 权限闸门：`AskPermission` / `POLICY` / `check` |
| `src/norma/agent.py` | **内核**：`Agent.execute` + `Agent.run` |
| `src/norma/cli.py` | CLI 客户端 + `norma` 入口 |
| `tests/test_events.py` | 事件序列化 |
| `tests/test_config.py` | 配置读取 |
| `tests/test_llm.py` | 增量拼接（全套重点） |
| `tests/test_tools.py` | 工具行为 |
| `tests/test_permission.py` | 闸门 |
| `tests/test_agent.py` | 循环 11 种情形 |

---

## Task 1: 项目脚手架 + 事件协议

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `.env.example`, `src/norma/__init__.py`, `src/norma/events.py`, `tests/test_events.py`

**Interfaces:**
- Consumes: 无（起点）
- Produces: `norma.events` 导出 `Event`（基类，含 `to_dict()`）、`TextDelta(text: str)`、`ToolCallStarted(call_id: str, name: str, args: dict)`、`ToolResult(call_id: str, name: str, ok: bool, content: str)`、`Finished(text: str)`、`Failed(reason: str)`

- [ ] **Step 1: 确认 Python 可用**

Run: `python --version`

Expected: `Python 3.11.x` 或更高。

若输出 Store 提示或 `9009`，说明仍是 Microsoft Store 别名桩——去「设置 → 应用 → 高级应用设置 → 应用执行别名」关闭 `python.exe` / `python3.exe`，或重装 Python 并勾选 Add to PATH。**此步不通过不要继续。**

- [ ] **Step 2: 创建 `pyproject.toml`**

```toml
[project]
name = "norma"
version = "0.1.0"
description = "个人 AI 助手基站的内核"
requires-python = ">=3.11"
dependencies = [
    "openai>=1.40",
    "pydantic>=2.0",
    "python-dotenv>=1.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-asyncio>=0.23"]

[project.scripts]
norma = "norma.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/norma"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

`asyncio_mode = "auto"` 让 async 测试函数无需 `@pytest.mark.asyncio` 装饰器。

- [ ] **Step 3: 创建 `.gitignore`**

```gitignore
.env
norma.log
__pycache__/
*.py[cod]
.pytest_cache/
*.egg-info/
dist/
build/
.venv/
```

- [ ] **Step 4: 创建 `.env.example`**

```dotenv
# 复制本文件为 .env 并填入真实密钥
NORMA_BASE_URL=https://api.deepseek.com
NORMA_API_KEY=sk-在这里填你的密钥
NORMA_MODEL=deepseek-chat
NORMA_MAX_STEPS=25
```

- [ ] **Step 5: 创建 `src/norma/__init__.py`**

```python
"""norma — 个人 AI 助手基站的内核。"""
__version__ = "0.1.0"
```

- [ ] **Step 6: 写失败的测试 `tests/test_events.py`**

```python
import json

import pytest

from norma.events import (
    Event, Failed, Finished, TextDelta, ToolCallStarted, ToolResult,
)

ALL_EVENTS = [
    TextDelta("你好"),
    ToolCallStarted("c1", "list_dir", {"path": "."}),
    ToolResult("c1", "list_dir", True, "a.txt"),
    Finished("做完了"),
    Failed("API 超时"),
]


def test_every_event_subclasses_event():
    for event in ALL_EVENTS:
        assert isinstance(event, Event)


def test_to_dict_carries_type_name_and_fields():
    assert TextDelta("hi").to_dict() == {"type": "TextDelta", "text": "hi"}


def test_tool_result_to_dict_shape():
    assert ToolResult("c1", "list_dir", True, "out").to_dict() == {
        "type": "ToolResult",
        "call_id": "c1",
        "name": "list_dir",
        "ok": True,
        "content": "out",
    }


@pytest.mark.parametrize("event", ALL_EVENTS, ids=lambda e: type(e).__name__)
def test_every_event_is_json_serializable(event):
    # 防的是「某天有人往事件里塞了个 Path 对象」——这类错直到接手机端才炸
    json.dumps(event.to_dict(), ensure_ascii=False)
```

- [ ] **Step 7: 运行测试，确认失败**

Run: `python -m pytest tests/test_events.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'norma'`

- [ ] **Step 8: 安装为可编辑包**

Run: `python -m pip install -e ".[dev]"`

Expected: `Successfully installed norma-0.1.0 ...`

- [ ] **Step 9: 运行测试，确认仍因缺实现而失败**

Run: `python -m pytest tests/test_events.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'norma.events'`

- [ ] **Step 10: 实现 `src/norma/events.py`**

```python
"""协议：内核往外播报的事件。

字段只用 JSON 原生类型，因为这条协议将被 Electron / Android / 鸿蒙 三个
语言各异的客户端消费。往事件里塞非原生类型（如 pathlib.Path）会让序列化
在客户端那一侧才炸。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class Event:
    """所有事件的基类。"""

    def to_dict(self) -> dict:
        return {"type": type(self).__name__, **asdict(self)}


@dataclass
class TextDelta(Event):
    """模型正在吐字。"""
    text: str


@dataclass
class ToolCallStarted(Event):
    """模型发起的工具调用（参数已拼完）。

    必定配对一条同 call_id 的 ToolResult——包括工具名不存在、参数非法、
    被权限拒绝这些失败情况。客户端只需维护一张 call_id → 卡片的表。
    """
    call_id: str
    name: str
    args: dict


@dataclass
class ToolResult(Event):
    """执行完毕。content 是回填给模型的原文（已截断）。"""
    call_id: str
    name: str
    ok: bool
    content: str


@dataclass
class Finished(Event):
    """模型不再调用工具，循环正常结束。"""
    text: str


@dataclass
class Failed(Event):
    """循环无法继续（API 失败、超步数）。"""
    reason: str
```

- [ ] **Step 11: 运行测试，确认通过**

Run: `python -m pytest tests/test_events.py -v`

Expected: PASS — 8 passed

- [ ] **Step 12: 提交**

```bash
git add pyproject.toml .gitignore .env.example src/norma/__init__.py src/norma/events.py tests/test_events.py
git commit -m "feat: 项目脚手架与事件协议"
```

---

## Task 2: 配置

**Files:**
- Create: `src/norma/config.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: 无
- Produces: `norma.config.Config`（frozen dataclass，字段 `base_url: str`、`api_key: str`、`model: str`、`max_steps: int`；类方法 `Config.from_env() -> Config`，缺 `NORMA_API_KEY` 时抛 `RuntimeError`）

- [ ] **Step 1: 写失败的测试 `tests/test_config.py`**

测试里 monkeypatch 掉 `load_dotenv`，避免开发机上真实存在的 `.env` 干扰断言。

```python
import pytest

from norma import config as config_module
from norma.config import Config


@pytest.fixture(autouse=True)
def no_dotenv(monkeypatch):
    """隔离真实 .env，让断言只依赖显式设置的环境变量。"""
    monkeypatch.setattr(config_module, "load_dotenv", lambda *a, **k: None)
    for key in ("NORMA_BASE_URL", "NORMA_API_KEY", "NORMA_MODEL", "NORMA_MAX_STEPS"):
        monkeypatch.delenv(key, raising=False)


def test_missing_api_key_raises_clear_error():
    with pytest.raises(RuntimeError, match="NORMA_API_KEY"):
        Config.from_env()


def test_reads_all_values_from_env(monkeypatch):
    monkeypatch.setenv("NORMA_API_KEY", "sk-test")
    monkeypatch.setenv("NORMA_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("NORMA_MODEL", "m1")
    monkeypatch.setenv("NORMA_MAX_STEPS", "7")

    cfg = Config.from_env()

    assert cfg.api_key == "sk-test"
    assert cfg.base_url == "https://example.com/v1"
    assert cfg.model == "m1"
    assert cfg.max_steps == 7


def test_defaults_when_only_api_key_present(monkeypatch):
    monkeypatch.setenv("NORMA_API_KEY", "sk-test")

    cfg = Config.from_env()

    assert cfg.base_url == "https://api.deepseek.com"
    assert cfg.model == "deepseek-chat"
    assert cfg.max_steps == 25


def test_blank_api_key_is_treated_as_missing(monkeypatch):
    monkeypatch.setenv("NORMA_API_KEY", "   ")
    with pytest.raises(RuntimeError, match="NORMA_API_KEY"):
        Config.from_env()
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python -m pytest tests/test_config.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'norma.config'`

- [ ] **Step 3: 实现 `src/norma/config.py`**

```python
"""配置：只从环境读，不含逻辑。"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_MAX_STEPS = 25


@dataclass(frozen=True)
class Config:
    base_url: str
    api_key: str
    model: str
    max_steps: int = DEFAULT_MAX_STEPS

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        api_key = os.environ.get("NORMA_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "缺少 NORMA_API_KEY。请复制 .env.example 为 .env 并填入密钥。"
            )
        return cls(
            base_url=os.environ.get("NORMA_BASE_URL", DEFAULT_BASE_URL).strip(),
            api_key=api_key,
            model=os.environ.get("NORMA_MODEL", DEFAULT_MODEL).strip(),
            max_steps=int(os.environ.get("NORMA_MAX_STEPS", str(DEFAULT_MAX_STEPS))),
        )
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `python -m pytest tests/test_config.py -v`

Expected: PASS — 4 passed

- [ ] **Step 5: 提交**

```bash
git add src/norma/config.py tests/test_config.py
git commit -m "feat: 从环境读取配置"
```

---

## Task 3: LLM 适配器与增量拼接

本任务包含整个实现中最容易写错的一段逻辑，必须严格 TDD。

**Files:**
- Create: `src/norma/llm.py`, `tests/test_llm.py`

**Interfaces:**
- Consumes: `norma.config.Config`
- Produces:
  - `norma.llm.ToolCall`（dataclass：`id: str`、`name: str`、`args: dict`、`args_error: str = ""`）
  - `norma.llm.Reply`（dataclass：`content: str = ""`、`tool_calls: list[ToolCall] = []`；方法 `to_message() -> dict`）
  - `norma.llm.assemble(chunks: list) -> Reply`（纯函数）
  - `norma.llm.LLM(config: Config)`；方法 `async chat(messages: list[dict], tools: list[dict]) -> AsyncIterator[TextDelta | Reply]`

- [ ] **Step 1: 写失败的测试 `tests/test_llm.py`**

用 `SimpleNamespace` 伪造 openai 的 chunk 对象——不需要网络，也不需要真的 SDK 对象。

```python
import json

from types import SimpleNamespace as NS

from norma.llm import Reply, ToolCall, assemble


def chunk(content=None, tool_calls=None):
    """伪造一个流式 chunk。tool_calls=None 表示这一片没有工具调用。"""
    return NS(choices=[NS(delta=NS(content=content, tool_calls=tool_calls))])


def tc(index, id=None, name=None, arguments=None):
    """伪造一个 tool_call 分片。真实 API 里，同一 index 的首片带 id 和 name，
    后续片这两项为 None，只有 arguments 继续追加。"""
    return NS(index=index, id=id, function=NS(name=name, arguments=arguments))


def test_text_only_stream():
    reply = assemble([chunk("你"), chunk("好"), chunk("！")])
    assert reply.content == "你好！"
    assert reply.tool_calls == []


def test_empty_choices_chunk_is_ignored():
    # 收尾 chunk 常常带 usage 且 choices 为空
    reply = assemble([chunk("嗨"), NS(choices=[])])
    assert reply.content == "嗨"
    assert reply.tool_calls == []


def test_single_tool_call_arriving_in_one_chunk():
    reply = assemble([
        chunk(tool_calls=[tc(0, id="c1", name="list_dir", arguments='{"path": "."}')]),
    ])
    assert len(reply.tool_calls) == 1
    assert reply.tool_calls[0] == ToolCall(id="c1", name="list_dir", args={"path": "."})


def test_tool_call_arguments_fragmented_across_chunks():
    """参数被拆成字符串碎片——这是真实 API 的常态。"""
    reply = assemble([
        chunk(tool_calls=[tc(0, id="c1", name="write_file", arguments='{"pa')]),
        chunk(tool_calls=[tc(0, arguments='th": "a.tx')]),
        chunk(tool_calls=[tc(0, arguments='t", "content": "哈"}')]),
    ])
    assert len(reply.tool_calls) == 1
    assert reply.tool_calls[0].args == {"path": "a.txt", "content": "哈"}
    assert reply.tool_calls[0].args_error == ""


def test_later_chunks_have_no_id_or_name():
    """首片之后 id / name 为 None。写成赋值而非累加会把已取到的工具名清空。"""
    reply = assemble([
        chunk(tool_calls=[tc(0, id="c1", name="read_file", arguments='{"path"')]),
        chunk(tool_calls=[tc(0, id=None, name=None, arguments=': "x.txt"}')]),
    ])
    assert reply.tool_calls[0].name == "read_file"
    assert reply.tool_calls[0].id == "c1"
    assert reply.tool_calls[0].args == {"path": "x.txt"}


def test_two_tool_calls_interleaved_by_index():
    reply = assemble([
        chunk(tool_calls=[tc(0, id="c1", name="list_dir", arguments='{"path"')]),
        chunk(tool_calls=[tc(1, id="c2", name="read_file", arguments='{"pa')]),
        chunk(tool_calls=[tc(0, arguments=': "."}')]),
        chunk(tool_calls=[tc(1, arguments='th": "b.txt"}')]),
    ])
    assert [c.id for c in reply.tool_calls] == ["c1", "c2"]
    assert reply.tool_calls[0].args == {"path": "."}
    assert reply.tool_calls[1].args == {"path": "b.txt"}


def test_truncated_arguments_record_args_error():
    """max_tokens 截断会让 arguments 断在半截。"""
    reply = assemble([
        chunk(tool_calls=[tc(0, id="c1", name="write_file", arguments='{"path": "a.tx')]),
    ])
    assert reply.tool_calls[0].args_error != ""
    assert reply.tool_calls[0].args == {}


def test_empty_arguments_string_gives_empty_dict():
    reply = assemble([chunk(tool_calls=[tc(0, id="c1", name="noop", arguments="")])])
    assert reply.tool_calls[0].args == {}
    assert reply.tool_calls[0].args_error == ""


def test_to_message_serializes_arguments_as_json_string():
    reply = Reply(content="", tool_calls=[ToolCall(id="c1", name="t", args={"a": 1})])
    msg = reply.to_message()
    assert msg["role"] == "assistant"
    raw = msg["tool_calls"][0]["function"]["arguments"]
    assert isinstance(raw, str)
    assert json.loads(raw) == {"a": 1}


def test_to_message_keeps_empty_content_as_none():
    reply = Reply(content="", tool_calls=[ToolCall(id="c1", name="t", args={})])
    assert reply.to_message()["content"] is None


def test_reply_without_tool_calls_omits_the_key():
    """空 tool_calls 数组在部分服务端会被判为非法，干脆不带这个键。"""
    msg = Reply(content="你好").to_message()
    assert msg == {"role": "assistant", "content": "你好"}
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python -m pytest tests/test_llm.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'norma.llm'`

- [ ] **Step 3: 实现 `src/norma/llm.py`**

```python
"""openai SDK 适配器。

本模块是内核里唯一碰网络的地方，也是测试的注入接缝。
除了 ``assemble`` 之外没有真实逻辑——它把流式 chunk 拼成 Reply。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import AsyncIterator

from openai import AsyncOpenAI

from .config import Config
from .events import TextDelta


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict
    args_error: str = ""  # 非空 = arguments 不是合法 JSON


@dataclass
class Reply:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)

    def to_message(self) -> dict:
        """转成 OpenAI 的 assistant 消息。

        arguments 必须是 **JSON 字符串**而不是 dict。塞 dict 是常见的静默
        错误：部分服务端宽容接受，直到换模型或参数含特殊字符时才报错。

        没有工具调用时不带 tool_calls 键——空数组在部分服务端会被判为非法。
        """
        message = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {
                        "name": c.name,
                        "arguments": json.dumps(c.args, ensure_ascii=False),
                    },
                }
                for c in self.tool_calls
            ]
        return message


@dataclass
class _Partial:
    """一个正在拼接中的 tool_call。"""
    id: str = ""
    name: str = ""
    args_json: str = ""


def assemble(chunks: list) -> Reply:
    """把流式 chunk 序列拼成一个 Reply。

    真实 API 会把 tool_call 的 arguments 拆成字符串碎片，按 index 分片下发；
    同一 index 的首片带 ``id`` 和 ``function.name``，后续片这两项为 None，
    只有 ``function.arguments`` 继续追加。因此这里必须：
      * 按 index 分别累积
      * 对 id / name 用条件累加，不能直接赋值（否则后续片的 None 会覆盖）
      * 全部收齐之后才 json.loads
    """
    content_parts: list[str] = []
    partials: dict[int, _Partial] = {}

    for chunk in chunks:
        choices = getattr(chunk, "choices", None)
        if not choices:
            continue
        delta = choices[0].delta
        if delta.content:
            content_parts.append(delta.content)
        for frag in (delta.tool_calls or []):
            partial = partials.setdefault(frag.index, _Partial())
            if frag.id:
                partial.id = frag.id
            func = frag.function
            if func is not None:
                if func.name:
                    partial.name += func.name
                if func.arguments:
                    partial.args_json += func.arguments

    calls: list[ToolCall] = []
    for index in sorted(partials):
        partial = partials[index]
        args: dict = {}
        args_error = ""
        if partial.args_json:
            try:
                parsed = json.loads(partial.args_json)
            except json.JSONDecodeError as exc:
                args_error = f"arguments 不是合法 JSON（{exc}）"
            else:
                if isinstance(parsed, dict):
                    args = parsed
                else:
                    args_error = f"arguments 不是 JSON 对象，而是 {type(parsed).__name__}"
        calls.append(ToolCall(
            id=partial.id, name=partial.name, args=args, args_error=args_error))

    return Reply(content="".join(content_parts), tool_calls=calls)


class LLM:
    def __init__(self, config: Config) -> None:
        self._client = AsyncOpenAI(
            base_url=config.base_url, api_key=config.api_key)
        self._model = config.model

    async def chat(
        self, messages: list[dict], tools: list[dict]
    ) -> AsyncIterator[TextDelta | Reply]:
        """先流式吐若干 TextDelta，最后吐一个 Reply。"""
        stream = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            tools=tools or None,
            stream=True,
        )
        collected = []
        async for chunk in stream:
            choices = getattr(chunk, "choices", None)
            if choices and choices[0].delta.content:
                yield TextDelta(choices[0].delta.content)
            collected.append(chunk)
        yield assemble(collected)
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `python -m pytest tests/test_llm.py -v`

Expected: PASS — 11 passed

- [ ] **Step 5: 提交**

```bash
git add src/norma/llm.py tests/test_llm.py
git commit -m "feat: LLM 适配器与 tool_call 增量拼接"
```

---

## Task 4: 工具机制 + `list_dir` + `read_file`

**Files:**
- Create: `src/norma/tools.py`, `tests/test_tools.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `norma.tools.Risk`（StrEnum：`READ` / `WRITE` / `EXEC`）
  - `norma.tools.Tool`（dataclass：`name: str`、`description: str`、`params: type[BaseModel]`、`risk: Risk`、`fn: Callable[..., Awaitable[str]]`）
  - `norma.tools.MAX_RESULT_CHARS = 8000`
  - `norma.tools.openai_schema(t: Tool) -> dict`
  - `norma.tools.truncate(text: str, limit: int = MAX_RESULT_CHARS) -> str`
  - `norma.tools.audit(name: str, args: dict, ok: bool, content: str) -> None`（写 `norma.audit` logger）
  - `norma.tools.TOOLS: dict[str, Tool]`、`norma.tools.tools_schema() -> list[dict]`

- [ ] **Step 1: 写失败的测试 `tests/test_tools.py`**

```python
import json
import logging

import pytest

from norma.tools import (
    MAX_RESULT_CHARS, TOOLS, Risk, Tool, openai_schema, tools_schema, truncate,
)


# ---------- 机制 ----------

def test_openai_schema_shape():
    schema = openai_schema(TOOLS["list_dir"])
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "list_dir"
    assert schema["function"]["description"]
    assert schema["function"]["parameters"]["type"] == "object"


def test_tools_schema_covers_every_registered_tool():
    names = {s["function"]["name"] for s in tools_schema()}
    assert names == set(TOOLS)


def test_every_tool_params_model_has_a_schema():
    for name, tool in TOOLS.items():
        assert tool.params.model_json_schema(), name


def test_truncate_leaves_short_text_untouched():
    assert truncate("短") == "短"


def test_truncate_marks_long_text():
    out = truncate("x" * (MAX_RESULT_CHARS + 50))
    assert out.startswith("x" * 100)
    assert "已截断" in out
    assert "8050" in out


def test_audit_writes_to_norma_audit_logger(caplog):
    from norma.tools import audit
    with caplog.at_level(logging.INFO, logger="norma.audit"):
        audit("list_dir", {"path": "."}, True, "有 3 个文件")
    assert "list_dir" in caplog.text
    assert "有 3 个文件" in caplog.text


# ---------- list_dir ----------

async def test_list_dir_lists_files_and_dirs(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.txt").write_text("hi", encoding="utf-8")

    out = await TOOLS["list_dir"].fn(path=str(tmp_path))

    assert "[目录] sub/" in out
    assert "[文件] a.txt" in out


async def test_list_dir_reports_empty(tmp_path):
    assert await TOOLS["list_dir"].fn(path=str(tmp_path)) == "（空目录）"


async def test_list_dir_missing_path(tmp_path):
    out = await TOOLS["list_dir"].fn(path=str(tmp_path / "nope"))
    assert "不存在" in out


# ---------- read_file ----------

async def test_read_file_roundtrip(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("你好世界", encoding="utf-8")
    assert await TOOLS["read_file"].fn(path=str(f)) == "你好世界"


async def test_read_file_respects_max_bytes(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("0123456789", encoding="utf-8")
    assert await TOOLS["read_file"].fn(path=str(f), max_bytes=4) == "0123"


async def test_read_file_missing(tmp_path):
    out = await TOOLS["read_file"].fn(path=str(tmp_path / "nope.txt"))
    assert "不存在" in out


async def test_read_file_binary_is_reported_not_crashed(tmp_path):
    f = tmp_path / "b.bin"
    f.write_bytes(b"\xff\xfe\x00\x01")
    out = await TOOLS["read_file"].fn(path=str(f))
    assert "无法按 UTF-8 解码" in out
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python -m pytest tests/test_tools.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'norma.tools'`

- [ ] **Step 3: 实现 `src/norma/tools.py`（机制 + 两个只读工具）**

`run_powershell` 与 `write_file` 在 Task 5 加入，本步注册表只放两个工具。

```python
"""工具定义、注册表，以及 v1 的本机工具实现。"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Awaitable, Callable

from pydantic import BaseModel, Field

MAX_RESULT_CHARS = 8000

log = logging.getLogger("norma.audit")


class Risk(StrEnum):
    READ = "read"
    WRITE = "write"
    EXEC = "exec"


@dataclass
class Tool:
    name: str
    description: str
    params: type[BaseModel]
    risk: Risk
    fn: Callable[..., Awaitable[str]]


def openai_schema(tool: Tool) -> dict:
    """pydantic 模型直接变成模型可用的 tool 定义，零手写。"""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.params.model_json_schema(),
        },
    }


def tools_schema() -> list[dict]:
    return [openai_schema(t) for t in TOOLS.values()]


def truncate(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    """工具输出会原样进入模型上下文，必须设硬上限。"""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[已截断，原文 {len(text)} 字符]"


def audit(name: str, args: dict, ok: bool, content: str) -> None:
    """一个能在你机器上执行命令的助手，必须能事后查它干了什么。"""
    brief = content[:200].replace("\n", " ")
    log.info("tool=%s ok=%s args=%s result=%s", name, ok, args, brief)


# ---------- list_dir ----------

class ListDirParams(BaseModel):
    path: str = Field(".", description="要列出的目录路径")


async def _list_dir(path: str = ".") -> str:
    target = Path(path).expanduser()
    if not target.exists():
        return f"路径不存在：{target}"
    if not target.is_dir():
        return f"不是目录：{target}"
    lines = []
    for child in sorted(target.iterdir(), key=lambda c: (c.is_file(), c.name.lower())):
        if child.is_dir():
            lines.append(f"[目录] {child.name}/")
        else:
            lines.append(f"[文件] {child.name}  {child.stat().st_size} 字节")
    return "\n".join(lines) if lines else "（空目录）"


# ---------- read_file ----------

class ReadFileParams(BaseModel):
    path: str = Field(..., description="要读取的文件路径")
    max_bytes: int = Field(64000, description="最多读取的字节数")


async def _read_file(path: str, max_bytes: int = 64000) -> str:
    target = Path(path).expanduser()
    if not target.is_file():
        return f"文件不存在：{target}"
    data = target.read_bytes()[:max_bytes]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return f"无法按 UTF-8 解码（可能是二进制文件）：{target}"


TOOLS: dict[str, Tool] = {
    tool.name: tool
    for tool in [
        Tool(
            name="list_dir",
            description="列出目录下的文件和子目录。",
            params=ListDirParams,
            risk=Risk.READ,
            fn=_list_dir,
        ),
        Tool(
            name="read_file",
            description="读取一个文本文件的内容。",
            params=ReadFileParams,
            risk=Risk.READ,
            fn=_read_file,
        ),
    ]
}
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `python -m pytest tests/test_tools.py -v`

Expected: PASS — 13 passed

- [ ] **Step 5: 提交**

```bash
git add src/norma/tools.py tests/test_tools.py
git commit -m "feat: 工具机制与两个只读工具"
```

---

## Task 5: `write_file` 与 `run_powershell`

**Files:**
- Modify: `src/norma/tools.py`（追加两个工具及其注册项）
- Modify: `tests/test_tools.py`（追加测试）

**Interfaces:**
- Consumes: Task 4 的 `Tool` / `Risk` / `TOOLS` / `truncate`
- Produces: `TOOLS["write_file"]`（`WriteFileParams(path: str, content: str)`，risk `WRITE`）、`TOOLS["run_powershell"]`（`RunPowershellParams(command: str, timeout_s: int = 60)`，risk `EXEC`）

- [ ] **Step 1: 追加失败的测试到 `tests/test_tools.py`**

在文件末尾追加：

```python
# ---------- write_file ----------

async def test_write_file_creates_parent_directories(tmp_path):
    target = tmp_path / "a" / "b" / "c.txt"
    out = await TOOLS["write_file"].fn(path=str(target), content="内容")
    assert target.read_text(encoding="utf-8") == "内容"
    assert "已写入" in out


async def test_write_file_overwrites(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("旧", encoding="utf-8")
    await TOOLS["write_file"].fn(path=str(target), content="新")
    assert target.read_text(encoding="utf-8") == "新"


# ---------- run_powershell ----------

async def test_run_powershell_captures_stdout():
    out = await TOOLS["run_powershell"].fn(command="Write-Output hello")
    assert "hello" in out


async def test_run_powershell_handles_chinese_output():
    """中文 Windows 上 PowerShell 默认输出编码不是 UTF-8，会糊成乱码。"""
    out = await TOOLS["run_powershell"].fn(command='Write-Output "中文测试"')
    assert "中文测试" in out


async def test_run_powershell_reports_nonzero_exit_code():
    out = await TOOLS["run_powershell"].fn(command="exit 3")
    assert "退出码 3" in out


async def test_run_powershell_captures_stderr():
    out = await TOOLS["run_powershell"].fn(
        command='[Console]::Error.WriteLine("出错了")')
    assert "出错了" in out


async def test_run_powershell_timeout_kills_process():
    out = await TOOLS["run_powershell"].fn(
        command="Start-Sleep -Seconds 30", timeout_s=1)
    assert "超时" in out
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python -m pytest tests/test_tools.py -v -k "write_file or run_powershell"`

Expected: FAIL — `KeyError: 'write_file'`

- [ ] **Step 3: 在 `src/norma/tools.py` 中追加实现**

在文件顶部的 import 区补充：

```python
import asyncio
import shutil
```

在 `TOOLS` 定义之前插入：

```python
# ---------- write_file ----------

class WriteFileParams(BaseModel):
    path: str = Field(..., description="要写入的文件路径")
    content: str = Field(..., description="文件内容（覆盖写入）")


async def _write_file(path: str, content: str) -> str:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"已写入 {target}（{len(content)} 字符）"


# ---------- run_powershell ----------

# PowerShell 在中文 Windows 上默认按 GBK/UTF-16 输出，直接按 UTF-8 解码会糊。
_SHELL = shutil.which("pwsh") or shutil.which("powershell") or "powershell"

# 强制子进程按 UTF-8 输出，否则中文文件名与中文输出全是乱码。
_UTF8_PREAMBLE = "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"


class RunPowershellParams(BaseModel):
    command: str = Field(..., description="要执行的 PowerShell 命令")
    timeout_s: int = Field(60, description="超时秒数，超时会终止进程")


async def _run_powershell(command: str, timeout_s: int = 60) -> str:
    proc = await asyncio.create_subprocess_exec(
        _SHELL, "-NoProfile", "-NonInteractive", "-Command",
        f"{_UTF8_PREAMBLE} {command}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return f"命令超时（{timeout_s} 秒）已终止：{command}"

    parts = []
    text = stdout.decode("utf-8", "replace").rstrip()
    if text:
        parts.append(text)
    errtext = stderr.decode("utf-8", "replace").rstrip()
    if errtext:
        parts.append(f"[stderr]\n{errtext}")
    if proc.returncode:
        parts.append(f"[退出码 {proc.returncode}]")
    return "\n".join(parts) if parts else "（无输出）"
```

编码的处理有个已知天花板，标注一下：

```python
# ponytail: _UTF8_PREAMBLE 只设了 [Console]::OutputEncoding，它覆盖 PowerShell
#           cmdlet 与 .NET 的写入（Console.Error 与 Console.Out 共用该编码），
#           所以本文件的两个编码测试能过。但**原生命令**（如 cmd /c dir）走
#           自己的代码页，中文仍可能糊。真遇到时再让命令显式输出 UTF-8，
#           或按 [Text.Encoding]::GetEncoding(936) 解码。
```

在 `TOOLS` 字典的列表里追加两项：

```python
        Tool(
            name="write_file",
            description="写入文本文件，覆盖已有内容，父目录会自动创建。",
            params=WriteFileParams,
            risk=Risk.WRITE,
            fn=_write_file,
        ),
        Tool(
            name="run_powershell",
            description="在这台 Windows 电脑上执行 PowerShell 命令并返回输出。",
            params=RunPowershellParams,
            risk=Risk.EXEC,
            fn=_run_powershell,
        ),
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `python -m pytest tests/test_tools.py -v`

Expected: PASS — 20 passed

- [ ] **Step 5: 提交**

```bash
git add src/norma/tools.py tests/test_tools.py
git commit -m "feat: 写入文件与执行 PowerShell 工具"
```

---

## Task 6: 权限闸门

**Files:**
- Create: `src/norma/permission.py`, `tests/test_permission.py`

**Interfaces:**
- Consumes: `norma.tools.Tool`、`norma.tools.Risk`
- Produces: `norma.permission.AskPermission`（类型别名 `Callable[[Tool, dict], Awaitable[bool]]`）、`norma.permission.POLICY: dict[Risk, bool]`、`norma.permission.check(tool: Tool, args: dict, ask: AskPermission) -> str | None`（`None` = 放行，字符串 = 拒绝理由）

- [ ] **Step 1: 写失败的测试 `tests/test_permission.py`**

```python
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
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python -m pytest tests/test_permission.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'norma.permission'`

- [ ] **Step 3: 实现 `src/norma/permission.py`**

```python
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
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `python -m pytest tests/test_permission.py -v`

Expected: PASS — 5 passed

- [ ] **Step 5: 提交**

```bash
git add src/norma/permission.py tests/test_permission.py
git commit -m "feat: 权限闸门"
```

---

## Task 7: `Agent.execute()` 执行路径

**Files:**
- Create: `src/norma/agent.py`, `tests/test_agent.py`

**Interfaces:**
- Consumes: `norma.events`、`norma.llm.ToolCall`、`norma.permission.check`、`norma.tools.{Tool, TOOLS, truncate, audit}`
- Produces:
  - `norma.agent.ExecResult`（dataclass：`ok: bool`、`content: str`）
  - `norma.agent.Agent.__init__(self, llm, tools: dict[str, Tool], ask_permission, max_steps: int = 25)`
  - `norma.agent.Agent.execute(call: ToolCall) -> ExecResult`（**永不抛异常**）
  - `norma.agent.Agent.messages: list[dict]`

- [ ] **Step 1: 写失败的测试 `tests/test_agent.py`（本任务只写 execute 部分）**

```python
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
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python -m pytest tests/test_agent.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'norma.agent'`

- [ ] **Step 3: 实现 `src/norma/agent.py`（本任务只写 `execute`）**

```python
"""内核：agent 循环本体。

本模块不读环境变量、不创建网络客户端——只接收构造好的 llm / tools /
ask_permission。因此内核完全可测，无需任何环境准备。
"""
from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError

from . import permission
from .llm import ToolCall
from .permission import AskPermission
from .tools import TOOLS, Tool, audit, truncate

DEFAULT_MAX_STEPS = 25


@dataclass
class ExecResult:
    """执行层内部产物。不是协议事件 ToolResult——执行层不该知道事件的存在。"""
    ok: bool
    content: str


class Agent:
    def __init__(
        self,
        llm,
        ask_permission: AskPermission,
        tools: dict[str, Tool] | None = None,
        max_steps: int = DEFAULT_MAX_STEPS,
    ) -> None:
        self.llm = llm
        self.ask_permission = ask_permission
        self.tools = TOOLS if tools is None else tools
        self.max_steps = max_steps
        self.messages: list[dict] = []

    async def execute(self, call: ToolCall) -> ExecResult:
        """执行一次工具调用。**永不抛异常**——失败一律转为结果回填。

        包一层 _run 是为了保证审计一定落盘：每条返回路径都要记日志，
        散在各个 return 前迟早漏一个。
        """
        result = await self._run(call)
        audit(call.name, call.args, result.ok, result.content)
        return result

    async def _run(self, call: ToolCall) -> ExecResult:
        tool = self.tools.get(call.name)
        if tool is None:
            # 模型会幻觉出不存在的工具名，这是常规路径而非异常
            return ExecResult(False, f"没有名为 {call.name} 的工具")

        if call.args_error:
            return ExecResult(False, f"参数错误：{call.args_error}")

        try:
            denied = await permission.check(tool, call.args, self.ask_permission)
        except Exception as exc:  # noqa: BLE001
            # 权限检查自身失败必须**按拒绝处理**（fail-closed）。远端 ask 的
            # 网络错误若直接抛出去，会毁掉整个 run，且默认放行是危险的。
            denied = f"权限检查失败，已按拒绝处理：{exc}"
        if denied:
            return ExecResult(False, denied)

        try:
            validated = tool.params(**call.args)
        except ValidationError as exc:
            return ExecResult(False, f"参数错误：{exc}")

        try:
            output = await tool.fn(**validated.model_dump())
        except Exception as exc:  # noqa: BLE001 — 工具的任何异常都只该是一次失败
            return ExecResult(False, f"工具报错：{exc}")

        return ExecResult(True, truncate(output))
```

`run()` 在 Task 8 加入。`ask_permission` 是**必填**而非默认 `None`：让缺参数在构造时立刻炸，而不是等到第一次写文件时才炸。

**权限检查本身也要兜住。** 这是第六道防线，且默认必须是拒绝——远端 `ask` 的网络错误、CLI 的 `EOFError` 都会走这条路。Fail-closed 是安全默认值。

- [ ] **Step 4: 运行测试，确认通过**

Run: `python -m pytest tests/test_agent.py -v`

Expected: PASS — 13 passed

- [ ] **Step 5: 提交**

```bash
git add src/norma/agent.py tests/test_agent.py
git commit -m "feat: 工具执行路径（六条失败路径均回填）"
```

---

## Task 8: `Agent.run()` 循环本体

**Files:**
- Modify: `src/norma/agent.py`（追加 `run`）
- Modify: `tests/test_agent.py`（追加循环测试）

**Interfaces:**
- Consumes: Task 7 的 `Agent` / `ExecResult`
- Produces: `norma.agent.Agent.run(user_input: str) -> AsyncIterator[Event]`

- [ ] **Step 1: 追加失败的测试到 `tests/test_agent.py`**

先在 `tests/test_agent.py` 的 import 区补充这两行：

```python
from norma.events import Failed, Finished, TextDelta, ToolCallStarted, ToolResult
from norma.llm import Reply
```

然后在文件末尾追加：

```python
from norma.events import Failed, Finished, TextDelta, ToolCallStarted, ToolResult
from norma.llm import Reply


class FakeLLM:
    """照剧本走的假 LLM。script 里每个元素是某一轮的输出序列。"""

    def __init__(self, script):
        self.script = list(script)
        self.seen_messages: list[list[dict]] = []

    async def chat(self, messages, tools):
        self.seen_messages.append([dict(m) for m in messages])
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


async def test_finished_text_is_not_duplicated_into_messages_twice():
    agent = scripted([reply("答案")])
    [e async for e in agent.run("hi")]

    assert [m["role"] for m in agent.messages] == ["user", "assistant"]
    assert agent.messages[1]["content"] == "答案"
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python -m pytest tests/test_agent.py -v -k "run or paired or backfill or max_steps or multiple_rounds"`

Expected: FAIL — `AttributeError: 'Agent' object has no attribute 'run'`

- [ ] **Step 3: 在 `src/norma/agent.py` 的 `Agent` 类中追加 `run`**

```python
    async def run(self, user_input: str) -> AsyncIterator[Event]:
        """驱动循环，产出事件流。这是内核唯一的对外入口。"""
        self.messages.append({"role": "user", "content": user_input})

        for _ in range(self.max_steps):
            reply = None
            async for item in self.llm.chat(self.messages, tools_schema()):
                if isinstance(item, TextDelta):
                    yield item
                else:
                    reply = item

            if reply is None:
                yield Failed("模型没有返回任何内容")
                return

            # 必须回填含 tool_calls 的 assistant 消息，
            # 否则模型下一轮会重复调用同一工具
            self.messages.append(reply.to_message())

            if not reply.tool_calls:
                yield Finished(reply.content)
                return

            for tool_call in reply.tool_calls:
                yield ToolCallStarted(
                    tool_call.id, tool_call.name, tool_call.args)
                result = await self.execute(tool_call)
                yield ToolResult(
                    tool_call.id, tool_call.name, result.ok, result.content)
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result.content,
                })

        yield Failed(f"超过最大步数 {self.max_steps}")
```

同时把 `agent.py` 的 import 区**替换为**：

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator

from pydantic import ValidationError

from . import permission
from .events import Event, Failed, Finished, TextDelta, ToolCallStarted, ToolResult
from .llm import ToolCall
from .permission import AskPermission
from .tools import TOOLS, Tool, audit, tools_schema, truncate
```

- [ ] **Step 4: 运行全部测试，确认通过**

Run: `python -m pytest -v`

Expected: PASS — 全部通过，无 failure

- [ ] **Step 5: 提交**

```bash
git add src/norma/agent.py tests/test_agent.py
git commit -m "feat: agent 循环本体与事件流"
```

---

## Task 9: CLI 客户端与真机冒烟

**Files:**
- Create: `src/norma/cli.py`

**Interfaces:**
- Consumes: 全部前述模块
- Produces: `norma.cli.main()`（`norma` 命令入口，已在 Task 1 的 `pyproject.toml` 中登记）

- [ ] **Step 1: 实现 `src/norma/cli.py`**

```python
"""最笨的客户端：渲染事件流，并实现 ask_permission。

它不知道循环内部如何工作。以后 Electron 接上来时，把 render 里的 print
换成 websocket.send 即可，内核一行不动。
"""
from __future__ import annotations

import asyncio
import logging
import sys

from .agent import Agent
from .config import Config
from .events import Event, Failed, Finished, TextDelta, ToolCallStarted, ToolResult
from .llm import LLM
from .tools import Tool

LOG_PATH = "norma.log"


def setup_logging() -> None:
    logging.basicConfig(
        filename=LOG_PATH,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )


async def ask_in_terminal(tool: Tool, args: dict) -> bool:
    prompt = f"\n⚠ 允许执行 [{tool.risk}] {tool.name} 吗？\n  参数：{args}\n  输入 y 允许："
    answer = await asyncio.to_thread(input, prompt)
    return answer.strip().lower() == "y"


def render(event: Event) -> None:
    match event:
        case TextDelta(text=text):
            print(text, end="", flush=True)
        case ToolCallStarted(name=name):
            print(f"\n[调用 {name}]", flush=True)
        case ToolResult(name=name, ok=ok, content=content):
            mark = "✓" if ok else "✗"
            first_line = content.strip().splitlines()[0] if content.strip() else ""
            print(f"[{mark} {name}] {first_line[:200]}", flush=True)
        case Finished():
            # 正文已随 TextDelta 流式打印过，这里只收尾
            print()
        case Failed(reason=reason):
            print(f"\n[失败] {reason}", file=sys.stderr)


async def repl(agent: Agent) -> None:
    print(f"norma 已就绪（审计日志：{LOG_PATH}）。直接输入内容开始对话，空行退出。")
    while True:
        try:
            line = (await asyncio.to_thread(input, "\n你> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            return
        async for event in agent.run(line):
            render(event)


def main() -> None:
    setup_logging()

    try:
        config = Config.from_env()
    except RuntimeError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    agent = Agent(
        llm=LLM(config),
        ask_permission=ask_in_terminal,
        max_steps=config.max_steps,
    )

    try:
        asyncio.run(repl(agent))
    except KeyboardInterrupt:
        print()
```

- [ ] **Step 2: 验证无密钥时报错清晰（无需 API key 的确定性检查）**

Run: `$env:NORMA_API_KEY=""; norma`

Expected: 输出 `配置错误：缺少 NORMA_API_KEY。请复制 .env.example 为 .env 并填入密钥。`，退出码 1。

- [ ] **Step 3: 真机冒烟（需要真实 API key）**

先在仓库根目录创建 `.env`（从 `.env.example` 复制并填入密钥），然后：

Run: `norma`

输入：`列出我的下载文件夹里有哪些文件`

Expected 依次看到：

1. `[调用 list_dir]`（只读工具，**不弹权限询问**）
2. `[✓ list_dir] …` 结果摘要
3. 一段中文自然语言总结

再输入：`在我的桌面创建一个叫 norma-测试.txt 的文件，内容写"你好"`

Expected：弹出 `⚠ 允许执行 [write] write_file 吗？`，输入 `y` 后看到 `[✓ write_file]`，并且桌面上真的出现该文件。

再输入：`执行 PowerShell 命令 Get-Date`

Expected：弹出 `⚠ 允许执行 [exec] run_powershell 吗？`；输入 `n` 后模型应改口说明未执行（**验证拒绝路径真的回填给了模型**）。

- [ ] **Step 4: 检查审计日志**

Run: `Get-Content norma.log -Tail 20`

Expected：每次工具调用一行，含工具名、参数、结果摘要。

- [ ] **Step 5: 提交**

```bash
git add src/norma/cli.py
git commit -m "feat: CLI 客户端与权限询问"
```

---

## Task 10: README

**Files:**
- Modify: `README.md`（当前只有一行 `# norma`）

**Interfaces:**
- Consumes: 无
- Produces: 无

- [ ] **Step 1: 写 `README.md`**

```markdown
# norma

个人 AI 助手基站的**内核**：把「用户输入 → 模型推理 → 工具调用 → 拦截校验 →
执行 → 结果回填 → 继续推理 → 最终答案」这条循环做对、做稳。

上层客户端（Electron / Android / 鸿蒙）在其他仓库实现，通过事件协议连接本内核。
本仓库不含任何 UI。

设计文档：`docs/superpowers/specs/2026-09-15-norma-agent-kernel-design.md`

## 安装

需要 Python 3.11 或更高。

```bash
python -m pip install -e ".[dev]"
```

## 配置

复制 `.env.example` 为 `.env` 并填入密钥：

```dotenv
NORMA_BASE_URL=https://api.deepseek.com
NORMA_API_KEY=sk-...
NORMA_MODEL=deepseek-chat
NORMA_MAX_STEPS=25
```

`NORMA_BASE_URL` 指向任何 OpenAI 兼容端点，换模型只需改这里。

## 使用

```bash
norma
```

输入自然语言即可。读写文件与执行命令前会先询问（只读操作不询问）。
每次工具调用都记入 `norma.log`。

## 测试

```bash
python -m pytest
```

测试无需网络与 API 密钥——LLM 是注入的假实现。

## 加一个工具

1. 定义 pydantic 参数模型
2. 写一个 async 函数，返回字符串
3. 在 `src/norma/tools.py` 的 `TOOLS` 里加一行（含 `risk` 等级）

模型的 tool 定义由 pydantic 模型自动生成，无需手写 JSON Schema。
```

- [ ] **Step 2: 运行全部测试，确认仍然通过**

Run: `python -m pytest -v`

Expected: PASS

- [ ] **Step 3: 提交**

```bash
git add README.md
git commit -m "docs: README"
```

---

## 完成标准

- `python -m pytest` 全绿，且不需要网络与 API 密钥
- `norma` 能对话，能列目录、读写文件、执行 PowerShell
- 写文件与执行命令会先询问；拒绝后模型能改道而不是崩溃
- `norma.log` 有完整的工具调用审计
- 故意让模型调用一个不存在的工具，run 不崩，模型收到「没有名为 X 的工具」
