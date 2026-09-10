# RingProof — 变换鸣钟 touch 序列证明 API

供变换鸣钟（change ringing）作曲者校核 touch 的本机服务。调用方定义方法
（place notation）、起始排列与 lead 顺序，在 lead end 配置 bob / single /
自定义 call；API 展开对称记号与重复段，逐 row 生成序列并完成证明：
排列完整性、记号一致性、rounds 回归、首次重复定位（标明 method / lead /
call 来源），并可枚举 call 变体。方法与 touch 以不可变版本保存。

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
| POST | `/touches` | 创建 touch 版本（起始排列、calls、lead 顺序、max_calls） |
| GET | `/touches/{id}/versions/{v}` | 规范化后的 touch 与输入哈希 |
| GET | `/touches/{id}/versions/{v}/rows` | 逐行来源（change、lead、记号、call 来源），支持分页 |
| POST | `/touches/{id}/versions/{v}/prove` | 序列证明（按版本缓存，`X-Proof-Cache` 头标识命中） |
| POST | `/touches/{id}/versions/{v}/enumerate` | 枚举 choice 槽位的 call 变体 |

### 证明结果字段

- `permutation_complete` / `notation_consistent` — 排列完整性 / 记号一致性
- `rounds_return` / `rounds_at` — rounds 回归及位置
- `truth` — 无重复 row 为 `true`（终点回到起始 rounds 属正常回归）
- `first_repeat` — 首次相同 row 的两个 change 序号及双方来源
  （method、lead、change_in_lead、notation、call 来源）
- `calls_used` / `exceeds_max_calls` — 替换次数及是否超限
- `lead_heads`、`events`（逐行来源）

### 枚举与排序

touch 的 lead 可写 `{"choice": ["plain", "bob", "single"]}` 作为枚举槽位；
`enumerate` 展开全部组合（可用 `max_calls` 限制替换次数、`max_variants`
限制组合数），结果按 **改动数 → 总 change 数 → rounds 回归** 排序。

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
```

## 测试

```bash
python3 -m pytest tests/ -q   # 34 个用例
```
