# norma Agent 内核设计

日期：2026-09-15
状态：已确认，待实现
范围：v1 = 内核先行（里程碑一）

## 1. 背景与目标

norma 是一个**常驻的个人 AI 助手基站**，目标是支撑日常办公，让助手能远程在这台电脑上执行任务，并提供语音输入输出。

`norma` 仓库只负责**内核**：把「用户输入 → 模型推理 → 工具调用 → 拦截校验 → 执行 → 结果回填 → 继续推理 → 最终答案」这条循环做对、做稳、可扩展。

上层客户端（Electron 桌面端、Android、鸿蒙 ArkTS）在**其他仓库**实现，通过协议连接 norma。本仓库不含任何 UI。

### 为什么内核要做成服务

Android 与鸿蒙上无法运行 Python，所以 norma 只能是一个跑在用户电脑上的常驻服务，各端是薄客户端。这决定了一条核心设计原则：

> **norma 对外的公共接口是「协议」，不是 Python API。**

三个语言各异的客户端消费同一份事件协议。协议一旦将就，三个端都得跟着将就。因此事件流的形状是本设计中最先钉死的部分。

### 客户端拓扑：三个端都是 norma 的对等客户端

一个必须先钉死的问题：**手机端对接的是 norma 内核，不是 Electron 桌面端。**

```
Electron ─┐
Android  ─┼─→ norma（常驻服务，持有工具与机器权限）→ 在电脑上执行任务
HarmonyOS ┘
```

Electron 没有特殊地位，它只是三个前端之一。

**为什么不能让手机绕道桌面端：**

1. **工具在 norma 手里，不在 Electron 手里。** 任务要在电脑上执行，能执行的只有 norma 进程本身。
2. **绕道意味着「必须开着桌面窗口」手机才能用。** GUI 进程成为单点依赖——关掉窗口、崩溃、未启动，远程能力全部消失。
3. **三个端消费同一份协议，正是把内核做成事件流的目的。** 若手机走 Electron，Electron 就得把协议再代理一遍——第二个语言里的第二份协议实现，外加双份连接管理。

**Electron 的真实职责是进程生命周期，不是数据通路**：拉起 norma（sidecar）、托盘图标、开机自启、崩溃重启。这是「**谁把 norma 启动起来**」，与「**谁转发 norma 的流量**」是两件事，不可混淆。

**由此引出 v1 的一条硬约束：**

> 会话状态（`Agent.messages`）属于内核，**不得存在于 `cli.py`**。

v1 中 CLI 进程恰好等于内核进程，很容易顺手把状态写进 CLI。但远端接入时，内核必须能脱离任何客户端独立存活——**客户端是可插拔的，会话不是**。当前模块划分已满足此约束（`cli.py` 只构造 `Agent` 并驱动它），实现时守住这条线即可。

**刻意推迟**：手机如何触达电脑（局域网 / Tailscale 之类组网 / 公网中继）。这是网络接入问题，不改变内核形状，留到做传输层时再定。

### v1 的成功标准

对着终端说「帮我整理下载文件夹」，它真的去做了——能列出目录、读文件、写文件、执行命令，并在多轮里自己推进。

## 2. 范围

### v1 做

- async generator 事件流内核
- 工具注册表 + 4 个本机工具
- 权限闸门（缝留好，策略最简）
- CLI 客户端（交互式 REPL）
- 审计日志
- 9 条自动化测试 + 1 次真机冒烟

### v1 不做（刻意推迟）

| 不做 | 什么时候做 |
|---|---|
| 网络传输层（WebSocket/HTTP）、鉴权 | 接入第一个远程客户端时 |
| 语音输入输出（ASR/TTS） | 内核稳定后 |
| 会话持久化（历史只存内存） | 需要跨进程恢复会话时 |
| token 用量统计 | 需要核算成本时 |
| 多 provider 抽象层 | 接入协议不同的第二家（如 Anthropic）时 |
| 规则文件 / 远端确认式权限策略 | 远程接入时（届时只改 `permission.py`） |
| 工具调用并发执行 | 串行成为可测量瓶颈时 |
| 上下文压缩 | 长会话触到窗口上限时 |

## 3. 技术选型与理由

**语言：Python 3.11+**

理由基于本项目的具体形态，而非泛泛的「生态好」：

