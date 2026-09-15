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


def test_non_numeric_max_steps_raises_chinese_error(monkeypatch):
    monkeypatch.setenv("NORMA_API_KEY", "sk-test")
    monkeypatch.setenv("NORMA_MAX_STEPS", "abc")
    with pytest.raises(RuntimeError, match="NORMA_MAX_STEPS 必须是整数"):
        Config.from_env()


def test_zero_max_steps_is_rejected(monkeypatch):
    monkeypatch.setenv("NORMA_API_KEY", "sk-test")
    monkeypatch.setenv("NORMA_MAX_STEPS", "0")
    with pytest.raises(RuntimeError, match="必须大于 0"):
        Config.from_env()


def test_blank_optional_values_fall_back_to_defaults(monkeypatch):
    monkeypatch.setenv("NORMA_API_KEY", "sk-test")
    monkeypatch.setenv("NORMA_BASE_URL", "   ")
    monkeypatch.setenv("NORMA_MODEL", "")
    monkeypatch.setenv("NORMA_MAX_STEPS", "  ")
    cfg = Config.from_env()
    assert cfg.base_url == "https://api.deepseek.com"
    assert cfg.model == "deepseek-chat"
    assert cfg.max_steps == 25
