"""API 请求/响应的 Pydantic 模型。"""
from __future__ import annotations

import re
from typing import Annotated, Literal

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


class BellPosition(BaseModel):
    """一个 (钟, 位置) 对：指定钟号须出现在指定位置（1 起）。"""

    bell: int = Field(ge=1, le=12, description="钟号")
    position: int = Field(ge=1, le=12, description="位置（1 为前端）")


class MusicRuleBase(BaseModel):
    """评分规则公共配置：名称、分值、是否允许叠加、单个 row 的计分上限。"""

    name: str = Field(min_length=1, max_length=100, description="规则名称（同一方案内唯一）")
    points: int = Field(default=1, description="每次命中的分值（可为负，表示惩罚）")
    allow_overlap: bool = Field(
        default=False, description="是否允许同一行内多次命中叠加计分（positions 规则）"
    )
    max_per_row: int | None = Field(
        default=None, ge=1, description="单个 row 的计分上限（命中次数），null 表示不限制"
    )


class RowRule(MusicRuleBase):
    """精确 row 规则：整行与指定排列完全一致时命中。"""

    type: Literal["row"] = "row"
    row: str = Field(min_length=1, description="目标排列，须为 1..stage 的完整排列")


class RunRule(MusicRuleBase):
    """连续钟组规则：正序或逆序的连续钟组出现在 row 前端或后端时命中。"""

    type: Literal["run"] = "run"
    bells: list[int] = Field(min_length=1, description="连续钟组（正序如 [4,5,6]，逆序如 [6,5,4]）")
    position: Literal["front", "back"] = Field(description="钟组出现的位置：前端或后端")


class PositionsRule(MusicRuleBase):
    """指定钟位置规则：每满足一个 (钟, 位置) 对计一次命中。"""

    type: Literal["positions"] = "positions"
    positions: list[BellPosition] = Field(min_length=1, description="(钟, 位置) 对列表")


MusicRule = Annotated[RowRule | RunRule | PositionsRule, Field(discriminator="type")]


class MusicSchemeCreate(BaseModel):
    """定义一个音乐评分方案版本：指定钟数 + 三类规则的组合。

    起始 row 与末尾 rounds 默认不计分，可由 score_start_row /
    score_final_rounds 开启；其余 row（含中间回到的 rounds）一律计分。
    """

    id: str | None = Field(default=None, description="留空则自动生成；同名 id 递增版本")
    name: str = Field(min_length=1, max_length=200)
    stage: int = Field(description="钟数，4~12；规则中的钟号/位置/排列须与之相符")
    rules: list[MusicRule] = Field(min_length=1, description="评分规则列表（至少一条）")
    score_start_row: bool = Field(default=False, description="起始 row 是否计分")
    score_final_rounds: bool = Field(default=False, description="末尾回到 rounds 的最后一 row 是否计分")


class MusicRef(BaseModel):
    """引用一个评分方案的不可变版本；version 留空则随 touch 冻结为首次使用时的最新版本。"""

    id: str
    version: int | None = Field(default=None, ge=1, description="留空则引用该方案的最新版本（随 touch 冻结）")


class ProveRequest(BaseModel):
    """证明请求：可引用音乐评分方案，结果附带音乐评分。"""

    music: MusicRef | None = Field(default=None, description="音乐评分方案引用（留空则不评分）")


class EnumerateRequest(BaseModel):
    """枚举 choice / method_choice 槽位的全部 call×method 候选组合。"""

    max_calls: int | None = Field(default=None, ge=0, description="替换次数上限（覆盖 touch 中的设置）")
    max_variants: int = Field(default=256, ge=1, le=4096, description="允许枚举的最大组合数")
    max_search: int = Field(
        default=1024, ge=1, le=100000,
        description="搜索预算：最多检查的候选组合数，超出则截断并说明原因",
    )
    music: MusicRef | None = Field(default=None, description="音乐评分方案引用（留空则不评分）")
    min_music_score: int | None = Field(default=None, description="音乐总分门槛：低于该值的变体被过滤")
    min_music_hits: int | None = Field(default=None, ge=0, description="规则命中数门槛：总命中数低于该值的变体被过滤")

    @model_validator(mode="after")
    def _check_music(self) -> "EnumerateRequest":
        if (self.min_music_score is not None or self.min_music_hits is not None) and self.music is None:
            raise ValueError("设置音乐门槛（min_music_score/min_music_hits）时必须引用评分方案 music")
        return self


def _validate_call_ref(name: str, allow_plain: bool = False) -> None:
    if allow_plain and name == "plain":
        return
    if name in RESERVED_CALLS:
        raise ValueError(f"{name!r} 为保留字，不能用作 call 名；无 call 请用 null")
    if not CALL_NAME_RE.match(name):
        raise ValueError(f"非法 call 名: {name!r}（须为字母开头的字母/数字/_/-）")


# ---------------- 部分 touch（前缀）与续接 ----------------


