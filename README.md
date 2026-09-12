# RingProof — 变换鸣钟 touch 序列证明 API

供变换鸣钟（change ringing）作曲者校核 touch 的本机服务。调用方定义方法
（place notation）、起始排列与 lead 顺序，在 lead end 配置 bob / single /
自定义 call；API 展开对称记号与重复段，逐 row 生成序列并完成证明：
排列完整性、记号一致性、rounds 回归、首次重复定位（标明 method / lead /
call 来源），并可枚举 call 变体。方法与 touch 以不可变版本保存。

支持**多方法拼接（splicing）**：一个 touch 可引用多个同钟数方法的不可变
版本，每个 lead 指定固定方法或候选方法，并可设置各方法最少/最多使用次数
与允许/禁止的相邻转换；证明时逐 row 标明方法来源、统计拼接次数与连续段，
枚举时按配额与转换规则剪枝并优先返回为真、回到 rounds、拼接较少且方法
分布更均衡的方案。

支持**音乐模式评分与选优**：调用方可为指定钟数创建不可变评分方案，组合
精确 row、位于前端/后端的正序或逆序连续钟组、指定钟位置三类规则，每条
规则可设名称、分值、是否允许叠加与单个 row 的计分上限；证明 touch 时
引用方案即得总分、各规则命中数与首次/最高分 row（追溯 change、lead、
method、call），枚举时可设最低总分或规则命中数门槛过滤并参与排序。
方案版本随 touch 冻结，重复证明与枚举结果一致。

支持**呼叫位置写法（calling positions）编译**：调用方按方法版本创建不可变
位置方案（指定观察钟与 Home/Wrong/Middle 等符号在 call 结束后的观察钟
位置，可按 call 给专属映射），再创建由可复用 **part** 组成的 composition；
每个 token 记录位置符号与 bob、single 或自定义 call，并可用 `plain_leads`
（精确值）/ `max_plain_leads`（搜索上界）限制此前经过的 plain lead 数。
系统从指定 course head 逐 lead 推演，位置不可达、限额内匹配不唯一或 part
接不上时返回出错 token、候选 lead 与当前排列；成功后另存不可变 touch 版本，
逐 lead 标明 part、token、位置、前后 lead head 与观察钟位置，并返回证明
结果。位置方案、composition 与生成 touch 的依赖版本冻结，相同输入重复
编译结果一致。

支持**multipart composition 校核**：调用方从不可变 touch 选取首尾落在
lead end 的连续区段作为一个 part，设置预期 part 数与必须保持原位的钟；
系统求出区段起止 row 间的钟置换并反复作用，各 part 无须重复提交 row——
按置换展开整首 composition，返回各 part end、置换循环与提前回到起点的
位置，轨道不能按预期闭合时指出不一致的钟位；同时检查 part 内及跨 part
重复，冲突给出 row 与双方的 part、change、method、call 来源。枚举接口
在 touch 的全部 lead-end 区段中搜索整首为真的方案，可限定 part 数，
先按轨道长度与行交集剪枝再沿用 touch 位置顺序；分析版本冻结 touch、
方法与 call 依赖，相同输入重复计算保持一致。

支持**可复用 block 拼装**：调用方从多个不可变 touch 截取首尾落在
lead end 的区段作为 block，设置各 block 使用次数与相邻衔接规则，并限定
总 change 数与目标末行；区段保存时转为相对起点的位置置换，可从不同
lead head 展开，钟数不一致、边界非法或区段自身为假时拒绝保存。搜索按
展开后的逐 row 检查跨 block 重复，只返回满足用量、衔接、长度与末行要求
的组合，冲突列出相同 row 两侧的 block、touch、change、method、call 来源；
先按端点可达性、剩余长度与行交集剪枝，再按目标达成、总 change 数、
block 数与 call 数排序；结果冻结 touch、方法与 call 依赖，相同请求重复
计算保持一致。

## 运行

```bash
pip install -r requirements.txt
python3 -m uvicorn ringproof.main:app --port 8765
# 数据库文件默认为 ./ringproof.db，可用环境变量 RINGPROOF_DB 指定
```

交互式文档：`http://127.0.0.1:8765/docs`

## place notation 约定

| 记号 | 含义 |
|---|---|
| `x` / `-` | 全交叉（所有相邻对互换） |
| `16`、`1258` | 一个 change 的 places（1-9、0=10、T=11、E=12） |
| `.` `,` 空白 | change 分隔符（可省略，每个 `x` 自动开启新 change） |
| `&` 段首 | 回文对称：`seq + reverse(seq[:-1])`，末位 change 为对称轴 |
| `+` | 之后为追加的 lead end 记号 |
| `(…)`xN / `(…)`*N | 重复段，展开为 N 份 |

外部位隐含规则：最前 place 为偶数位则补 1 位；末 place 与 stage 间隔为奇数
则补 stage 位。校验拒绝三类非法记号：

- `BELL_OUT_OF_RANGE` — 钟号缺失/越界（引用 1..stage 之外的钟）
- `DUPLICATE_PLACE` — 位置冲突（同一 change 内钟号重复）
- `NOT_ADJACENT` — 无法由相邻换位产生（非 place 位置无法配成相邻对）

