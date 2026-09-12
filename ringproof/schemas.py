"""API 请求/响应的 Pydantic 模型。"""
from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .multipart import MAX_COMPOSITION_CHANGES

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


class CoverageCellReq(BaseModel):
    """all-the-work 覆盖方案中的一个要求格：某工作钟在某方法下须至少敲
    ``min_leads`` 次指定 place-bell 类别；``place_bell`` 留空表示全部
    place-bell 类别（1..stage），保存时展开为逐类别格。"""

    bell: int = Field(ge=1, le=12, description="工作钟钟号")
    method: str = Field(description="方法 id（须在方案的 methods 列表中）")
    place_bell: int | None = Field(
        default=None, ge=1, le=12,
        description="place-bell 类别（钟在 lead 开始前的位置，1 起）；null 表示全部类别",
    )
    min_leads: int = Field(default=1, ge=1, description="该格要求的最少 lead 次数")


class CoverageSchemeCreate(BaseModel):
    """创建 all-the-work 覆盖方案版本（不可变，按方法版本冻结）。

    方案按钟数创建：``working_bells`` 选定工作钟（钟号 1..stage，须唯一），
    ``methods`` 给出每个工作钟应经历的方法版本（version 留空则冻结为最新），
    ``cells`` 逐格设置最低 lead 次数；``place_bell`` 留空表示全部 place-bell
    类别。重复格、越界钟号或方法钟数不一致由服务端拒绝（不落库）。
    """

    id: str | None = Field(default=None, description="留空则自动生成；同名 id 递增版本")
    name: str = Field(min_length=1, max_length=200)
    stage: int = Field(description="钟数，4~12")
    working_bells: list[int] = Field(min_length=1, max_length=12, description="工作钟钟号列表（1..stage，须唯一）")
    methods: list[MethodRef] = Field(min_length=1, description="覆盖分析覆盖的方法版本列表（同钟数，id 须唯一）")
    cells: list[CoverageCellReq] = Field(min_length=1, description="覆盖要求格（每格一个 钟×方法×place-bell 最低次数）")

    @model_validator(mode="after")
    def _check(self) -> "CoverageSchemeCreate":
        if not 4 <= self.stage <= 12:
            raise ValueError("钟数 stage 须为 4~12")
        if len(set(self.working_bells)) != len(self.working_bells):
            raise ValueError("working_bells 存在重复钟号")
        if any(not 1 <= b <= self.stage for b in self.working_bells):
            raise ValueError(f"working_bells 钟号须在 1..{self.stage} 范围内")
        method_ids = [m.id for m in self.methods]
        if len(set(method_ids)) != len(method_ids):
            raise ValueError("methods 列表存在重复的方法 id")
        valid_bells = set(self.working_bells)
        for c in self.cells:
            if c.bell not in valid_bells:
                raise ValueError(f"覆盖格的钟号 {c.bell} 不在 working_bells 中")
            if c.method not in method_ids:
                raise ValueError(f"覆盖格引用了 methods 列表外的方法: {c.method!r}")
            if c.place_bell is not None and not 1 <= c.place_bell <= self.stage:
                raise ValueError(f"place_bell {c.place_bell} 超出 1..{self.stage} 范围")
        # 注：展开 place-bell 类别（None→全部）后的重复格由服务端在方法版本
        # 冻结后检测并以 DUPLICATE_CELL 拒绝（错误响应带机器可读 code）。
        return self


class CoverageRef(BaseModel):
    """引用一个 all-the-work 覆盖方案的不可变版本；version 留空则随 touch
    冻结为首次使用时的最新版本。"""

    id: str
    version: int | None = Field(default=None, ge=1, description="留空则引用该方案的最新版本（随 touch 冻结）")


class ProveRequest(BaseModel):
    """证明请求：可引用音乐评分方案与 all-the-work 覆盖方案。"""

    music: MusicRef | None = Field(default=None, description="音乐评分方案引用（留空则不评分）")
    coverage: CoverageRef | None = Field(default=None, description="all-the-work 覆盖方案引用（留空则不分析）")


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
    coverage: CoverageRef | None = Field(default=None, description="all-the-work 覆盖方案引用（留空则不分析覆盖）")
    require_full_coverage: bool = Field(default=False, description="仅保留全覆盖（每个要求格满足 min_leads）的变体")
    min_completion: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description="最低完成率（0~1）：覆盖完成率低于该值的变体被过滤",
    )

    @model_validator(mode="after")
    def _check_music(self) -> "EnumerateRequest":
        if (self.min_music_score is not None or self.min_music_hits is not None) and self.music is None:
            raise ValueError("设置音乐门槛（min_music_score/min_music_hits）时必须引用评分方案 music")
        if self.require_full_coverage and self.coverage is None:
            raise ValueError("要求全覆盖（require_full_coverage）时必须引用覆盖方案 coverage")
        if self.min_completion is not None and self.coverage is None:
            raise ValueError("设置最低完成率（min_completion）时必须引用覆盖方案 coverage")
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
    """引用不可变 touch 的指定 change 作为前缀（可位于 lead 中途）。"""

    touch_id: str
    touch_version: int = Field(ge=1)
    up_to_change: int = Field(ge=1, description="前缀截止的 change 序号（可在 lead 中途，续接时先强制敲完该 lead 剩余 change）")


