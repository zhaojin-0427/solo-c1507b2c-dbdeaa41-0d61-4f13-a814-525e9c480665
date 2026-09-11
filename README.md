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
| POST | `/touches/{id}/versions/{v}/prove` | 序列证明，可引用评分方案（按版本+方案缓存，`X-Proof-Cache` 头标识命中） |
| POST | `/touches/{id}/versions/{v}/enumerate` | 枚举 call×method 候选组合（配额/转换剪枝、音乐门槛过滤、排序、截断说明） |
| POST | `/music-schemes` | 创建评分方案版本（指定钟数；同 id 递增版本，不可变） |
| GET | `/music-schemes/{id}/versions/{v}` | 方案规则、计分开关与输入哈希 |
| POST | `/prefixes` | 提交部分 touch 前缀（显式 rows+leads，或 from_touch 引用任意 change，可在 lead 中途）并重放校核 |
| GET | `/prefixes/{id}/versions/{v}` | 前缀冻结状态（当前排列、已出现 row、lead 方法、依赖版本、逐行事件） |
| GET | `/prefixes/{id}/versions/{v}/rows` | 前缀逐行来源（分页） |
| POST | `/prefixes/{id}/versions/{v}/continuation` | 续接搜索：目标 row、剩余 lead 上限、方法/call、配额与转换规则、音乐评分，稳定排序+截断进度 |

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
```

## 测试

```bash
python3 -m pytest tests/ -q   # 108 个用例
```