## 主要接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/methods` | 创建方法版本（4~12 口钟；同 id 递增版本，不可变） |
| GET | `/methods/{id}/versions/{v}` | 规范化记号、逐 change places、输入哈希 |
| POST | `/touches` | 创建 touch 版本（起始排列、calls、lead 顺序、max_calls、多方法拼接、配额与转换规则） |
| GET | `/touches/{id}/versions/{v}` | 规范化后的 touch 与输入哈希 |
| GET | `/touches/{id}/versions/{v}/rows` | 逐行来源（change、lead、记号、方法 id/版本、call 与拼接标记），支持分页 |
| POST | `/touches/{id}/versions/{v}/prove` | 序列证明，可引用评分方案与 all-the-work 覆盖方案（按版本+方案缓存，`X-Proof-Cache` 头标识命中） |
| POST | `/touches/{id}/versions/{v}/enumerate` | 枚举 call×method 候选组合（配额/转换剪枝、音乐/覆盖门槛过滤、排序、截断说明） |
| POST | `/music-schemes` | 创建评分方案版本（指定钟数；同 id 递增版本，不可变） |
| GET | `/music-schemes/{id}/versions/{v}` | 方案规则、计分开关与输入哈希 |
| POST | `/coverage-schemes` | 创建 all-the-work 覆盖方案版本（工作钟、方法版本、逐格最低 lead 次数；同 id 递增版本） |
| GET | `/coverage-schemes/{id}/versions/{v}` | 展开后的要求格、冻结方法版本与输入哈希 |
| POST | `/prefixes` | 提交部分 touch 前缀（显式 rows+leads，或 from_touch 引用任意 change，可在 lead 中途）并重放校核 |
| GET | `/prefixes/{id}/versions/{v}` | 前缀冻结状态（当前排列、已出现 row、lead 方法、依赖版本、逐行事件） |
| GET | `/prefixes/{id}/versions/{v}/rows` | 前缀逐行来源（分页） |
| POST | `/prefixes/{id}/versions/{v}/continuation` | 续接搜索：目标 row、剩余 lead 上限、方法/call、配额与转换规则、音乐评分，稳定排序+截断进度 |
| POST | `/position-schemes` | 创建呼叫位置方案版本（观察钟、Home/Wrong/Middle 位置映射、按 call 专属映射；按方法版本冻结） |
| GET | `/position-schemes/{id}/versions/{v}` | 位置映射、观察钟与方法版本、输入哈希 |
| POST | `/compositions` | 创建呼叫位置 composition 版本（可复用 part、逐 token 位置符号+call+plain lead 限额） |
| GET | `/compositions/{id}/versions/{v}` | 规范化 part/token、冻结的位置方案与方法版本、plain course 长度 |
| POST | `/compositions/{id}/versions/{v}/compile` | 编译为 touch：逐 lead 位置推演+证明，成功另存 touch 版本（按请求内容哈希缓存，`X-Compile-Cache`） |
| POST | `/multipart-analyses` | 创建 multipart 校核版本（touch 区段 + 预期 part 数 + 保持原位的钟；冻结 touch/方法/call 依赖） |
| GET | `/multipart-analyses/{id}/versions/{v}` | 各 part end、置换循环、提前回归位置、闭合与不一致钟位、重复冲突来源 |
| POST | `/multipart-analyses/{id}/versions/{v}/enumerate` | 枚举整首为真的区段（part 数限定、轨道/行交集剪枝、原排序；按请求哈希缓存，`X-Multipart-Cache`） |
| POST | `/block-compositions` | 创建 block 拼装版本（多 touch 的 lead-end 区段 + 使用次数 + 衔接规则 + 总 change 数与目标末行；冻结 touch/方法/call 依赖） |
| GET | `/block-compositions/{id}/versions/{v}` | 各 block 的相对置换、区段与用量、冻结依赖与输入哈希 |
| POST | `/block-compositions/{id}/versions/{v}/search` | 搜索满足用量/衔接/长度/末行的 block 组合（可达性/剩余长度/行交集剪枝、冲突两侧来源；按请求哈希缓存，`X-Block-Search-Cache`） |

### 证明结果字段

- `permutation_complete` / `notation_consistent` — 排列完整性 / 记号一致性
- `rounds_return` / `rounds_at` — rounds 回归及位置
- `truth` — 无重复 row 为 `true`（终点回到起始 rounds 属正常回归）
- `first_repeat` — 首次相同 row 的两个 change 序号及双方来源
  （method、method_id、method_version、lead、change_in_lead、notation、call 来源）
- `calls_used` / `exceeds_max_calls` — 替换次数及是否超限
- `lead_methods` — 每个 lead 实际使用的方法 id
- `splices` / `splice_count` — 发生拼接的 lead（含 from/to 方法）及拼接次数
- `runs` — 同方法连续段（起止 lead、lead 数、change 数）
- `method_usage` — 各方法用量（lead 数、change 数、版本）
- `quota_status` / `quotas_satisfied` — 各方法配额（min/max）满足情况
- `transition_violations` / `transitions_satisfied` — 相邻转换违规明细
- `lead_heads`、`events`（逐行来源：方法 id/版本、call 来源、`splice` 标记）

### 多方法拼接

touch 用 `methods`（替代单方法的 `method_id`）引用多个**同钟数**方法的
不可变版本（`version` 留空则冻结为创建时的最新版本）：

```json
{
  "methods": [{"id": "pb-minor"}, {"id": "alt-minor", "version": 2}],
  "method_quotas": {"pb-minor": {"min": 2}, "alt-minor": {"max": 3}},
  "allowed_transitions": [["pb-minor", "alt-minor"]],
  "forbidden_transitions": [["alt-minor", "pb-minor"]],
  "sequence": [{"leads": [
    {"method": "pb-minor"},
    {"method_choice": ["pb-minor", "alt-minor"], "choice": ["plain", "bob"]}
  ]}]
}
```

- lead 的 `method` 固定方法、`method_choice` 留给枚举的候选方法；
  两者都缺省时用 `methods` 的首选方法
- `method_quotas` — 各方法按 lead 计的最少/最多使用次数
- `allowed_transitions`（白名单）/ `forbidden_transitions`（黑名单）约束
  相邻 lead 间的方法变化；同方法延续始终允许