class PrefixCreate(BaseModel):
    """提交部分 touch：显式 rows+leads，或引用不可变 touch 的指定 change。

    显式模式下 ``rows`` 为不含起始 row 的逐 change row，长度可为 1..各标注
    lead 的 change 总数之间的任意值——止于最后一个标注 lead 的中途时，
    该 lead 记为部分 lead（``partial_lead``），续接时先强制敲完其剩余 change。
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


# ---------------- 呼叫位置写法（calling positions） ----------------


class PositionSchemeRef(BaseModel):
    """引用一个位置方案的不可变版本；version 留空则在创建 composition 时冻结为最新。"""

    id: str
    version: int | None = Field(default=None, ge=1, description="留空则引用该方案的最新版本（随 composition 冻结）")


class CallPositions(BaseModel):
    """某个 call 对位置符号的专属映射；未覆盖的符号回落方案的 default 映射。"""

    call: str = Field(description="call 名称（bob/single/自定义，不能为保留字 plain）")
    positions: dict[str, int] = Field(
        min_length=1, description="该 call 结束后位置符号 → 观察钟位置（1 起）"
    )


class PositionSchemeCreate(BaseModel):
    """创建呼叫位置方案版本（不可变，按方法版本冻结）。

    positions 给出 Home/Wrong/Middle 等符号在 call 结束（lead end）后观察钟
    的位置；call_positions 为 bob/single/自定义 call 的专属映射，未覆盖符号
    回落 positions。home_symbol 指定 home（course end）符号。
    """

    id: str | None = Field(default=None, description="留空则自动生成；同名 id 递增版本")
    name: str = Field(min_length=1, max_length=200)
    method_id: str = Field(description="方法 id（位置推演按该方法的不可变版本冻结）")
    method_version: int | None = Field(default=None, ge=1, description="留空则冻结为创建时最新版本")
    observer: int = Field(ge=1, le=12, description="观察钟（按钟号，1..stage）")
    positions: dict[str, int] = Field(
        min_length=1, description="位置符号 → call 结束后的观察钟位置（1 起）"
    )
    call_positions: list[CallPositions] = Field(
        default_factory=list, description="各 call 的专属位置映射（可缺省，回落 positions）"
    )
    home_symbol: str = Field(default="Home", description="home（course end）位置符号")

    @field_validator("positions")
    @classmethod
    def _check_positions(cls, v: dict[str, int]) -> dict[str, int]:
        if not all(isinstance(k, str) and k for k in v):
            raise ValueError("位置符号须为非空字符串")
        if any(p < 1 for p in v.values()):
            raise ValueError("观察钟位置须 ≥ 1")
        return v


class PositionToken(BaseModel):
    """composition 中的一个呼叫：位置符号 + call，并可限制此前经过的 plain lead 数。

    plain_leads（精确值）与 max_plain_leads（搜索上界）只能二选一；两者都
    缺省时在一个 plain course 内枚举匹配。
    """

    symbol: str = Field(min_length=1, description="位置符号，如 Home/Wrong/Middle")
    call: str | None = Field(
        default=None,
        description="call 名称（bob/single/自定义）；null 表示该位置由 plain lead 结束",
    )
    plain_leads: int | None = Field(
        default=None, ge=0,
        description="此 call 之前须恰好经过的 plain lead 数（自 course head 或上一个 call 起）",
    )
    max_plain_leads: int | None = Field(
        default=None, ge=0,
        description="此 call 之前至多经过的 plain lead 数；缺省则枚举一个 plain course",
    )

    @model_validator(mode="after")
    def _check(self) -> "PositionToken":
        if self.plain_leads is not None and self.max_plain_leads is not None:
            raise ValueError("plain_leads 与 max_plain_leads 只能二选一")
        if self.call is not None:
            _validate_call_ref(self.call)
        return self


class CompositionPart(BaseModel):
    """composition 中可复用的一个 part：有序 token 列表 + 重复次数。"""

    tokens: list[PositionToken] = Field(min_length=1, description="该 part 的呼叫序列")
    repeat: int = Field(default=1, ge=1, le=1000, description="该 part 重复次数")
    name: str | None = Field(default=None, max_length=100, description="part 名称（可缺省）")


class CompositionCreate(BaseModel):
    """创建呼叫位置 composition 版本（不可变，冻结位置方案与方法版本）。

    composition 由可复用 part 组成；calls 给出 bob/single/自定义 call 的
    place notation，call 名在整个 composition 内唯一。
    """

    id: str | None = Field(default=None, description="留空则自动生成；同名 id 递增版本")
    name: str | None = Field(default=None, max_length=200)
    scheme: PositionSchemeRef = Field(description="位置方案引用（版本可随 composition 冻结）")
    calls: dict[str, CallDef] = Field(
        default_factory=dict, description="bob/single/自定义 call 定义；token 中引用的 call 须在此声明"
    )
    parts: list[CompositionPart] = Field(min_length=1, description="可复用 part 列表（按顺序展开 repeat）")
    start_row: str | None = Field(default=None, description="起始排列（第一个 course head），缺省为 rounds")
    max_calls: int | None = Field(default=None, ge=0, description="允许的替换（call）次数上限")

    @field_validator("calls")
    @classmethod
    def _check_call_names(cls, v: dict[str, CallDef]) -> dict[str, CallDef]:
        for name in v:
            _validate_call_ref(name)
        return v


class CompileRequest(BaseModel):
    """编译请求：可指定起始 course head、plain course 长度与末行要求。"""

    course_head: str | None = Field(
        default=None, description="起始 course head 排列；缺省用 composition 冻结的 start_row（rounds）"
    )
    course_length: int | None = Field(
        default=None, ge=1, le=500,
        description="一个 plain course 的 lead 数（搜索/收尾窗口）；缺省时由观察钟回 home 自动界定",
    )
    expect_rounds: bool = Field(
        default=True, description="是否要求末行回到 rounds（不影响编译落库，仅在结果中标注是否满足）"
    )
    touch_id: str | None = Field(
        default=None, description="另存 touch 版本时使用的 id；留空则按 composition id 派生",
    )
    save_touch: bool = Field(default=True, description="成功后是否另存为不可变 touch 版本")


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


# ---------------- multipart composition 校核 ----------------


class TouchRef(BaseModel):
    """引用一个 touch 的不可变版本；version 留空则在创建分析时冻结为最新版本。"""

    id: str
    version: int | None = Field(default=None, ge=1, description="留空则引用该 touch 的最新版本（随分析冻结）")


class MultipartAnalysisCreate(BaseModel):
    """创建 multipart composition 校核的不可变版本。

    从不可变 touch 选取首尾落在 lead end 的连续区段作为一个 part（起始
    change 0 表示 touch 起始 row），系统求出区段起止 row 间的钟置换并
    反复作用展开整首 composition：各 part 无须重复提交 row。
    """

    id: str | None = Field(default=None, description="留空则自动生成；同名 id 递增版本")
    touch: TouchRef = Field(description="part 区段所在的不可变 touch（版本随分析冻结）")
    part_start_change: int = Field(
        ge=0, description="part 区段起始 change 序号（0 为 touch 起始 row；须为 lead end）"
    )
    part_end_change: int = Field(
        ge=1, description="part 区段结束 change 序号（须为 lead end）"
    )
    expected_parts: int = Field(
        ge=1, le=500, description="预期 part 数：区段置换反复作用的次数"
    )
    fixed_bells: list[int] = Field(
        default_factory=list,
        description="必须保持原位的钟：区段置换须将这些钟固定（1..stage，不重复）",
    )

    @model_validator(mode="after")
    def _check(self) -> "MultipartAnalysisCreate":
        if self.part_start_change >= self.part_end_change:
            raise ValueError("part_start_change 须小于 part_end_change")
        if len(set(self.fixed_bells)) != len(self.fixed_bells):
            raise ValueError("fixed_bells 存在重复钟号")
        return self


class MultipartEnumerateRequest(BaseModel):
    """在分析冻结的 touch 中枚举整首为真的 multipart composition。

    候选为全部首尾落在 lead end 的连续区段；先按 fixed bells 与轨道长度
    （part 数须等于置换的阶）剪枝，再做行交集扫描，只保留整首为真的结果，
    沿用 touch 中的位置顺序返回。
    """

    parts: int | None = Field(
        default=None, ge=2, le=500,
        description="精确 part 数（置换的阶须恰为该值）；缺省用 min_parts..max_parts 范围",
    )
    min_parts: int = Field(default=2, ge=2, le=500, description="最小 part 数")
    max_parts: int = Field(default=64, ge=2, le=500, description="最大 part 数")
    max_results: int = Field(default=50, ge=1, le=500, description="最多返回的结果数")
    max_search: int = Field(
        default=20000, ge=1, le=2_000_000,
        description="搜索预算：最多检查的候选区段数，超出则截断并说明原因",
    )

    @model_validator(mode="after")
    def _check(self) -> "MultipartEnumerateRequest":
        if self.parts is not None:
            self.min_parts = self.max_parts = self.parts
        if self.min_parts > self.max_parts:
            raise ValueError("min_parts 不能大于 max_parts")
        return self


# ---------------- 可复用 block 拼装 ----------------


class BlockSpec(BaseModel):
    """一个可复用 block：从不可变 touch 截取的 lead-end 区段 + 使用次数。"""

    id: str = Field(
        min_length=1, max_length=50,
        description="block 标识（同一 composition 内唯一；衔接规则与搜索结果以此引用）",
    )
    touch: TouchRef = Field(description="区段所在的不可变 touch（版本留空则随 composition 冻结为最新）")
    start_change: int = Field(
        ge=0, description="区段起始 change 序号（0 为 touch 起始 row；须为 lead end）"
    )
    end_change: int = Field(ge=1, description="区段结束 change 序号（须为 lead end）")
    min_uses: int = Field(default=0, ge=0, le=1000, description="组合中最少使用次数")
    max_uses: int | None = Field(
        default=None, ge=1, le=1000,
        description="组合中最多使用次数；null 表示不限（受总 change 数约束）",
    )

    @model_validator(mode="after")
    def _check(self) -> "BlockSpec":
        if self.start_change >= self.end_change:
            raise ValueError("start_change 须小于 end_change")
        if self.max_uses is not None and self.min_uses > self.max_uses:
            raise ValueError(f"min_uses({self.min_uses}) 不能大于 max_uses({self.max_uses})")
        return self


class BlockCompositionCreate(BaseModel):
    """创建可复用 block 拼装的不可变版本。

    从多个不可变 touch 截取首尾落在 lead end 的区段作为 block（保存时转为
    相对起点的钟置换，可从不同 lead head 展开），设置各 block 使用次数与
    相邻衔接规则，并限定总 change 数与目标末行。钟数不一致、边界非法或
    区段自身为假时拒绝保存（不落库）。
    """

    id: str | None = Field(default=None, description="留空则自动生成；同名 id 递增版本")
    name: str | None = Field(default=None, max_length=200)
    blocks: list[BlockSpec] = Field(
        min_length=1, max_length=64, description="可复用 block 列表（可引用多个 touch）"
    )
    start_row: str | None = Field(default=None, description="起始排列，缺省为 rounds")
    target_row: str | None = Field(
        default=None, description="目标末行；null 表示不限制末行"
    )
    min_changes: int = Field(
        default=0, ge=0, le=MAX_COMPOSITION_CHANGES, description="组合总 change 数下限"
    )
    max_changes: int = Field(
        ge=1, le=MAX_COMPOSITION_CHANGES,
        description="组合总 change 数上限（搜索规模由此界定）",
    )
    allowed_transitions: list[TransitionPair] | None = Field(
        default=None,
        description="相邻 block 间允许的衔接白名单 [from, to]；null 表示不限制（同 block 延续始终允许）",
    )
    forbidden_transitions: list[TransitionPair] | None = Field(
        default=None, description="相邻 block 间禁止的衔接 [from, to]"
    )

    @model_validator(mode="after")
    def _check(self) -> "BlockCompositionCreate":
        ids = [b.id for b in self.blocks]
        if len(set(ids)) != len(ids):
            raise ValueError("blocks 列表存在重复的 block id")
        if self.min_changes > self.max_changes:
            raise ValueError("min_changes 不能大于 max_changes")
        return self


class BlockSearchRequest(BaseModel):
    """block 组合搜索请求。"""

    max_results: int = Field(
        default=50, ge=1, le=500, description="最多返回的组合数（按排序取最优）"
    )
    max_search: int = Field(
        default=20000, ge=1, le=2_000_000,
        description="搜索预算：最多访问的状态数，超出则截断并说明原因",
    )
