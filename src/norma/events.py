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
