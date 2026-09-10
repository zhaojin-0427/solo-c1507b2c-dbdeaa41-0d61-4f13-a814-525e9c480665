"""API 请求/响应的 Pydantic 模型。"""
from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator, model_validator

CALL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
RESERVED_CALLS = {"plain"}


class MethodCreate(BaseModel):
    """定义一个方法版本：4~12 口钟 + place notation。"""

    id: str | None = Field(default=None, description="留空则自动生成；同名 id 递增版本")
    name: str = Field(min_length=1, max_length=200)
    stage: int = Field(description="钟数，4~12")
    notation: str = Field(min_length=1, description="place notation，支持 & 对称、+ lead end、(…)xN 重复段")


class CallDef(BaseModel):
    """lead end 处的 call：用一段记号替换方法末尾 replace 个 change。"""

    notation: str = Field(min_length=1, description="call 的 place notation（可含 &、+、重复段）")
    replace: int = Field(default=1, ge=1, le=64, description="替换 lead 末尾的 change 数")


class LeadSpec(BaseModel):
    """一个 lead：固定 call、或留给枚举的 choice 槽位。"""

    call: str | None = Field(default=None, description="call 名称；null 表示 plain lead")
    choice: list[str] | None = Field(default=None, description="枚举槽位：可选值含 'plain' 或已定义 call 名")

    @model_validator(mode="after")
    def _check(self) -> "LeadSpec":
        if self.call is not None and self.choice is not None:
            raise ValueError("call 与 choice 只能二选一")
        if self.call is not None:
            _validate_call_ref(self.call)
        if self.choice is not None:
            if not self.choice:
                raise ValueError("choice 列表不能为空")
            if len(set(self.choice)) != len(self.choice):
                raise ValueError("choice 列表存在重复项")
            for c in self.choice:
                _validate_call_ref(c, allow_plain=True)
        return self


class SeqGroup(BaseModel):
    """一组 lead 的顺序段，repeat 为重复次数（重复段在证明前展开）。"""

    leads: list[LeadSpec] = Field(min_length=1)
    repeat: int = Field(default=1, ge=1, le=1000)


class TouchCreate(BaseModel):
    """定义一个 touch 版本。"""

    id: str | None = Field(default=None, description="留空则自动生成；同名 id 递增版本")
    method_id: str
    method_version: int | None = Field(default=None, description="留空则引用该方法的最新版本")
    start_row: str | None = Field(default=None, description="起始排列，缺省为 rounds")
    calls: dict[str, CallDef] = Field(default_factory=dict, description="bob/single/自定义 call 定义")
    sequence: list[SeqGroup] = Field(min_length=1, description="lead 顺序（含重复段）")
    max_calls: int | None = Field(default=None, ge=0, description="允许的替换（call）次数上限")

    @field_validator("calls")
    @classmethod
    def _check_call_names(cls, v: dict[str, CallDef]) -> dict[str, CallDef]:
        for name in v:
            _validate_call_ref(name)
        return v


class EnumerateRequest(BaseModel):
    """枚举 choice 槽位的全部 call 变体。"""

    max_calls: int | None = Field(default=None, ge=0, description="替换次数上限（覆盖 touch 中的设置）")
    max_variants: int = Field(default=256, ge=1, le=4096, description="允许枚举的最大组合数")


def _validate_call_ref(name: str, allow_plain: bool = False) -> None:
    if allow_plain and name == "plain":
        return
    if name in RESERVED_CALLS:
        raise ValueError(f"{name!r} 为保留字，不能用作 call 名；无 call 请用 null")
    if not CALL_NAME_RE.match(name):
        raise ValueError(f"非法 call 名: {name!r}（须为字母开头的字母/数字/_/-）")