- 创建时拒绝：`STAGE_MISMATCH`（钟数不一致）、方法版本缺失（404）、
  `UNKNOWN_METHOD` / `UNKNOWN_TRANSITION`（引用未声明方法）、
  `UNSATISFIABLE_QUOTA`（配额在固定 lead 与候选槽位下无解，最大流精确判定）、
  `TRANSITION_VIOLATION`（相邻固定 lead 违反转换规则）、
  `UNSATISFIABLE_CONSTRAINTS`（配额与转换规则联合无解：不存在同时满足
  两者的 lead 分配，创建即拒绝而非留待枚举得到 0 个方案）、
  `CONSTRAINT_CHECK_LIMIT`（联合校验超出状态预算、无法确认可行性，
  fail-closed 拒绝，未校验的 touch 不落库）

### 枚举与排序

touch 的 lead 可写 `{"choice": ["plain", "bob", "single"]}`（call 槽位）与
`{"method_choice": ["pb-minor", "alt-minor"]}`（方法槽位）；`enumerate`
展开全部 call×method 组合，先按**配额 → 相邻转换 → max_calls** 剪枝
（分别计数 `pruned_by_quota` / `pruned_by_transition` / `filtered_by_max_calls`），
再逐组合证明。多方法 touch 按 **为真 → 回到 rounds → 拼接较少 →
方法分布更均衡（用量极差）→ 改动数 → 总 change 数** 排序；单方法 touch
保持 **改动数 → 总 change 数 → rounds 回归** 排序。

`max_variants` 限制组合总数（超出返回 422 `TOO_MANY_VARIANTS`，响应同样
携带 `checked` / `truncated` / `truncation_reason`）；`max_search` 限制
搜索预算，超出时截断并返回 `checked`（已检查组合数）、`truncated: true`
与 `truncation_reason`。同一 touch 版本重复枚举结果一致。

### 音乐模式评分

评分方案按钟数创建、版本不可变，组合三类规则（每条规则设 `name`、
`points`（可为负）、`allow_overlap`、`max_per_row`）：

```json
{
  "id": "music-6", "name": "六钟音乐", "stage": 6,
  "rules": [
    {"type": "row", "name": "queens-ish", "points": 10, "row": "135264"},
    {"type": "run", "name": "back-56", "points": 2, "bells": [5, 6], "position": "back"},
    {"type": "run", "name": "front-654", "points": 3, "bells": [6, 5, 4], "position": "front"},
    {"type": "positions", "name": "5-front", "points": 1,
     "allow_overlap": true, "max_per_row": 2,
     "positions": [{"bell": 5, "position": 1}, {"bell": 6, "position": 6}]}
  ],
  "score_start_row": false,
  "score_final_rounds": false
}
```

- `row` — 整行与指定排列完全一致（须为 1..stage 的完整排列）；
- `run` — 正序或逆序连续钟组出现在 row 前端（`front`）或后端（`back`）；
- `positions` — 每满足一个 (钟, 位置) 对计一次命中；`allow_overlap=false`
  时每行至多计 1 次，`max_per_row` 限制每条规则从单个 row 计分的命中次数。

创建时拒绝非法规则（不落库）：不完整排列（`ROW_NOT_PERMUTATION` /
`ROW_LENGTH_MISMATCH`）、越界钟号（`BELL_OUT_OF_RANGE`）、与钟数不符的
位置（`POSITION_OUT_OF_RANGE`）、非连续钟组（`RUN_NOT_CONSECUTIVE`）、
重复钟号/位置对/规则名等。

证明时在请求体引用方案：`POST /touches/{id}/versions/{v}/prove`
`{"music": {"id": "music-6"}}`（`version` 留空则随 touch 版本冻结为首次
使用时的最新版本）。结果新增 `music` 段：`total_score`、各规则 `hits` /
`score`、`rows_scored`，以及 `first_scoring_row` / `highest_scoring_row`
（含 change、lead、method、call 来源；平分取最早 row）。起始 row 与末尾
rounds 是否计分由方案的 `score_start_row` / `score_final_rounds` 指定，
其余 row（含中间回到的 rounds）一律计分。方案钟数与 touch 不符返回 422
`MUSIC_STAGE_MISMATCH`。

枚举时同样可引用方案，并可设 `min_music_score` / `min_music_hits` 门槛：
在配额、转换、max_calls 筛选之后按门槛过滤（计数 `filtered_by_music`），
变体按 **真值 → rounds 回归 → 音乐分（高者优先）→ 既有排序项** 稳定排序，
每个变体携带 `music_score` / `music_hits`。设置门槛时必须引用方案。

### all-the-work 覆盖分析

覆盖方案按钟数创建、版本不可变：`working_bells` 选定工作钟，`methods`
列出每个工作钟应经历的方法版本（`version` 留空则冻结为创建时最新版本），
`cells` 逐格设置最低 lead 次数；`place_bell` 留空表示全部 place-bell 类别
（1..stage），保存时展开为逐类别格。

```json
{
  "id": "atw-minor", "name": "Minor all-the-work", "stage": 6,
  "working_bells": [2, 3, 4, 5, 6],
  "methods": [{"id": "pb-minor"}],
  "cells": [
    {"bell": 2, "method": "pb-minor", "place_bell": 4, "min_leads": 1}
  ]
}
```

- **place-bell 语义**：一口钟在某 lead 中敲第 p 号 place bell，当且仅当该
  lead 开始前的实际排列（lead head）中这口钟位于第 p 个位置。因此 call
  改变后续 lead head 后，各钟的 place-bell 归属自动按实际位置计数。
