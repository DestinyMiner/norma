import logging

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


def test_tools_schema_with_empty_registry_is_empty():
    """空注册表是合法输入，不得悄悄回落到全局 TOOLS。

    这条守的是 `registry is None` 与真值判断的区别：Agent(tools={}) 表示
    "没有工具"，若回落成全局注册表，模型会被展示它根本调不到的工具。
    """
    assert tools_schema({}) == []


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


async def test_read_file_truncation_mid_character_is_not_reported_as_binary(tmp_path):
    """中文文本按字节截断时不该被误报成二进制文件（每字 3 字节）。"""
    f = tmp_path / "cn.txt"
    f.write_text("中文测试" * 100, encoding="utf-8")
    # 10 字节 = 3 个完整汉字（9 字节）+ 第 4 个字的第 1 个字节
    out = await TOOLS["read_file"].fn(path=str(f), max_bytes=10)
    assert "无法按 UTF-8 解码" not in out
    assert out.startswith("中文测")


async def test_untruncated_binary_file_is_reported_as_binary(tmp_path):
    """未截断时不该走回退路径——否则末字节非法的二进制会被当成文本返回。"""
    f = tmp_path / "b.bin"
    f.write_bytes(b"abc\xff\xfe")

    out = await TOOLS["read_file"].fn(path=str(f))

    assert "无法按 UTF-8 解码" in out


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


async def test_write_file_rejects_a_directory_path(tmp_path):
    target = tmp_path / "adir"
    target.mkdir()
    out = await TOOLS["write_file"].fn(path=str(target), content="x")
    assert out.startswith("不是文件")
    assert target.is_dir()  # 目录没有被破坏


async def test_write_file_reports_oserror_instead_of_raising(tmp_path):
    """父路径撞上已存在的文件：返回中文说明，而不是抛异常。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("我是文件", encoding="utf-8")
    out = await TOOLS["write_file"].fn(path=str(blocker / "x.txt"), content="y")
    assert "写入失败" in out


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


async def test_run_powershell_does_not_inherit_the_api_key(monkeypatch):
    """子进程环境里不能有 NORMA_*：否则一句 env: 就能把密钥打进日志与会话。"""
    monkeypatch.setenv("NORMA_API_KEY", "sk-secret-must-not-leak")

    out = await TOOLS["run_powershell"].fn(
        command='Write-Output "key=[$env:NORMA_API_KEY]"')

    assert "sk-secret-must-not-leak" not in out
    assert "key=[]" in out