1. **差异化能力是「在电脑上执行任务」**，这恰是 Python 生态最深、其他语言最浅的位置：`pywin32`（COM/WMI）、`uiautomation`、`pyautogui`、`openpyxl`、`python-docx`。纯 shell 操作别的语言也能做，但办公自动化很快越出这个范围，底座的能力上限会被语言生态直接封顶。
2. **语音的本地路径是 Python 的独角戏**：`faster-whisper` + `silero-vad` + `piper`/`edge-tts` 是既成路径。日常助手不应每句话都往返云端。
3. **工具注册表有更干净的答案**：pydantic 模型 → JSON Schema → 模型可用的 tool 定义，这条链类型安全、零手写，同时白送参数校验。

代价：前端客户端需另写，协议要手写一遍。但那是**一个薄客户端**，比用其他语言重写全部 OS 自动化与音频胶水便宜得多。

**模型接入**：`openai` SDK 指向可配置的 `base_url`。v1 以 DeepSeek 为主，**不做 provider 抽象层**——没有第二个实现就不要接口。换地址/换 key/换模型名是改配置，不是改代码。

## 4. 依赖

```
openai           # 传输层：流式响应、tool_call 增量解析
pydantic         # 工具参数校验 + schema 生成（openai 的传递依赖，不新增成本）
python-dotenv    # 读 .env，日常改配置不必 setx
--- dev ---
pytest
pytest-asyncio
```

**不新增其他依赖。** 不引入 agent 框架、不引入 Schema 库、不引入 CLI 框架（`argparse` 足够）。

关于 `python-dotenv` 的说明：stdlib 没有 `.env` 读取器，自己搓是重新发明。这是一个每天要用的工具，配置别扭就不会被用起来——这一条依赖是刻意的。

## 5. 文件结构

采用 `src/` 布局。扁平布局（`norma/norma/`）下从仓库根目录运行 `python` 时，`import norma` 拿到的是「当前目录碰巧同名的那份源码」而非已安装的包；两者不一致时会调试一个根本没在跑的版本。这个坑在打包给 Electron 做 sidecar 时会咬人。

```
norma/
├── pyproject.toml
├── .env.example
├── README.md
├── docs/superpowers/specs/2026-09-15-norma-agent-kernel-design.md
├── src/
│   └── norma/
│       ├── __init__.py
│       ├── config.py
│       ├── events.py
│       ├── llm.py
│       ├── tools.py
│       ├── permission.py
│       ├── agent.py
│       └── cli.py
└── tests/
    └── test_agent.py
```

## 6. 模块职责与依赖方向

| 模块 | 职责 | 关键约束 |
|---|---|---|
| `config.py` | 读环境变量：`base_url` / `api_key` / `model` / `max_steps` | 只做配置，不含逻辑 |
| `events.py` | **协议**：内核往外播报的 5 种事件 | 字段只用 JSON 原生类型 |
| `llm.py` | openai SDK 适配器 | 不认识工具实现、不认识权限 |
| `tools.py` | 工具定义 + 注册表 + 4 个工具实现 | 不认识循环 |
| `permission.py` | 权限缝：`AskPermission` 类型 + 最简策略 + `check()` | 不认识循环，不知道答案从哪来 |
| `agent.py` | **内核：循环本体** | 只依赖上述模块的接口 |
| `cli.py` | 最笨的客户端 | 只消费事件 + 实现 `ask_permission` |

**依赖方向单向向下，无回环**：

```
cli → agent → {llm, tools, permission} → events
cli → config ← llm
```

即：`agent.py` 既不读配置也不读环境，它只接收构造好的 `llm`、`tools`、`ask_permission`。因此每一层都可独立测试，内核无需任何环境准备。

`permission.py` 单独成文件（v1 仅约 15 行）是刻意的：**策略与机制必须分家**。以后把「终端 y/n」换成「规则文件」或「手机远程确认」，改的是这个文件，`agent.py` 一行不动。

### 配置项

```
NORMA_BASE_URL=https://api.deepseek.com
NORMA_API_KEY=sk-...
NORMA_MODEL=deepseek-chat
NORMA_MAX_STEPS=25
```

## 7. 事件协议

五条，一条不多：

```python
@dataclass
class TextDelta:        # 模型正在吐字
    text: str

@dataclass
class ToolCallStarted:  # 模型发起的工具调用（参数已拼完）；必定配对一条 ToolResult
    call_id: str
    name: str
    args: dict

@dataclass
class ToolResult:       # 执行完毕；content 是回填给模型的原文
    call_id: str
    name: str
    ok: bool
    content: str

@dataclass
class Finished:         # 模型不再调用工具，循环正常结束
    text: str

@dataclass
class Failed:           # 循环无法继续（API 失败、超步数）
    reason: str
```