- 创建时拒绝（不落库）：重复格 `DUPLICATE_CELL`（含展开后类别相交）、
  越界/重复工作钟、覆盖格引用 `working_bells`/`methods` 之外的钟或方法、
  `COVERAGE_STAGE_MISMATCH`（方法钟数与方案不一致）、方法版本缺失（404）。
- 接口：`POST /coverage-schemes`、`GET /coverage-schemes`、
  `GET /coverage-schemes/{id}`、`GET /coverage-schemes/{id}/versions/{v}`。

证明时在请求体引用：`POST /touches/{id}/versions/{v}/prove`
`{"coverage": {"id": "atw-minor"}}`（`version` 留空则随 touch 版本冻结为
首次使用时的最新版本）。结果新增 `coverage` 段：

- `matrix` — bell×method×place-bell 覆盖矩阵：每格 `count`（覆盖 lead 数）、
  `first_lead` / `last_lead`（首次/末次出现的 lead）、`gaps` / `longest_gap`
  （相邻出现的 lead 间隔）、`required`；
- `cells` — 方案要求格与观测连接：附加 `min_leads`、`deficit`（未达最低
  次数的缺口）、`satisfied`；
- `missing_cells`（从未出现的要求格）/ `deficits`（未达标格）；
- `bells` — 各工作钟完成率、要求/观测/封顶 lead 数与满足格数；
- `completion`（按最低 lead 次数封顶的总完成率）、`cell_completion`（满足
  格占比）、`balance`（各钟完成率极差，越小越均衡）、`full_coverage`；
- `unmatched_methods` — 使用了方案外方法（id 或版本不符）的 lead，不计入矩阵。

枚举时可引用覆盖方案并要求 `require_full_coverage`（仅保留全覆盖变体）或
设置 `min_completion`（0~1 最低完成率，边界值达标）：不达标变体在既有
约束筛选之后被过滤（计数 `filtered_by_coverage`），变体按
**覆盖完成率（高者优先）→ 覆盖均衡度（极差小者优先）→（音乐分）→
真值 → rounds 回归 → 既有排序项** 稳定排序，每个变体携带紧凑的 `coverage`
指标。设置门槛时必须引用方案。

覆盖方案版本随 touch 冻结，证明结果按 (touch 版本, 音乐方案, 覆盖方案)
六元组缓存：重复证明命中缓存且与枚举中该变体的覆盖结果一致。

## 部分 touch：前缀校核与续接

作曲进行到一半时，可把**已经敲出的 row 前缀**提交校核，再让系统搜索
不与前缀重复的续接尾段。前缀与续接均为不可变版本，依赖版本
（方法、call、评分方案、`ringproof_version`）随前缀冻结。

### 提交前缀：`POST /prefixes`

两种模式（二选一）：

- **显式 rows + lead 标注**：`stage`、`methods`（版本留空则冻结为最新）、
  `calls`、`leads`（逐个 lead 标注 `method` 与 `call`）与 `rows`
  （逐 change row，**不含起始 row**）。`rows` 长度可为 1..标注 lead 的
  change 总数之间的任意值：止于最后一个 lead 中途时，该 lead 记为
  **部分 lead**（响应 `partial_lead` 给出方法、已敲/剩余 change 数；
  `ends_at_lead_end` 为 false），续接时会先**强制敲完该 lead 的剩余
  change**再扩展完整 lead。
- **引用不可变 touch**：`from_touch: {touch_id, touch_version, up_to_change}`，
  `up_to_change` 可位于 lead 中途（超出范围返回 422 `CHANGE_OUT_OF_RANGE`）；
  方法、call、起始排列全部取自该 touch 版本。

重放时逐 change 校核：排列完整性（`ROW_NOT_PERMUTATION`）、相邻换位
（`NOT_ADJACENT`）、实际 places 与标注方法/call 的记号一致性
（`NOTATION_MISMATCH`），并定位首个重复 row（`REPEATED_ROW`，末尾回到
rounds 属正常回归）。校核失败返回 422 `PREFIX_INVALID`，响应体 `prefix`
段仍给出首个非法处的 `at_change`、期望来源（method/lead/call/记号）、
已接受 change 数与**冻结的当前排列**；非法前缀不落库。

合法前缀返回 201 并冻结：

- `current_row` / `seen_rows` — 当前排列与已出现的全部 row（续接搜索的禁重集合）；
- `lead_methods` / `lead_end_changes` — 逐 lead 方法与已完成 lead 的 lead end change 序号；
- `partial_lead` / `ends_at_lead_end` — 末尾部分 lead 的方法/call/已敲/剩余
  change 数（止于 lead end 时分别为 null / true）；
- `frozen`、`dependencies` — 冻结状态与方法/call/ringproof 版本；
- `events` — 逐行来源（change、lead、记号、实际/期望 places、方法 id/版本、
  call、`splice` 标记）。

另有 `GET /prefixes/{id}/versions/{v}` 与
`GET /prefixes/{id}/versions/{v}/rows`（分页）。

### 续接搜索：`POST /prefixes/{id}/versions/{v}/continuation`

| 参数 | 说明 |
|---|---|
| `max_leads` | 剩余 lead 数上限（默认 12） |
| `target_row` | 目标 row（缺省 rounds），须出现在尾段末 row |
| `methods` | 尾段可用方法；缺省沿用前缀方法（版本随前缀冻结） |
| `calls` | 尾段新增 lead 的可用 call；以前缀 call 为底、同名覆盖/新增。**同名覆盖不影响部分 lead 的强制余段**（始终用前缀冻结时的 call 定义） |
| `max_calls` | 尾段 call 数上限 |
| `method_quotas` | 各方法尾段用量 min/max（按尾段 lead 计，**不**计前缀用量与强制完成的部分 lead） |
| `allowed_transitions` / `forbidden_transitions` | 相邻方法转换白/黑名单；**同时约束前缀末 lead 方法 → 第一个尾段 lead 方法**，同方法延续始终允许（白名单只约束跨方法转换） |
| `max_results` / `max_search` | 返回方案上限（默认 50）/ 搜索状态预算（默认 20000） |
| `music` + `min_music_score` / `min_music_hits` | 引用评分方案（版本随前缀冻结）与门槛 |

