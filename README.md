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

> Windows 上若 `python` 提示"未找到"或弹出应用商店，说明命中的是 Microsoft Store 的
> 别名占位程序。去「设置 → 应用 → 高级应用设置 → 应用执行别名」关闭 `python.exe` /
> `python3.exe`，或改用 `py` 启动器。

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