### 设计决定

**1. 没有 `PermissionRequested` 事件。**

权限询问走注入的 `ask_permission` 可调用对象，不走事件流。理由：事件是**单向广播**，权限是**需要回话的**。客户端本来就必须实现「回应通道」这第二个接入点；与其在事件流里加一条、再另开一条回程通道，不如把「问一句并等答案」直接做成一个函数。少一个概念，少一处状态同步。

客户端若想在等待时渲染「等待授权中…」，就在自己的 `ask_permission` 实现里先渲染再 await。**UI 状态是客户端的事，不是协议的事。**

**2. `ToolCallStarted` 与 `ToolResult` 严格一一配对。**

模型发出的每一次工具调用都会产生一条 `ToolCallStarted`，随后**必定**有一条同 `call_id` 的 `ToolResult`——包括工具名不存在、参数非法、被权限拒绝这些失败情况。

反过来的设计（只在确定要执行时才发 `ToolCallStarted`）会产生**孤立的 `ToolResult`**：客户端收到一条指向它从未见过的 `call_id` 的结果，被迫处理「未知调用的结果」这种状态。配对不变式让客户端只需维护一张 `call_id → 卡片` 的表。

代价是客户端会看到模型拼错的调用。但那正是**真相**——助手确实尝试了，UI 应该显示它失败了，而不是假装什么都没发生。

**3. 预期内失败一律是 `Failed` 事件，不是异常。**

规则：

- API 超时/失败、超步数、鉴权失败 → `yield Failed` 然后 `return`
- 代码 bug（如工具名重复注册）→ 直接抛

客户端只需处理一种错误路径；调试时不会把 bug 误判为网络抖动。

**4. `ToolResult.ok` 与 `content` 并存。**

`content` 是回填给模型的原文，可能很长（已截断，见 §8）。客户端展示时自行决定截断，内核不替它决定。

### 序列化

`Event` 基类提供约 2 行的 `to_dict()`（`dataclasses.asdict` + 类型名）。这条协议的全部意义就是将来被三个语言的客户端消费，所以配一条测试断言每种事件都能 `json.dumps`——它防的是「某天有人往事件里塞了个 `Path` 对象」这类直到接手机端才炸的错。

## 8. 工具

### 形态

```python
class Risk(StrEnum):
    READ = "read"
    WRITE = "write"
    EXEC = "exec"

@dataclass
class Tool:
    name: str
    description: str
    params: type[BaseModel]                     # pydantic：既校验参数，又生成 schema
    risk: Risk
    fn: Callable[..., Awaitable[str]]
```

模型所需的 tool 定义零手写：

```python
def openai_schema(t: Tool) -> dict:
    return {"type": "function", "function": {
        "name": t.name,
        "description": t.description,
        "parameters": t.params.model_json_schema()}}
```

注册表是 `tools.py` 里一个模块级 `TOOLS: dict[str, Tool]`。**不加装饰器、不加插件机制**——尚未到需要它的规模。

加一个工具 = 定义 pydantic 模型 + 写 async 函数 + 在 `TOOLS` 里加一行。

### v1 的四个工具

| 工具 | 风险 | 说明 |
|---|---|---|
| `list_dir(path=".")` | read | 结构化列目录。看似可省，但模型极度依赖它定位；用 shell 代替效果明显变差 |
| `read_file(path, max_bytes=64000)` | read | 读文本，硬上限 |
| `write_file(path, content)` | write | 写/覆盖，自动创建父目录 |
| `run_powershell(command, timeout_s=60)` | exec | Windows 上的主力，也是最大的风险面 |

移动/复制/删除**不单独做工具**——`run_powershell` 已覆盖，且同属 write/exec 风险等级，多一个工具只多一处要维护的描述文案。

### 截断

工具输出会原样进入模型上下文。`dir /s` 一次能吐数 MB 直接撑爆窗口。**每个工具结果硬上限 8000 字符**，超出截断并标注。这是 `tools.py` 的职责，不是协议层的。

## 9. 执行路径（本设计的核心）

每次工具调用依次过四关。**四条失败路径全部变成结果回填，没有一条会中断循环**：