搜索固定分支顺序（方法按声明顺序、call 按 plain 优先再按名称）做深度优先
枚举，因此**同一前缀与约束重复请求结果一致**（按请求内容哈希缓存，
`X-Continuation-Cache: hit/miss`）。剪枝计数 `pruned_by_quota` /
`pruned_by_transition` / `pruned_by_repeat` / `filtered_by_max_calls` /
`filtered_by_music`。**每个被访问且未超 max 配额的 lead end 都会成为候选**：
部分 lead 前缀先产生一个 `num_leads=0` 的强制段候选（只含强制敲完的剩余
change），之后每个完整 lead 末端都入 `results`——**无论是否到达目标**，
未到目标候选以 `reached_target: false` 标记并排到所有到达方案之后。每个结果：

- `forced_remainder` — 部分 lead 的强制剩余段（方法、版本、call、剩余
  change 数与强制段末 row），其记号沿用前缀冻结时的 call 定义，不受续接
  请求中同名 call 覆盖影响；无部分 lead 时为 null；
- `leads` — 尾段完整 lead，逐个标明 `lead`（全局序号）、
  `method`/`method_version`、`call`；
- `rows` / `events` — 强制段+尾段的逐 row 与逐 change 来源（全局 change
  编号、lead、记号、方法 id/版本、call、`forced_remainder`/`splice` 标记）；
- `num_leads` / `num_changes` / `num_calls` / `num_splices`（`num_splices`
  与逐 row 事件的 `splice` 标记一致，含前缀末 lead → 第一个尾段 lead 的边界拼接）；
- `method_counts` 与 `quota_remaining`（各方法已用/剩余 min/max）；
- `reached_target` / `target_row` 与 `music_score` / `music_hits`（引用方案时）。

稳定排序：**到达目标 → 尾段完整 lead 数 → call 数 → 拼接数 → change 数 →
音乐分（高者优先）→ 确定性字典序**。无解或预算耗尽时返回
`deepest_progress`（最深 lead 数、当前 row 与方法/call 路径）、
`checked_states` 与 `truncation_reason`（穷尽时说明未发现方案，
截断时说明达到 `max_search`；强制剩余段本身重复时返回
`forced_remainder_impossible`）。

> 转换白名单只约束**跨方法**转换；**同方法延续始终允许**（即使白名单
> 非空，也不必显式列出 `["pb","pb"]`）；黑名单始终优先于一切放行。

## 呼叫位置写法：位置方案、composition 与编译

作曲时常用“在 Home / Wrong / Middle 处叫 bob/single”的呼叫位置写法。
本服务把这种写法编译成逐 lead 的 touch，再走同一套证明流程。

### 1. 位置方案（不可变版本，按方法版本冻结）

```json
POST /position-schemes
{
  "id": "tenor-6", "name": "tenor 观察钟位置",
  "method_id": "pb-minor",
  "observer": 6,
  "positions": {"Home": 6, "Wrong": 4, "Middle": 2},
  "call_positions": [
    {"call": "single", "positions": {"Wrong": 4}}
  ],
  "home_symbol": "Home"
}
```

- `observer` 为观察钟（按钟号）；`positions` 把位置符号映射到 **call 结束
  （lead end）后观察钟所在位置**（1 起，1 为前端）；
- `call_positions` 给出某个 call 的专属映射，未列出的符号回落 `positions`；
- `home_symbol` 指定 home（course end）符号。创建时校验：观察钟/位置不越界
  （`BELL_OUT_OF_RANGE` / `POSITION_OUT_OF_RANGE`）、home 符号已映射
  （`HOME_SYMBOL_MISSING`）、观察钟能在 plain course 内回到 home
  （`HOME_NOT_REACHABLE`），非法方案不落库。

### 2. composition（可复用 part + 逐 token 呼叫）

```json
POST /compositions
{
  "id": "comp", "scheme": {"id": "tenor-6"},
  "calls": {"bob": {"notation": "14", "replace": 1},
            "single": {"notation": "1234", "replace": 1}},
  "parts": [
    {"name": "A", "repeat": 1, "tokens": [
      {"symbol": "Wrong", "call": "bob", "plain_leads": 1},
      {"symbol": "Home", "call": "bob", "plain_leads": 0}
    ]}
  ]
}
```

- composition 由有序的可复用 **part** 组成，每个 part 可 `repeat`；part 内
  token 按顺序排列，记录位置符号与 bob/single/自定义 call（`call: null`
  表示由 plain lead 结束）；
- `plain_leads` 精确指定此 call 之前经过的 plain lead 数；`max_plain_leads`
  给出搜索上界；两者皆缺省时在一个 course 窗口内枚举匹配；
- 创建时拒绝未声明 call（`UNKNOWN_CALL`）、方案未映射的符号
  （`UNKNOWN_SYMBOL`）与超窗限额（`PLAIN_LEADS_OUT_OF_COURSE`），
  并冻结位置方案与方法版本。

### 3. 编译

```bash
curl -X POST localhost:8765/compositions/comp/versions/1/compile \
  -H 'Content-Type: application/json' \
  -d '{"course_head": "123456", "touch_id": "touch-A"}'
```

