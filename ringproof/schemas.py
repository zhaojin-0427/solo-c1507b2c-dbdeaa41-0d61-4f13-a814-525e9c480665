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


class MethodRef(BaseModel):
    """引用一个方法的不可变版本；version 留空则在创建 touch 时冻结为最新版本。"""

    id: str
    version: int | None = Field(default=None, ge=1, description="留空则引用该方法的最新版本")


class MethodQuota(BaseModel):
    """单个方法的使用次数配额（按 lead 计）。"""

    min: int | None = Field(default=None, ge=0, description="最少使用次数")
    max: int | None = Field(default=None, ge=0, description="最多使用次数")

    @model_validator(mode="after")
    def _check(self) -> "MethodQuota":
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"配额 min({self.min}) 不能大于 max({self.max})")
        return self


class CallDef(BaseModel):
    """lead end 处的 call：用一段记号替换方法末尾 replace 个 change。"""

    notation: str = Field(min_length=1, description="call 的 place notation（可含 &、+、重复段）")
    replace: int = Field(default=1, ge=1, le=64, description="替换 lead 末尾的 change 数")


class LeadSpec(BaseModel):
    """一个 lead：固定 call/方法、或留给枚举的 choice 槽位。"""

    call: str | None = Field(default=None, description="call 名称；null 表示 plain lead")
    choice: list[str] | None = Field(default=None, description="call 枚举槽位：可选值含 'plain' 或已定义 call 名")
    method: str | None = Field(default=None, description="固定方法 id；null 表示用 touch 的首选方法")
    method_choice: list[str] | None = Field(default=None, description="方法枚举槽位：候选方法 id 列表")

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
        if self.method is not None and self.method_choice is not None:
            raise ValueError("method 与 method_choice 只能二选一")
        if self.method_choice is not None:
            if not self.method_choice:
                raise ValueError("method_choice 列表不能为空")
            if len(set(self.method_choice)) != len(self.method_choice):
                raise ValueError("method_choice 列表存在重复项")
        return self


class SeqGroup(BaseModel):
    """一组 lead 的顺序段，repeat 为重复次数（重复段在证明前展开）。"""

    leads: list[LeadSpec] = Field(min_length=1)
    repeat: int = Field(default=1, ge=1, le=1000)


TransitionPair = tuple[str, str]


class TouchCreate(BaseModel):
    """定义一个 touch 版本。

    单方法模式用 ``method_id``；多方法拼接模式用 ``methods``（同钟数），
    每个 lead 可用 ``method``/``method_choice`` 指定固定或候选方法，
    并可配置各方法配额（``method_quotas``）与相邻转换规则
    （``allowed_transitions`` / ``forbidden_transitions``）。
    """

    id: str | None = Field(default=None, description="留空则自动生成；同名 id 递增版本")
    method_id: str | None = Field(default=None, description="单方法模式：方法 id")
    method_version: int | None = Field(default=None, description="留空则引用该方法的最新版本")
    methods: list[MethodRef] | None = Field(default=None, description="多方法拼接模式：同钟数方法列表")
    start_row: str | None = Field(default=None, description="起始排列，缺省为 rounds")
    calls: dict[str, CallDef] = Field(default_factory=dict, description="bob/single/自定义 call 定义")
    sequence: list[SeqGroup] = Field(min_length=1, description="lead 顺序（含重复段）")
    max_calls: int | None = Field(default=None, ge=0, description="允许的替换（call）次数上限")
    method_quotas: dict[str, MethodQuota] = Field(
        default_factory=dict, description="各方法最少/最多使用次数（按 lead 计）"
    )
    allowed_transitions: list[TransitionPair] | None = Field(
        default=None,
        description="相邻 lead 间允许的方法转换白名单 [from, to]；null 表示不限制（仅约束方法变化）",
    )
    forbidden_transitions: list[TransitionPair] | None = Field(
        default=None, description="相邻 lead 间禁止的方法转换 [from, to]"
    )

    @field_validator("calls")
    @classmethod
    def _check_call_names(cls, v: dict[str, CallDef]) -> dict[str, CallDef]:
        for name in v:
            _validate_call_ref(name)
        return v

    @model_validator(mode="after")
    def _check_methods(self) -> "TouchCreate":
        if self.methods is not None:
            if self.method_id is not None or self.method_version is not None:
                raise ValueError("methods 与 method_id/method_version 只能二选一")
            if not self.methods:
                raise ValueError("methods 列表不能为空")
            ids = [m.id for m in self.methods]
            if len(set(ids)) != len(ids):
                raise ValueError("methods 列表存在重复的方法 id")
        elif self.method_id is None:
            raise ValueError("必须提供 method_id（单方法）或 methods（多方法拼接）")
        return self


class EnumerateRequest(BaseModel):
    """枚举 choice / method_choice 槽位的全部 call×method 候选组合。"""

    max_calls: int | None = Field(default=None, ge=0, description="替换次数上限（覆盖 touch 中的设置）")
    max_variants: int = Field(default=256, ge=1, le=4096, description="允许枚举的最大组合数")
    max_search: int = Field(
        default=1024, ge=1, le=100000,
        description="搜索预算：最多检查的候选组合数，超出则截断并说明原因",
    )


def _validate_call_ref(name: str, allow_plain: bool = False) -> None:
    if allow_plain and name == "plain":
        return
    if name in RESERVED_CALLS:
        raise ValueError(f"{name!r} 为保留字，不能用作 call 名；无 call 请用 null")
    if not CALL_NAME_RE.match(name):
        raise ValueError(f"非法 call 名: {name!r}（须为字母开头的字母/数字/_/-）")