```python
@dataclass
class ExecResult:          # 内部类型，不是协议类型——勿与协议事件 ToolResult 混淆
    ok: bool
    content: str

async def execute(name, args, ask) -> ExecResult:
    result = await _run(name, args, ask)
    audit(name, args, result)                             # 每个调用都记一行，含失败与拒绝
    return result

async def _run(name, args, ask) -> ExecResult:
    tool = TOOLS.get(name)                                # ⓪ 工具存在性
    if tool is None:
        return ExecResult(False, f"没有名为 {name} 的工具")

    denied = await permission.check(tool, args, ask)      # ① 权限
    if denied:
        return ExecResult(False, denied)                  #    "用户拒绝了这次操作"

    try:
        validated = tool.params(**args)                   # ② 参数校验
    except ValidationError as e:
        return ExecResult(False, f"参数错误：{e}")

    try:
        out = await tool.fn(**validated.model_dump())     # ③ 执行
    except Exception as e:
        return ExecResult(False, f"工具报错：{e}")

    return ExecResult(True, truncate(out, 8000))          # ④ 截断
```

**⓪ 不是凑数的。** 模型会幻觉出不存在的工具名——它会调用 `move_file`、`search_web` 这类它认为「应该有」的工具。若不拦截，`TOOLS[name]` 直接 `KeyError` 崩掉整个 run。必须把它当成常规路径而非异常。

**`ExecResult` 与协议事件 `ToolResult` 是两个东西**：前者是执行层内部产物，后者是发给客户端的播报（含 `call_id`、`name`）。执行层不该知道事件的存在，`agent.run` 负责把前者包装成后者。

`execute` 包一层 `_run` 是为了保证**审计一定落盘**——四条返回路径都要记日志，散在四个 `return` 前迟早漏一个。

模块归属：`execute` 是 `Agent` 的方法（`agent.py`）；`audit` 与 `truncate` 是 `tools.py` 里的辅助函数。

**这是 agent 能自愈的全部秘密。** 模型看到「参数错误」会重试，看到「用户拒绝了」会换个办法，看到「没有这个工具」会改用现有的。若此处任何一处 `raise` 出去，一次拼错字段名就整个 run 崩掉——而这在真实使用中是每天都会发生的事。

## 10. 权限闸门

```python
AskPermission = Callable[[Tool, dict], Awaitable[bool]]

POLICY: dict[Risk, bool] = {          # True = 执行前必须问
    Risk.READ:  False,
    Risk.WRITE: True,
    Risk.EXEC:  True,
}

async def check(tool, args, ask) -> str | None:
    """None = 放行；字符串 = 拒绝理由（会回填给模型）"""
    if not POLICY[tool.risk] or await ask(tool, args):
        return None
    return "用户拒绝了这次操作"
```

契约一句话：**`None` 放行，字符串是拒绝理由**。不引入 `Decision` 类，不引入策略引擎。

**关于取值的设计意图**：缝留好、策略最简。v1 在用户自己的电脑上、对着本地终端使用，风险低；真正的风险从「能被远程唤起」那一刻开始指数上升。但闸门本身是循环的必经之路，属于内核，必须现在就在。以后换规则文件或远程确认，改的是这个函数和它上面那张表。

**这张表的最终形态是「声明式数据 + 本地覆盖」。** v1 硬编码（三个工具，为读文件写策略没有意义），但必须清楚它迟早变成读一份本地 YAML/JSON，且用户能在本地覆盖默认值。原因不是灵活，是安全性：权限边界必须**本地可读、可审计、可修改**。任何形式的「云端返回 allow/deny」都是错的设计（见 §16）。

`ask` 的两个实现（说明缝的价值）：

```python
# v1：终端
async def ask_in_terminal(tool, args) -> bool:
    answer = await asyncio.to_thread(
        input, f"要执行 {tool.name} {args}? (y/n) ")
    return answer.strip().lower() == "y"

# 将来：远程（agent.py 一行不改）
async def ask_over_websocket(tool, args) -> bool:
    await ws.send(json.dumps({"type": "permission_request",
                              "tool": tool.name, "args": args}))
    reply = await ws.recv()
    return json.loads(reply)["allow"]
```

## 11. 内核循环