系统从 `course_head`（缺省 rounds）起逐 lead 推演：先铺 plain lead 再接
call lead，取 call 结束后观察钟位置唯一命中符号的候选。每个 part 的 token
解析完后补 plain lead 至观察钟第一次回到 home，下一 part 从该 course head
继续。失败（HTTP 422）时返回：

| 错误码 | 含义 |
|---|---|
| `POSITION_UNREACHABLE` | 限额内没有 lead 能命中符号位置（返回出错 token、候选 lead、当前排列） |
| `AMBIGUOUS_POSITION` | 限额内多个 lead 命中（返回 `matching_leads` 与全部候选，须收紧 plain lead 限额） |
| `PART_MISMATCH` | part 最后一个 call 后，course 窗口内观察钟回不到 home（返回该 part 最后一个 token、候选 lead、当前排列） |
| `SYMBOL_NOT_MAPPED` | 符号在方案中没有映射（该 call 亦无专属映射） |
| `PLAIN_COURSE_NOT_BOUND` | 起点无法由观察钟自动界定 plain course，须显式给 `course_length` |

成功响应逐 lead 给出编译标注：

- `compiled_tokens` — 每个 token 解析到的 part/token、符号、call、此前
  plain lead 数、part 内 lead 序号与命中的观察钟位置；
- `compiled_leads` — 逐 lead 的 `part` / `part_repeat` / `part_name` /
  `token` / `call` / `symbol` / `position` / `lead_head_before` /
  `lead_head_after` / `observer_bell`（自动补的 plain lead 的 `token` 为 null）；
- `course_heads` / `course_lengths`、`final_row`、`rounds_return`；
- `proof`（真值、rounds 回归、首次重复定位等）与逐行 `events`；
- `touch` — 另存的不可变 touch 版本；`dependencies` — ringproof、方法、
  位置方案、composition 与各 call 的冻结版本。

编译缓存键包含 course head、course 长度、`touch_id`、是否保存等全部请求
输入：**同一份 composition 编译到不同 `touch_id` 会各自落库为独立 touch，
互不串用缓存**；同一请求重复编译命中缓存（`X-Compile-Cache: hit`）。
另存的 touch 可直接用既有 `/touches/.../prove`、`/rows` 接口，且
`GET /touches/{id}/versions/{v}` 读回时每个 lead 仍完整带 part、token、
position、前后 lead head 与观察钟标注（`generated_by`、`compiled_tokens`、
`course_heads` 一并读回）。

## multipart composition 校核

multipart composition 把同一个区段（part）反复敲响：第 k 段的逐 row 是
第一段对应 row 经**钟置换** φ 改名后的像（change 作用于位置、φ 作用于
钟，两者可交换）。因此调用方只需提交一个 part，系统按置换展开整首
composition，各 part 无须重复提交 row。

### 1. 创建分析（不可变版本，冻结 touch/方法/call 依赖）

```json
POST /multipart-analyses
{
  "id": "mp",
  "touch": {"id": "pc"},
  "part_start_change": 0,
  "part_end_change": 12,
  "expected_parts": 5,
  "fixed_bells": [1]
}
```

- `touch` 引用不可变 touch（`version` 留空则冻结为创建时最新版本）；
- `part_start_change` / `part_end_change` 为区段起止 change 序号，须落在
  lead end（起始 change `0` 表示 touch 起始 row），否则 422 `NOT_LEAD_END`；
  超出 touch 范围返回 422 `CHANGE_OUT_OF_RANGE`；
- `expected_parts` 为预期 part 数（置换反复作用的次数）；
- `fixed_bells` 为必须保持原位的钟：区段置换须将其固定（越界 422
  `BELL_OUT_OF_RANGE`）；展开规模超限返回 422 `TOO_MANY_CHANGES`。

### 2. 分析结果

- `permutation` — 区段置换：`images`（各钟的像）、`cycles`（循环分解）、
  `fixed_bells`（被固定的钟）、`order`（阶，即轨道长度）；
- `part_ends` — 各 part end 排列（是否为起点/rounds）；
- `early_returns` — 提前回到起点（part end 在预期 part 数之前回到区段首
  row）的 part 位置；
- `closes_as_expected` / `inconsistent_bells` — 轨道不能按预期闭合
  （φ^expected ≠ 恒等）时，逐个指出不一致钟位的像与起止位置；
- `fixed_bell_checks` / `fixed_bells_satisfied` — 保持原位钟的逐个校核；
- `truth` / `first_conflict` — 按置换展开整首 composition 后检查 part 内
  及跨 part 重复；冲突给出 row 与双方来源（`part`、`change_in_part`、整首
  `change`、对应的 `touch_change`、`lead`、`change_in_lead`、`method`、
  `call`、`notation`）；末 part 末 row 回到起点属正常闭合，不算重复；
- `dependencies` — 冻结的 touch、方法、call 与 ringproof 版本；相同输入
  重复计算保持一致。

### 3. 枚举整首为真的区段

```bash
curl -X POST localhost:8765/multipart-analyses/mp/versions/1/enumerate \
  -H 'Content-Type: application/json' \
  -d '{"parts": 5, "max_results": 50}'
```

在分析冻结的 touch 中枚举全部首尾落在 lead end 的候选区段（起始 change
`0` 含在内）：先按 fixed bells（继承自分析）与**轨道长度**剪枝——part 数
须等于置换的阶才能整首闭合且不重复，`parts` 或 `min_parts`/`max_parts`
限定范围——再做**行交集**扫描（计数 `pruned_by_fixed_bells` /
`pruned_by_orbit` / `pruned_by_size` / `pruned_by_rows`），只保留整首为真
的结果，沿用 touch 中的位置顺序（`start_change`、`end_change`）返回。
`max_results` / `max_search` 超出时截断并给出 `truncation_reason`；相同
请求重复枚举命中缓存（`X-Multipart-Cache: hit`），结果一致。

