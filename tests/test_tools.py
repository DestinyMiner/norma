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