```python
async def run(self, user_input: str) -> AsyncIterator[Event]:
    self.messages.append({"role": "user", "content": user_input})
    for _ in range(self.max_steps):
        reply = None
        async for item in self.llm.chat(self.messages, self.tools_schema):
            if isinstance(item, TextDelta):
                yield item
            else:
                reply = item                       # Reply（含 content + tool_calls）
        if reply is None:
            yield Failed("模型没有返回任何内容")
            return

        self.messages.append(reply.to_message())   # assistant 消息（含 tool_calls）
        if not reply.tool_calls:
            yield Finished(reply.content)
            return

        for call in reply.tool_calls:
            yield ToolCallStarted(call.id, call.name, call.args)
            result = await self.execute(call.name, call.args)
            yield ToolResult(call.id, call.name, result.ok, result.content)
            self.messages.append({                 # tool 结果回填
                "role": "tool",
                "tool_call_id": call.id,
                "content": result.content,
            })

    yield Failed(f"超过最大步数 {self.max_steps}")
```

要点：

- `max_steps` 计的是**模型回合数**，不是工具调用数。默认 25。
- 每轮必须把 assistant 消息（含 `tool_calls`）追加进 `messages`，否则模型下一轮会重复调用同一工具。
- 四条已知失败路径（工具不存在、权限拒绝、参数非法、工具报错）均在 `execute` 内转为结果回填，不中断循环（见 §9）。
- 每个 `ToolCallStarted` 必定配对一条 `ToolResult`（见 §7 决定 2）。
- **多工具串行执行**。OpenAI 兼容接口可以一次返回多个 `tool_calls`，v1 串行处理：顺序确定，权限询问不会同时弹两个。这是一处刻意简化：

```python
# ponytail: 多工具串行执行；若并发成为可测量瓶颈，改为 asyncio.gather，
#           并注意权限询问需排队，否则会同时弹出多个确认框
```

### LLM 适配层

`llm.chat()` 是 async generator，先吐若干 `TextDelta`，最后吐一个 `Reply`：

```python
@dataclass
class ToolCall:
    id: str
    name: str
    args: dict          # 已解析的 dict

@dataclass
class Reply:
    content: str
    tool_calls: list[ToolCall]

    def to_message(self) -> dict:
        return {"role": "assistant", "content": self.content or None,
                "tool_calls": [{"id": c.id, "type": "function",
                                "function": {"name": c.name,
                                             "arguments": json.dumps(c.args, ensure_ascii=False)}}
                               for c in self.tool_calls]}
```

**`arguments` 回填时必须序列化成 JSON 字符串，不能是 dict。** 这是常见的静默错误：部分服务端会宽容接受，直到某天换模型或参数含特殊字符时才报错，届时很难定位。

**tool_call 增量拼接是本实现中最容易写错的地方。** OpenAI 兼容的流式接口把 `tool_calls[].function.arguments` 拆成**字符串碎片**，按 `index` 分片下发。必须按 index 累积，全部结束后才 `json.loads`。

它平时不暴露：单个短参数调用常常一个 chunk 就到齐了；等参数变长、或模型一次调两个工具时才崩——那时你已经在调别的东西了。因此必须有独立单元测试（见 §13 第 8 条）。

## 12. 审计日志

每个工具调用记一行到 `norma.log`：工具名、参数、放行与否、结果前 200 字。用 stdlib `logging`，约 5 行。

**这不是可选项**：一个会在你机器上执行命令的助手，用户必须能事后查它到底干了什么。这也是以后做远程接入时的第一道取证手段。

## 13. 测试策略

接缝在 `llm.py`——它是唯一碰网络的地方。测试注入照剧本走的假 LLM：

```python
class FakeLLM:
    def __init__(self, script): self.script = list(script)   # 每轮的预设输出
    async def chat(self, messages, tools):
        for item in self.script.pop(0):
            yield item
```

`tests/test_agent.py`：

| # | 验证 |
|---|---|
| 1 | 模型不调工具 → `Finished` |
| 2 | 调一次工具 → 结果真的回填进了 `messages` → `Finished` |
| 3 | 工具抛异常 → 错误回填 → 模型恢复 |
| 4 | 权限被拒 → 拒绝理由回填 → 模型改道 |
| 5 | 参数不合法 → 校验错误回填（**不崩**） |
| 6 | 超过 `max_steps` → `Failed` |
| 7 | 五种事件都能 `json.dumps` |
| 8 | **tool_call 增量拼接**：喂手工构造的碎片序列，验证按 index 累积 |
| 9 | 模型调用不存在的工具名 → 错误回填 → 模型改道（**不崩**，见 §9 的 ⓪） |

**第 8 条是全套中最重要的。** 理由见 §11。

### 手工冒烟

真 API 跑一次「列出我的下载文件夹」。这是唯一能验证「模型真的会调工具」的检查——假 LLM 只验证循环，不验证与真实模型的协议是否对得上。