class PrefixLead(BaseModel):
    """前缀中的一个 lead：方法 id（缺省用首选方法）与 call（null=plain）。"""

    method: str | None = Field(default=None, description="方法 id；null 表示用首选方法")
    call: str | None = Field(default=None, description="call 名称；null 表示 plain lead")


class PrefixFromTouch(BaseModel):
    """引用不可变 touch 的指定 change 作为前缀（该 change 须位于 lead end）。"""

    touch_id: str
    touch_version: int = Field(ge=1)
    up_to_change: int = Field(ge=1, description="前缀截止的 change 序号（须为某个 lead end）")


class PrefixCreate(BaseModel):
    """提交部分 touch：显式 rows+leads，或引用不可变 touch 的指定 change。

    显式模式下 ``rows`` 为不含起始 row 的逐 change row，长度须等于各标注
    lead 的 change 数之和（前缀须止于 lead end）。
    """

    id: str | None = Field(default=None, description="留空则自动生成；同名 id 递增版本")
    stage: int | None = Field(default=None, description="钟数；from_touch 模式可留空（取 touch 钟数）")
    methods: list[MethodRef] | None = Field(default=None, description="前缀引用的同钟数方法（版本留空则冻结为最新）")
    calls: dict[str, CallDef] = Field(default_factory=dict, description="前缀标注用到的 call 定义")
    start_row: str | None = Field(default=None, description="起始排列，缺省为 rounds")
    leads: list[PrefixLead] | None = Field(default=None, description="逐 lead 的方法/call 标注（显式模式）")
    rows: list[str] | None = Field(default=None, description="已敲出的逐 change row（显式模式，不含起始 row）")
    from_touch: PrefixFromTouch | None = Field(
        default=None, description="从不可变 touch 的指定 lead end change 导出前缀"
    )

    @field_validator("calls")
    @classmethod
    def _check_call_names(cls, v: dict[str, CallDef]) -> dict[str, CallDef]:
        for name in v:
            _validate_call_ref(name)
        return v

    @model_validator(mode="after")
    def _check_mode(self) -> "PrefixCreate":
        if self.from_touch is not None:
            if any(v is not None for v in (self.leads, self.rows)):
                raise ValueError("from_touch 与 leads/rows 只能二选一")
        else:
            if self.stage is None:
                raise ValueError("显式前缀必须提供 stage")
            if not self.methods:
                raise ValueError("显式前缀必须提供 methods")
            if not self.leads:
                raise ValueError("显式前缀必须提供非空 leads")
            if self.rows is None:
                raise ValueError("显式前缀必须提供 rows")
        return self


class ContinueRequest(BaseModel):
    """在前缀之后搜索续接尾段。

    方法/call/配额/转换规则均只作用于尾段；相邻转换规则同时约束
    前缀末 lead 方法 → 尾段首 lead 方法。
    """

    max_leads: int = Field(default=12, ge=1, le=200, description="剩余 lead 数上限")
    target_row: str | None = Field(default=None, description="目标 row，缺省为 rounds；须出现在尾段末 row")
    methods: list[MethodRef] | None = Field(
        default=None, description="尾段可用方法；缺省沿用前缀方法（版本随前缀冻结）"
    )
    calls: dict[str, CallDef] | None = Field(
        default=None, description="尾段可用 call；缺省沿用前缀 call，同名可覆盖"
    )
    max_calls: int | None = Field(default=None, ge=0, description="尾段替换（call）次数上限")
    method_quotas: dict[str, MethodQuota] = Field(
        default_factory=dict, description="尾段各方法用量配额（按尾段 lead 计）"
    )
    allowed_transitions: list[TransitionPair] | None = Field(
        default=None, description="相邻 lead 间允许的方法转换白名单（同方法延续始终允许）"
    )
    forbidden_transitions: list[TransitionPair] | None = Field(
        default=None, description="相邻 lead 间禁止的方法转换"
    )
    max_results: int = Field(default=50, ge=1, le=500, description="最多返回的方案数")
    max_search: int = Field(
        default=20000, ge=1, le=2_000_000,
        description="搜索预算：最多检查的状态（前缀路径）数，超出则截断并说明原因",
    )
    music: MusicRef | None = Field(default=None, description="音乐评分方案引用（随前缀版本冻结）")
    min_music_score: int | None = Field(default=None, description="音乐总分门槛：低于该值的方案被过滤")
    min_music_hits: int | None = Field(default=None, ge=0, description="规则命中数门槛")

    @field_validator("calls")
    @classmethod
    def _check_call_names(cls, v: dict[str, CallDef] | None) -> dict[str, CallDef] | None:
        if v:
            for name in v:
                _validate_call_ref(name)
        return v

    @model_validator(mode="after")
    def _check_music(self) -> "ContinueRequest":
        if (self.min_music_score is not None or self.min_music_hits is not None) and self.music is None:
            raise ValueError("设置音乐门槛（min_music_score/min_music_hits）时必须引用评分方案 music")
        return self