## 可复用 block 拼装

把多个 touch 的 lead-end 区段当作可复用积木：每个 block 保存时转为
**相对起点的位置置换** C（末 row 第 i 位的钟来自区段起点第 C[i] 位；
change 作用于位置、与起点无关），因此同一 block 可从任意 lead head
展开——从任意 row 出发施加同一串 change，末 row 由同一位置置换唯一
确定（规范化摘要同时给出相对起点的钟置换 φ）。组合搜索把 block 逐个
衔接，检查跨 block 重复，找出满足全部约束的拼装方案。

### 1. 创建 block composition（不可变版本，冻结 touch/方法/call 依赖）

```json
POST /block-compositions
{
  "id": "bc",
  "blocks": [
    {"id": "A", "touch": {"id": "pc"}, "start_change": 0, "end_change": 12,
     "max_uses": 5},
    {"id": "B", "touch": {"id": "pc2"}, "start_change": 0, "end_change": 12}
  ],
  "target_row": "123456",
  "min_changes": 0,
  "max_changes": 60,
  "forbidden_transitions": [["B", "A"]]
}
```

- 每个 block 引用一个不可变 touch（`version` 留空则冻结为创建时最新），
  `start_change`/`end_change` 须落在 lead end（起始 change `0` 为 touch
  起始 row），否则 422 `NOT_LEAD_END`；超出 touch 范围返回 422
  `CHANGE_OUT_OF_RANGE`；`min_uses`/`max_uses` 限定组合中的使用次数
  （`max_uses` 为 null 表示不限，受总 change 数约束）；
- 创建时拒绝（不落库）：`STAGE_MISMATCH`（各 touch 钟数不一致）、
  `BLOCK_NOT_TRUE`（区段从自身起点展开即含重复 row，末 row 回到区段起点
  属正常闭合）、`UNKNOWN_BLOCK`（衔接规则引用未声明 block）、
  `UNSATISFIABLE_LENGTH`（各 block 最少用量所需 change 超出
  `max_changes`）；
- `allowed_transitions`（白名单）/ `forbidden_transitions`（黑名单）约束
  相邻 block 的衔接；同 block 延续始终允许，黑名单优先；
- 响应给出各 block 的规范化摘要（区段、相对置换 `permutation`、call 数、
  方法列表）与 `dependencies`（冻结的 touch、方法、call 与 ringproof 版本）。

### 2. 搜索 block 组合

```bash
curl -X POST localhost:8765/block-compositions/bc/versions/1/search \
  -H 'Content-Type: application/json' -d '{"max_results": 50}'
```

深度优先枚举（block 按声明顺序分支），按展开后的逐 row 检查**跨 block
重复**（到达目标即终止该路径；仅当目标即起点时，组合末 row 回到起点
属正常闭合 come-round，不算重复——其余任何重复，含目标末行撞上路径中
已出现的 row，一律按冲突剪枝），只返回满足用量、衔接、长度与末行要求
的组合：

- 剪枝：**端点可达性**（当前 row 到目标的最少 block 数，自目标反向 BFS
  的安全下界）→ **剩余长度**（补足最少用量所需与可达 change 范围）→
  **行交集**，分别计数 `pruned_by_reachability` / `pruned_by_length` /
  `pruned_by_rows`（另有 `pruned_by_usage` / `pruned_by_transition`）；
- 排序：**目标达成 → 总 change 数 → block 数 → call 数**（→ 确定性
  字典序），`max_results` 保留最优；
- `first_conflict` — 搜索中首次行交集冲突的两侧来源：block 序号与 id、
  touch id/版本、组合内 change 与 block 内 change、对应的 `touch_change`、
  `lead`、`method`/`method_id`/`method_version`、`call`、`notation`；
- 每个结果给出 `sequence`（block id 序列）、`segments`（逐 block 起止
  row、change 与 call 数）、`blocks_used`、`num_blocks` / `total_changes` /
  `num_calls`、`final_row` 与 `target_reached`；
- `max_search` 超出时截断并给出 `truncation_reason`；相同请求重复搜索
  命中缓存（`X-Block-Search-Cache: hit`），结果一致。

## 示例