## 14. 已知取舍

| 取舍 | 代价 | 升级路径 |
|---|---|---|
| 无 `PermissionRequested` 事件 | 客户端有两个接入点（事件流 + ask 实现） | 若需把「等待授权」做成一等协议事件，改为双向流 |
| 会话历史仅存内存 | 退出进程即失忆 | 加 JSONL 追加写 + 启动加载 |
| 无 token 统计 | 不知道花了多少钱 | 在 `Reply` 上带 `usage`，累计到会话 |
| 多工具串行 | 多工具场景变慢 | `asyncio.gather` + 权限询问排队 |
| 工具结果截断 8000 字符 | 超大输出会丢信息 | 改为写临时文件、只回填路径与摘要 |
| 权限策略硬编码在 `permission.py` | 改策略要改代码 | 规则文件 / 远端确认 |
| 无上下文压缩 | 长会话会触窗口上限 | 历史摘要 + 保留最近 N 轮 |

## 15. 环境前提

**本机当前未安装 Python。**

`where python` 只命中 `C:\Users\Administrator\AppData\Local\Microsoft\WindowsApps\python.exe`，这是一个 **0 字节的 Microsoft Store 别名桩**，运行返回 `9009`（命令未找到）。系统内无 python.org 安装、无 conda、无 uv、无 pip。

实现前需先安装 Python 3.11+（推荐 python.org 官方安装包，或 `winget install Python.Python.3.12`），并确认该 Store 别名不再抢占 `PATH`（设置 → 应用 → 高级应用设置 → 应用执行别名，关闭 `python.exe` / `python3.exe`）。

## 16. 分发形态与演进（记录，不在 v1 范围）

「将来给其他人一起使用」时的边界。**其中两条约束 v1 的实现纪律**，故记录在案。

### Tool 与 Skill 不是一回事

| | 是什么 | 可否集中分发 |
|---|---|---|
| **Tool** | Python 代码 | **否**——分发代码等于分发可执行程序，需签名、审核、沙箱 |
| **Skill** | 纯数据（提示词 / 流程 / 用哪些工具） | **是**，零执行风险 |

Skill 是数据，一个 git 仓库或静态 URL 即可分发，**不需要服务**。Tool 是代码，且不该集中分发。

### 什么才真需要服务

| 东西 | 小规模（几个朋友） | 何时才真需要服务 |
|---|---|---|
| Skill 分发 | git 仓库 / 静态 URL | 需要灰度、审核时 |
| 权限策略模板 | 静态文件 + 本地覆盖 | 基本永远不需要 |
| 远程接入（手机 → 家里电脑） | **Tailscale 等组网工具** | 用户多到不能要求安装第三方 |
| 账号 / 授权 | 不需要，各实例独立 | 需要计费或权限管控时 |
| 更新分发 | 检查 release 版本号 | 需要强制升级时 |
| 遥测 | 不需要 | 想知道别人怎么用时 |

**小规模的正确服务数量是零。** 唯一技术上真麻烦的是 NAT 穿透（手机在 4G 上连家里电脑），而它由组网工具解决，不必自建中继。等真有几十上百用户再谈控制面。

### 不可协商：权限判断永远在本地

云端**可以**分发默认策略模板，**最终判断必须在本地内核**：

1. 断网不应导致助手瘫痪——本地执行本该离线可用
2. 云端被攻破 = 所有用户的机器同时失守
3. 用户必须能审计自己的安全边界；一个黑盒返回的 `allow: true` 与之不可比

Skill 同理：**分发可集中，执行必须在本地。**

### 「发安装包给别人」意味着什么

1. **内核必须能独立安装**（不是 dev 环境跑 Python 脚本）→ 需打包为可执行文件
2. **需要自更新机制**，否则你会永远在帮别人手动升级
3. **发布者成为安全责任主体**：该程序将在他人机器上执行任意命令。这不是技术问题，是真实责任——也正是权限闸门必须从 v1 就在的原因
4. 默认**各实例独立、互不相通**：他人的内核不得指挥你的机器

### 由此约束 v1 的两条纪律

> **① 内核必须能完全独立运行。** 不允许任何「启动时必须联网获取配置」的设计。网络是可选增强，不是依赖。
>
> **② 权限策略是声明式数据。** v1 硬编码（见 §10），但方向是读一份本地 YAML/JSON 并允许本地覆盖。
