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
| POST | `/touches/{id}/versions/{v}/prove` | 序列证明（按版本缓存，`X-Proof-Cache` 头标识命中） |
| POST | `/touches/{id}/versions/{v}/enumerate` | 枚举 call×method 候选组合（配额/转换剪枝、排序、截断说明） |

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
  `TRANSITION_VIOLATION`（相邻固定 lead 违反转换规则）

### 枚举与排序

touch 的 lead 可写 `{"choice": ["plain", "bob", "single"]}`（call 槽位）与
`{"method_choice": ["pb-minor", "alt-minor"]}`（方法槽位）；`enumerate`
展开全部 call×method 组合，先按**配额 → 相邻转换 → max_calls** 剪枝
（分别计数 `pruned_by_quota` / `pruned_by_transition` / `filtered_by_max_calls`），
再逐组合证明。多方法 touch 按 **为真 → 回到 rounds → 拼接较少 →
方法分布更均衡（用量极差）→ 改动数 → 总 change 数** 排序；单方法 touch
保持 **改动数 → 总 change 数 → rounds 回归** 排序。

`max_variants` 限制组合总数（超出返回 422 `TOO_MANY_VARIANTS`）；
`max_search` 限制搜索预算，超出时截断并返回 `checked`（已检查组合数）、
`truncated: true` 与 `truncation_reason`。同一 touch 版本重复枚举结果一致。

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

# 4. 多方法拼接：两个方法 + 配额 + 禁止 alt→pb 转换 + 候选槽位
curl -X POST localhost:8765/touches -H 'Content-Type: application/json' -d \
  '{"id":"spliced","methods":[{"id":"pb-minor"},{"id":"alt-minor"}],
    "calls":{"bob":{"notation":"14","replace":1}},
    "method_quotas":{"alt-minor":{"max":2}},
    "forbidden_transitions":[["alt-minor","pb-minor"]],
    "sequence":[{"leads":[{"method":"pb-minor"},
      {"method_choice":["pb-minor","alt-minor"],"choice":["plain","bob"]},
      {"method":"pb-minor"}]}]}'

# 5. 枚举候选组合（剪枝 + 排序 + 截断说明）
curl -X POST localhost:8765/touches/spliced/versions/1/enumerate \
  -H 'Content-Type: application/json' -d '{}'
# → total_combos 4, pruned_by_transition 2, variants 按真值/rounds/拼接数排序
```

## 测试

```bash
python3 -m pytest tests/ -q   # 56 个用例
```