```bash
# 1. 定义方法（Cambridge Surprise Minor，对称记号 + lead end）
curl -X POST localhost:8765/methods -H 'Content-Type: application/json' -d \
  '{"id":"cambridge-minor","name":"Cambridge Surprise Minor","stage":6,
    "notation":"&x3x4x2x3x4x5,+2"}'

# 2. 定义 touch（Plain Bob Minor，5 个 plain lead 的 plain course）
curl -X POST localhost:8765/touches -H 'Content-Type: application/json' -d \
  '{"id":"pc","method_id":"pb-minor",
    "calls":{"bob":{"notation":"14","replace":1},
             "single":{"notation":"1234","replace":1}},
    "sequence":[{"leads":[{"call":null}],"repeat":5}]}'

# 3. 证明
curl -X POST localhost:8765/touches/pc/versions/1/prove
# → total_changes 60, truth "true", rounds_return true, rounds_at [0, 60]

# 4. 定义评分方案（精确 row + 后端连续钟组 + 指定钟位置）
curl -X POST localhost:8765/music-schemes -H 'Content-Type: application/json' -d \
  '{"id":"music-6","name":"六钟音乐","stage":6,
    "rules":[{"type":"row","name":"queens-ish","points":10,"row":"135264"},
             {"type":"run","name":"back-56","points":2,"bells":[5,6],"position":"back"},
             {"type":"positions","name":"5-front","points":1,"allow_overlap":true,
              "positions":[{"bell":5,"position":1},{"bell":6,"position":6}]}]}'

# 5. 引用评分方案证明（总分、各规则命中数、首次/最高分 row 来源）
curl -X POST localhost:8765/touches/pc/versions/1/prove \
  -H 'Content-Type: application/json' -d '{"music":{"id":"music-6"}}'

# 6. 多方法拼接：两个方法 + 配额 + 禁止 alt→pb 转换 + 候选槽位
curl -X POST localhost:8765/touches -H 'Content-Type: application/json' -d \
  '{"id":"spliced","methods":[{"id":"pb-minor"},{"id":"alt-minor"}],
    "calls":{"bob":{"notation":"14","replace":1}},
    "method_quotas":{"alt-minor":{"max":2}},
    "forbidden_transitions":[["alt-minor","pb-minor"]],
    "sequence":[{"leads":[{"method":"pb-minor"},
      {"method_choice":["pb-minor","alt-minor"],"choice":["plain","bob"]},
      {"method":"pb-minor"}]}]}'

# 7. 枚举候选组合（剪枝 + 排序 + 截断说明）
curl -X POST localhost:8765/touches/spliced/versions/1/enumerate \
  -H 'Content-Type: application/json' -d '{}'
# → total_combos 4, pruned_by_transition 2, variants 按真值/rounds/拼接数排序

# 8. 枚举 + 音乐门槛：低于最低总分的变体被过滤并计数
curl -X POST localhost:8765/touches/spliced/versions/1/enumerate \
  -H 'Content-Type: application/json' \
  -d '{"music":{"id":"music-6"},"min_music_score":10}'
# → sorted_by 以 music_score 优先于拼接数，filtered_by_music 给出被过滤数量

# 8b. 创建 all-the-work 覆盖方案：工作钟 2，在方法下敲全部 place-bell 类别各 ≥1 lead
curl -X POST localhost:8765/coverage-schemes -H 'Content-Type: application/json' -d \
  '{"id":"atw-2","name":"钟2 all-the-work","stage":6,
    "working_bells":[2],"methods":[{"id":"pb-minor"}],
    "cells":[{"bell":2,"method":"pb-minor","min_leads":1}]}'
# → cells 展开为 place_bell 1..6 六格（1 号类别在 Plain Bob 中钟 2 不会敲到）

# 8c. 证明时引用覆盖方案：bell×method×place-bell 矩阵、首末次 lead、缺口、完成率
curl -X POST localhost:8765/touches/pc/versions/1/prove \
  -H 'Content-Type: application/json' -d '{"coverage":{"id":"atw-2"}}'
# → coverage.matrix 每格含 count/first_lead/last_lead/gaps；钟 2 覆盖 2/4/6/5/3
#   completion 5/6，missing_cells=[place_bell 1]，bells 给出各钟完成率
# 重复证明命中缓存（X-Proof-Cache: hit）；方案版本随该 touch 冻结

# 8d. 枚举时按覆盖门槛过滤：仅保留全覆盖变体，其余计入 filtered_by_coverage
curl -X POST localhost:8765/touches/spliced/versions/1/enumerate \
  -H 'Content-Type: application/json' \
  -d '{"coverage":{"id":"atw-2"},"require_full_coverage":true}'
# → sorted_by 以 coverage_completion、coverage_balance 开头；
#   也可改用 "min_completion":0.9 设最低完成率（边界值达标）

# 9. 提交已敲出的 3 个 plain lead（36 row）前缀，按 lead 标注方法/call
curl -X POST localhost:8765/prefixes -H 'Content-Type: application/json' -d \
  '{"id":"pfx","stage":6,"methods":[{"id":"pb-minor"}],
    "calls":{"bob":{"notation":"14","replace":1}},
    "leads":[{"call":null},{"call":null},{"call":null}],
    "rows":["214365","... 共 36 个 row ..."]}'
# → valid true，冻结 current_row、seen_rows 与方法/ringproof 版本

# 10. 搜索续接到 rounds 的尾段（≤3 lead），并引用音乐方案排序
curl -X POST localhost:8765/prefixes/pfx/versions/1/continuation \
  -H 'Content-Type: application/json' \
  -d '{"max_leads":3,"music":{"id":"music-6"}}'
# → results[0] 为剩余 2 个 plain lead，尾 row=123456，逐 lead/row 标来源与配额余量

# 11. 也可直接引用不可变 touch 的任意 change 作为前缀（可在 lead 中途）
curl -X POST localhost:8765/prefixes -H 'Content-Type: application/json' -d \
  '{"from_touch":{"touch_id":"pc","touch_version":1,"up_to_change":10}}'
# → partial_lead 给出 consumed=10/lead_length=12，续接时先强制敲完剩余 2 个 change

# 12. multipart 校核：取 plain course 的第一个 lead（change 0..12）作为 part，
#     预期 5 个 part、钟 1 保持原位
curl -X POST localhost:8765/multipart-analyses -H 'Content-Type: application/json' -d \
  '{"id":"mp","touch":{"id":"pc"},"part_start_change":0,"part_end_change":12,
    "expected_parts":5,"fixed_bells":[1]}'
# → permutation.cycles=[[2,3,5,6,4]]、order=5；part_ends 依次为
#   135264/156342/164523/142635/123456；closes_as_expected=true、truth="true"

# 13. 枚举整首为真的 multipart 区段（限定 5 个 part）
curl -X POST localhost:8765/multipart-analyses/mp/versions/1/enumerate \
  -H 'Content-Type: application/json' -d '{"parts":5}'
# → 5 个结果（每个 1-lead 区段、parts=5），pruned_by_orbit/pruned_by_rows
#   给出剪枝计数；重复请求命中缓存（X-Multipart-Cache: hit）
```

## 测试

```bash
python3 -m pytest tests/ -q   # 209 个用例
```
