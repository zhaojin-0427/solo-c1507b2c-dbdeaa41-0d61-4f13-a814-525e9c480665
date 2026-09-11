"""部分 touch 前缀校核与续接搜索的 API 端到端测试。

覆盖：显式 rows+leads 前缀重放、非法相邻换位/非排列/记号不符/重复 row 定位、
from_touch 引用、版本冻结与不可变、续接 DFS 排序与确定性、call 数/方法配额/
转换规则（含前缀边界）剪枝、音乐评分门槛、截断与无解进度、缓存一致性。
"""
import pytest
from fastapi.testclient import TestClient

from ringproof.main import create_app

PB_MINOR = "x16x16x16x16x16x12"
ALT_MINOR = "x16x14x16x14x16x12"
CALLS = {
    "bob": {"notation": "14", "replace": 1},
    "single": {"notation": "1234", "replace": 1},
}


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def methods(client):
    for mid, name, notation in [
        ("pb-minor", "Plain Bob Minor", PB_MINOR),
        ("alt-minor", "Alt Bob Minor", ALT_MINOR),
    ]:
        r = client.post(
            "/methods",
            json={"id": mid, "name": name, "stage": 6, "notation": notation},
        )
        assert r.status_code == 201


@pytest.fixture()
def pb_touch(client, methods):
    """5 个 plain lead 的 PB Minor plain course touch。"""
    r = client.post(
        "/touches",
        json={
            "id": "pc",
            "method_id": "pb-minor",
            "calls": CALLS,
            "sequence": [{"leads": [{"call": None}], "repeat": 5}],
        },
    )
    assert r.status_code == 201
    return "pc"


def _touch_rows(client, tid, version=1, limit=1000):
    return [
        e["row"]
        for e in client.get(
            f"/touches/{tid}/versions/{version}/rows", params={"limit": limit}
        ).json()["rows"]
    ]


def _explicit_prefix(client, rows, n_leads, **over):
    body = {
        "stage": 6,
        "methods": [{"id": "pb-minor"}],
        "calls": CALLS,
        "leads": [{"call": None}] * n_leads,
        "rows": rows,
    }
    body.update(over)
    return client.post("/prefixes", json=body)


# ---------------- 前缀校核 ----------------


def test_explicit_prefix_valid_freeze(client, pb_touch):
    rows = _touch_rows(client, pb_touch)[:24]  # 2 个 lead
    r = _explicit_prefix(client, rows, 2, id="pfx")
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["version"] == 1 and p["valid"]
    assert p["accepted_changes"] == 24 and p["total_changes"] == 24
    assert p["current_row"] == rows[-1]
    assert p["lead_end_changes"] == [12, 24]
    assert p["lead_methods"] == ["pb-minor", "pb-minor"]
    assert len(p["seen_rows"]) == 25
    assert p["dependencies"]["ringproof_version"]
    assert p["dependencies"]["methods"][0]["id"] == "pb-minor"
    assert p["methods"][0]["version"] == 1
    # GET 一致
    assert client.get("/prefixes/pfx/versions/1").json() == p


def test_prefix_rows_pagination_and_provenance(client, pb_touch):
    rows = _touch_rows(client, pb_touch)[:12]
    _explicit_prefix(client, rows, 1, id="one").status_code
    page = client.get(
        "/prefixes/one/versions/1/rows", params={"offset": 10, "limit": 5}
    ).json()
    assert page["total_changes"] == 12 and len(page["rows"]) == 2
    last = page["rows"][-1]
    assert last["row"] == rows[-1]
    assert last["method_id"] == "pb-minor" and last["lead"] == 1
    assert last["source"] == "method" and last["call"] is None and last["splice"] is False


def test_prefix_not_adjacent_located(client, pb_touch):
    rows = list(_touch_rows(client, pb_touch)[:12])
    rows[5] = "123456"  # 与上一 row 不可能是相邻换位
    r = _explicit_prefix(client, rows, 1)
    assert r.status_code == 422
    d = r.json()
    assert d["detail"]["code"] == "PREFIX_INVALID"
    err = d["prefix"]["first_error"]
    assert err["at_change"] == 6
    assert err["code"] == "NOT_ADJACENT"
    # 冻结在首个非法处的当前排列（前 5 个 change 已接受）
    assert d["prefix"]["accepted_changes"] == 5
    assert d["prefix"]["current_row"] == _touch_rows(client, pb_touch)[4]


def test_prefix_bad_permutation(client, pb_touch):
    rows = list(_touch_rows(client, pb_touch)[:12])
    rows[0] = "123455"
    r = _explicit_prefix(client, rows, 1)
    assert r.status_code == 422
    err = r.json()["prefix"]["first_error"]
    assert err["code"] == "ROW_NOT_PERMUTATION" and err["at_change"] == 1
    assert client.get("/prefixes").json()["prefixes"] == []  # 非法前缀不落库


def test_prefix_notation_mismatch_wrong_label(client, methods):
    # 实际敲出 PB 的 lead，却标注成 ALT 方法 → places 不符
    r = client.post(
        "/touches",
        json={
            "method_id": "pb-minor",
            "calls": CALLS,
            "sequence": [{"leads": [{"call": None}]}],
        },
    )
    rows = _touch_rows(client, r.json()["id"])
    body = {
        "stage": 6,
        "methods": [{"id": "alt-minor"}, {"id": "pb-minor"}],
        "calls": CALLS,
        "leads": [{"method": "alt-minor", "call": None}],
        "rows": rows,
    }
    resp = client.post("/prefixes", json=body)
    assert resp.status_code == 422
    err = resp.json()["prefix"]["first_error"]
    assert err["code"] == "NOTATION_MISMATCH"
    assert err["expected"]["method_id"] == "alt-minor"
    assert err["actual_places"] is not None


def test_prefix_repeated_row_non_final(client, methods):
    # 6 个 plain lead 共 72 change：rounds 在 60 处回归（非该前缀终点）
    r = client.post(
        "/touches",
        json={
            "method_id": "pb-minor",
            "calls": CALLS,
            "sequence": [{"leads": [{"call": None}], "repeat": 6}],
        },
    )
    rows = _touch_rows(client, r.json()["id"])
    assert len(rows) == 72 and rows[59] == "123456"
    resp = _explicit_prefix(client, rows[:72], 6)
    assert resp.status_code == 422
    err = resp.json()["prefix"]["first_error"]
    assert err["code"] == "REPEATED_ROW"
    assert err["at_change"] == 60 and err["first_seen_at"] == 0


def test_prefix_final_rounds_is_valid(client, pb_touch):
    # 完整 plain course：末 row 回 rounds 属正常回归，前缀合法
    rows = _touch_rows(client, pb_touch)
    r = _explicit_prefix(client, rows, 5, id="full")
    assert r.status_code == 201 and r.json()["valid"]
    assert r.json()["current_row"] == "123456"
    # 续接 0 lead：current 已是目标，直接得到一个空尾段方案
    cont = client.post(
        "/prefixes/full/versions/1/continuation", json={"max_leads": 1}
    ).json()
    zero = [x for x in cont["results"] if x["num_leads"] == 0]
    assert len(zero) == 1
    assert zero[0]["reached_target"] is True
    assert zero[0]["rows"] == [] and zero[0]["events"] == []
    # min 配额不允许空尾段
    cont2 = client.post(
        "/prefixes/full/versions/1/continuation",
        json={"max_leads": 1, "method_quotas": {"pb-minor": {"min": 1}}},
    ).json()
    assert all(x["num_leads"] > 0 for x in cont2["results"])


def test_prefix_may_end_mid_lead(client, pb_touch):
    # 前缀可止于 lead 中途：1 个标注 lead 只敲了 10 个 change
    all_rows = _touch_rows(client, pb_touch)
    r = _explicit_prefix(client, all_rows[:10], 1, id="mid")
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["valid"] and p["ends_at_lead_end"] is False
    pl = p["partial_lead"]
    assert pl == {
        "lead": 1,
        "method_id": "pb-minor",
        "method_version": 1,
        "call": None,
        "consumed": 10,
        "lead_length": 12,
        "remaining": 2,
    }
    assert p["accepted_changes"] == 10 and p["lead_end_changes"] == []
    assert p["lead_methods"] == ["pb-minor"]
    assert p["current_row"] == all_rows[9]

    # 超出标注 lead 总 change 数仍拒绝
    r2 = _explicit_prefix(client, all_rows[:13], 1)
    assert r2.status_code == 422
    err = r2.json()["prefix"]["first_error"]
    assert err["code"] == "PREFIX_LENGTH_MISMATCH"


def test_prefix_unknown_method_and_call(client, pb_touch):
    rows = _touch_rows(client, pb_touch)[:12]
    r = client.post(
        "/prefixes",
        json={
            "stage": 6,
            "methods": [{"id": "pb-minor"}],
            "calls": CALLS,
            "leads": [{"call": "ghost"}],
            "rows": rows,
        },
    )
    assert r.status_code == 422 and r.json()["detail"]["code"] == "UNKNOWN_CALL"
    r2 = client.post(
        "/prefixes",
        json={
            "stage": 6,
            "methods": [{"id": "pb-minor"}],
            "calls": CALLS,
            "leads": [{"method": "nope"}],
            "rows": rows,
        },
    )
    assert r2.status_code == 422 and r2.json()["detail"]["code"] == "UNKNOWN_METHOD"


def test_prefix_versions_immutable(client, pb_touch):
    rows = _touch_rows(client, pb_touch)[:12]
    a = _explicit_prefix(client, rows, 1, id="v").json()
    b = _explicit_prefix(client, rows, 1, id="v").json()
    assert (a["version"], b["version"]) == (1, 2)
    assert a["input_hash"] == b["input_hash"]
    assert client.get("/prefixes/v/versions/1").json() == a


# ---------------- from_touch ----------------


def test_prefix_from_touch(client, pb_touch):
    r = client.post(
        "/prefixes",
        json={
            "id": "ft",
            "from_touch": {
                "touch_id": "pc",
                "touch_version": 1,
                "up_to_change": 36,
            },
        },
    )
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["source"] == {
        "type": "touch",
        "touch_id": "pc",
        "touch_version": 1,
        "up_to_change": 36,
    }
    assert p["accepted_changes"] == 36
    assert p["current_row"] == _touch_rows(client, pb_touch)[35]
    assert len(p["lead_methods"]) == 3


def test_prefix_from_touch_mid_lead(client, pb_touch):
    all_rows = _touch_rows(client, pb_touch)
    r = client.post(
        "/prefixes",
        json={
            "id": "ft",
            "from_touch": {
                "touch_id": "pc",
                "touch_version": 1,
                "up_to_change": 10,  # 位于第 1 个 lead 中途
            },
        },
    )
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["source"] == {
        "type": "touch",
        "touch_id": "pc",
        "touch_version": 1,
        "up_to_change": 10,
    }
    assert p["accepted_changes"] == 10
    assert p["current_row"] == all_rows[9]
    assert len(p["lead_methods"]) == 1
    assert p["partial_lead"]["consumed"] == 10
    assert p["partial_lead"]["remaining"] == 2

    # 续接：先强制敲完剩余 2 个 change，再扩展完整 lead；
    # max_leads=1 时也应返回候选（强制段终点），并标 reached_target
    d = client.post(
        "/prefixes/ft/versions/1/continuation", json={"max_leads": 1}
    ).json()
    assert d["solutions_found"] >= 1
    forced_only = [x for x in d["results"] if x["num_leads"] == 0]
    assert len(forced_only) == 1
    cand = forced_only[0]
    assert cand["forced_remainder"]["remaining_changes"] == 2
    assert cand["forced_remainder"]["method"] == "pb-minor"
    assert len(cand["rows"]) == 2
    assert cand["reached_target"] is False
    assert cand["events"][0]["forced_remainder"] is True
    assert cand["events"][0]["change"] == 11
    assert cand["events"][0]["change_in_lead"] == 11


def test_prefix_from_touch_out_of_range(client, pb_touch):
    r = client.post(
        "/prefixes",
        json={
            "from_touch": {
                "touch_id": "pc",
                "touch_version": 1,
                "up_to_change": 10_000,
            }
        },
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "CHANGE_OUT_OF_RANGE"


def test_prefix_from_touch_not_found(client):
    r = client.post(
        "/prefixes",
        json={"from_touch": {"touch_id": "x", "touch_version": 1, "up_to_change": 12}},
    )
    assert r.status_code == 404


# ---------------- 续接搜索 ----------------


def _prefix_three_leads(client, pb_touch, pid="pfx3"):
    rows = _touch_rows(client, pb_touch)[:36]
    r = _explicit_prefix(client, rows, 3, id=pid)
    assert r.status_code == 201
    return pid


def test_continuation_plain_course_completion(client, pb_touch):
    pid = _prefix_three_leads(client, pb_touch)
    r = client.post(f"/prefixes/{pid}/versions/1/continuation", json={"max_leads": 3})
    assert r.status_code == 200
    d = r.json()
    assert d["truncated"] is False and d["solutions_found"] >= 1
    best = d["results"][0]
    # 剩余 2 个 plain lead 回到 rounds，是最短到达方案
    assert best["reached_target"] is True
    assert best["num_leads"] == 2 and best["num_calls"] == 0
    assert best["rows"][-1] == "123456"
    assert [l["call"] for l in best["leads"]] == ["plain", "plain"]
    # 逐 lead 标明方法/版本/lead/call，全局 lead 号续接前缀
    assert [l["lead"] for l in best["leads"]] == [4, 5]
    assert all(l["method_version"] == 1 for l in best["leads"])
    assert all(ev["method_id"] == "pb-minor" for ev in best["events"])
    assert best["events"][0]["lead"] == 4
    # 同时返回上限内合法但未到目标的候选，且全部排在到达方案之后
    not_target = [x for x in d["results"] if not x["reached_target"]]
    assert not_target
    assert all(not x["reached_target"] for x in d["results"][len([
        x for x in d["results"] if x["reached_target"]
    ]):])
    # 稳定排序：到达目标 → lead 数 → call 数 → 拼接数 → change 数
    keys = [
        (not x["reached_target"], x["num_leads"], x["num_calls"],
         x["num_splices"], x["num_changes"])
        for x in d["results"]
    ]
    assert keys == sorted(keys)
    assert all(x["forced_remainder"] is None for x in d["results"])


def test_continuation_mid_lead_forced_remainder(client, pb_touch):
    # 显式提交 lead 中途前缀（10/12 change）
    all_rows = _touch_rows(client, pb_touch)
    p = _explicit_prefix(client, all_rows[:10], 1, id="midx")
    assert p.status_code == 201
    d = client.post(
        "/prefixes/midx/versions/1/continuation", json={"max_leads": 4}
    ).json()
    # num_leads=0 的强制段候选：只含剩余 2 个 change，未到目标
    forced = [x for x in d["results"] if x["num_leads"] == 0]
    assert len(forced) == 1
    cand = forced[0]
    assert cand["reached_target"] is False
    assert cand["rows"] == all_rows[10:12]
    assert cand["forced_remainder"] == {
        "lead": 1,
        "method": "pb-minor",
        "method_version": 1,
        "call": "plain",
        "remaining_changes": 2,
        "end_row": all_rows[11],
    }
    assert [ev["forced_remainder"] for ev in cand["events"]] == [True, True]
    assert cand["events"][0]["change_in_lead"] == 11
    # 到达目标的方案：强制段 + 4 个完整 lead（剩余 plain course）
    targets = [x for x in d["results"] if x["reached_target"]]
    assert targets
    best = targets[0]
    assert best["num_leads"] == 4
    assert best["rows"][-1] == "123456"
    assert best["num_changes"] == 50  # 2 强制 + 48 完整 lead
    # 完整 lead 的事件不带 forced 标记，全局 lead 号从 2 起
    tail_events = [ev for ev in best["events"] if not ev["forced_remainder"]]
    assert tail_events[0]["lead"] == 2
    assert all(ev["forced_remainder"] is False for ev in tail_events)
    # 所有候选都带 reached_target 布尔字段
    assert all("reached_target" in x for x in d["results"])


def test_continuation_deterministic_and_cached(client, pb_touch):
    pid = _prefix_three_leads(client, pb_touch)
    a = client.post(f"/prefixes/{pid}/versions/1/continuation", json={"max_leads": 3})
    b = client.post(f"/prefixes/{pid}/versions/1/continuation", json={"max_leads": 3})
    assert a.headers["X-Continuation-Cache"] == "miss"
    assert b.headers["X-Continuation-Cache"] == "hit"
    assert a.json() == b.json()
    # 请求键参与哈希：不同约束结果可能不同，但各自稳定
    c1 = client.post(
        f"/prefixes/{pid}/versions/1/continuation", json={"max_leads": 3, "max_calls": 1}
    ).json()
    c2 = client.post(
        f"/prefixes/{pid}/versions/1/continuation", json={"max_leads": 3, "max_calls": 1}
    ).json()
    assert c1 == c2
    assert all(x["num_calls"] <= 1 for x in c1["results"])


def test_continuation_no_solution_reports_exhaustion(client, methods):
    # 完整 plain course 前缀（已见全部 row），目标改成不可达排列：
    # 任何尾段首 lead 都与前缀重复 → 一个合法候选都没有
    r = client.post(
        "/touches",
        json={
            "method_id": "pb-minor",
            "calls": CALLS,
            "sequence": [{"leads": [{"call": None}], "repeat": 5}],
        },
    )
    rows = _touch_rows(client, r.json()["id"])
    p = _explicit_prefix(client, rows, 5).json()
    pid, pv = p["id"], p["version"]
    d = client.post(
        f"/prefixes/{pid}/versions/{pv}/continuation",
        json={"max_leads": 1, "target_row": "654321"},
    ).json()
    assert d["solutions_found"] == 0 and d["truncated"] is False
    assert "穷尽" in d["truncation_reason"]
    assert d["checked_states"] >= 1
    assert d["pruned_by_repeat"] > 0


def test_non_target_candidates_visible_with_unreachable_min(client, methods):
    # 部分 lead 前缀（10/12）；min 配额不可达时强制段候选仍在结果中
    r = client.post(
        "/touches",
        json={
            "method_id": "pb-minor",
            "calls": CALLS,
            "sequence": [{"leads": [{"call": None}]}],
        },
    )
    rows = _touch_rows(client, r.json()["id"])
    p = _explicit_prefix(client, rows[:10], 1).json()
    pid, pv = p["id"], p["version"]
    d = client.post(
        f"/prefixes/{pid}/versions/{pv}/continuation",
        json={"max_leads": 2, "method_quotas": {"pb-minor": {"min": 99}}},
    ).json()
    assert d["solutions_found"] >= 1
    assert all(x["reached_target"] is False for x in d["results"])
    assert d["pruned_by_quota"] >= 1
    # max 配额仍然约束未到目标候选：max=0 时不含完整 lead 的候选
    d2 = client.post(
        f"/prefixes/{pid}/versions/{pv}/continuation",
        json={"max_leads": 2, "method_quotas": {"pb-minor": {"max": 0}}},
    ).json()
    assert all(x["num_leads"] == 0 for x in d2["results"])


def test_continuation_truncation(client, pb_touch):
    pid = _prefix_three_leads(client, pb_touch)
    d = client.post(
        f"/prefixes/{pid}/versions/1/continuation",
        json={"max_leads": 6, "max_search": 1},
    ).json()
    assert d["truncated"] is True and d["checked_states"] == 1
    assert "max_search" in d["truncation_reason"]
    assert "choice_path" in d["deepest_progress"]


def test_continuation_quota_pruning_and_remaining(client, methods):
    r = client.post(
        "/touches",
        json={
            "method_id": "pb-minor",
            "calls": CALLS,
            "sequence": [{"leads": [{"call": None}]}],
        },
    )
    rows = _touch_rows(client, r.json()["id"])
    p = client.post(
        "/prefixes",
        json={
            "stage": 6,
            "methods": [{"id": "pb-minor"}],
            "calls": CALLS,
            "leads": [{"call": None}],
            "rows": rows,
        },
    ).json()
    pid, pv = p["id"], p["version"]
    # 不可达的 min 配额 → 剪枝计数增长、无解
    d = client.post(
        f"/prefixes/{pid}/versions/{pv}/continuation",
        json={"max_leads": 4, "method_quotas": {"pb-minor": {"min": 99}}},
    ).json()
    assert d["solutions_found"] == 0 and d["pruned_by_quota"] > 0
    # 正常配额：每个方案给出余量
    d2 = client.post(
        f"/prefixes/{pid}/versions/{pv}/continuation",
        json={"max_leads": 5, "method_quotas": {"pb-minor": {"min": 1, "max": 5}}},
    ).json()
    best = d2["results"][0]
    q = best["quota_remaining"]["pb-minor"]
    assert q["used"] == best["num_leads"]
    assert q["remaining_min"] == 0 and q["remaining_max"] >= 0


def test_continuation_boundary_transition_pruning(client, methods):
    r = client.post(
        "/touches",
        json={
            "method_id": "pb-minor",
            "calls": CALLS,
            "sequence": [{"leads": [{"call": None}]}],
        },
    )
    rows = _touch_rows(client, r.json()["id"])
    p = client.post(
        "/prefixes",
        json={
            "stage": 6,
            "methods": [{"id": "pb-minor"}, {"id": "alt-minor"}],
            "calls": CALLS,
            "leads": [{"method": "pb-minor"}],
            "rows": rows,
        },
    ).json()
    pid, pv = p["id"], p["version"]
    # 白名单仅允许 pb->alt：根边界上 alt 可选（剪枝计数为 0），同方法 pb 也始终允许
    d = client.post(
        f"/prefixes/{pid}/versions/{pv}/continuation",
        json={
            "max_leads": 1,
            "allowed_transitions": [["pb-minor", "alt-minor"]],
        },
    ).json()
    assert d["pruned_by_transition"] == 0
    assert {
        x["leads"][0]["method"] for x in d["results"] if x["num_leads"] == 1
    } == {"pb-minor", "alt-minor"}

    # 白名单只有 alt->pb（缺 pb->alt）：前缀末方法为 pb，alt 不能作为
    # 第一个尾段 lead，只有同方法 pb 延续
    d_alt = client.post(
        f"/prefixes/{pid}/versions/{pv}/continuation",
        json={
            "max_leads": 2,
            "allowed_transitions": [["alt-minor", "pb-minor"]],
        },
    ).json()
    assert d_alt["pruned_by_transition"] >= 1
    for x in d_alt["results"]:
        if x["num_leads"] >= 1:
            assert x["leads"][0]["method"] == "pb-minor"

    # 白名单不含 pb->alt：alt 在根边界被剪枝，pb 同方法延续始终允许
    d2 = client.post(
        f"/prefixes/{pid}/versions/{pv}/continuation",
        json={
            "max_leads": 2,
            "allowed_transitions": [["pb-minor", "pb-minor"]],
        },
    ).json()
    assert d2["pruned_by_transition"] >= 1
    for x in d2["results"]:
        if x["num_leads"] >= 1:
            assert x["leads"][0]["method"] == "pb-minor"
    # 同方法连续 lead 不被白名单剪掉：只列跨方法转换时仍能找到同方法续接
    d3 = client.post(
        f"/prefixes/{pid}/versions/{pv}/continuation",
        json={
            "max_leads": 2,
            "allowed_transitions": [["pb-minor", "alt-minor"], ["alt-minor", "pb-minor"]],
        },
    ).json()
    same_method = [
        x for x in d3["results"]
        if x["num_leads"] >= 1 and all(l["method"] == "pb-minor" for l in x["leads"])
    ]
    assert same_method


def test_boundary_splice_count_matches_row_flags(client, methods):
    """前缀末 lead 方法参与首尾段 lead 的转换：num_splices 与逐 row splice 一致。"""
    r = client.post(
        "/touches",
        json={
            "method_id": "pb-minor",
            "calls": CALLS,
            "sequence": [{"leads": [{"call": None}]}],
        },
    )
    rows = _touch_rows(client, r.json()["id"])
    p = client.post(
        "/prefixes",
        json={
            "stage": 6,
            "methods": [{"id": "pb-minor"}, {"id": "alt-minor"}],
            "calls": CALLS,
            "leads": [{"method": "pb-minor"}],
            "rows": rows,
        },
    ).json()
    pid, pv = p["id"], p["version"]
    d = client.post(
        f"/prefixes/{pid}/versions/{pv}/continuation",
        json={
            "max_leads": 1,
            "allowed_transitions": [["pb-minor", "alt-minor"]],
        },
    ).json()
    alt_lead = [
        x for x in d["results"]
        if x["num_leads"] == 1 and x["leads"][0]["method"] == "alt-minor"
    ]
    assert alt_lead
    cand = alt_lead[0]
    # 跨方法边界：num_splices=1，且该尾段每个 row 事件 splice=true
    assert cand["num_splices"] == 1
    assert all(ev["splice"] is True for ev in cand["events"])
    # 同方法延续：num_splices=0 且逐 row splice=false
    pb_lead = [
        x for x in d["results"]
        if x["num_leads"] == 1 and x["leads"][0]["method"] == "pb-minor"
    ]
    assert pb_lead
    assert pb_lead[0]["num_splices"] == 0
    assert all(ev["splice"] is False for ev in pb_lead[0]["events"])


def test_partial_frozen_call_not_affected_by_override(client, methods):
    """部分 bob lead 的强制余段沿用冻结的 call 定义；请求中同名覆盖只用于新 lead。"""
    r = client.post(
        "/touches",
        json={
            "method_id": "pb-minor",
            "calls": CALLS,
            "sequence": [{"leads": [{"call": "bob"}]}],
        },
    )
    rows = _touch_rows(client, r.json()["id"])
    assert rows[-1] == "123564"  # bob=14 的 lead end
    # 前缀停在 bob lead 的第 11 个 change
    p = client.post(
        "/prefixes",
        json={
            "stage": 6,
            "methods": [{"id": "pb-minor"}],
            "calls": CALLS,
            "leads": [{"call": "bob"}],
            "rows": rows[:11],
        },
    )
    assert p.status_code == 201, p.text
    pj = p.json()
    pid, pv = pj["id"], pj["version"]
    assert pj["partial_lead"]["call"] == "bob"
    assert pj["partial_lead"]["remaining"] == 1

    d = client.post(
        f"/prefixes/{pid}/versions/{pv}/continuation",
        json={
            "max_leads": 1,
            "calls": {"bob": {"notation": "16", "replace": 1}},  # 同名覆盖
        },
    ).json()
    forced_only = [x for x in d["results"] if x["num_leads"] == 0]
    assert len(forced_only) == 1
    cand = forced_only[0]
    # 强制余段仍是冻结 touch 的 bob=14 结果，而非覆盖后的 16
    assert cand["rows"] == ["123564"]
    assert cand["events"][0]["notation"] == "14"
    assert cand["events"][0]["source"] == "call:bob"
    assert cand["reached_target"] is False  # 123564 不是 rounds
    assert cand["events"][0]["forced_remainder"] is True
    # 覆盖后的 bob=16 从该强制段末 row 开新 lead 会立即撞回 123564（重复），
    # 此场景下新 bob lead 被重复剪枝
    assert not [
        x for x in d["results"]
        if x["num_leads"] == 1 and x["leads"][0]["call"] == "bob"
    ]
    assert d["pruned_by_repeat"] >= 1

    # 用 1234 覆盖同名 bob：强制余段仍是冻结的 14，而新增 lead 的
    # lead end 记号使用覆盖后的 1234
    d2 = client.post(
        f"/prefixes/{pid}/versions/{pv}/continuation",
        json={
            "max_leads": 1,
            "calls": {"bob": {"notation": "1234", "replace": 1}},
        },
    ).json()
    forced2 = [x for x in d2["results"] if x["num_leads"] == 0]
    assert forced2 and forced2[0]["events"][0]["notation"] == "14"
    assert forced2[0]["rows"] == ["123564"]
    new_bob = [
        x for x in d2["results"]
        if x["num_leads"] == 1 and x["leads"][0]["call"] == "bob"
    ]
    assert new_bob
    tail_last = [
        ev for ev in new_bob[0]["events"]
        if not ev["forced_remainder"] and ev["change_in_lead"] == 12
    ]
    assert tail_last and tail_last[0]["notation"] == "1234"
    # 强制段记号不受影响
    assert new_bob[0]["forced_remainder"]["call"] == "bob"
    forced_events = [e for e in new_bob[0]["events"] if e["forced_remainder"]]
    assert forced_events and forced_events[0]["notation"] == "14"


def test_continuation_music_sorting_and_threshold(client, methods):
    r = client.post(
        "/touches",
        json={
            "method_id": "pb-minor",
            "calls": CALLS,
            "sequence": [{"leads": [{"call": None}], "repeat": 2}],
        },
    )
    rows = _touch_rows(client, r.json()["id"])[:12]
    p = client.post(
        "/prefixes",
        json={
            "stage": 6,
            "methods": [{"id": "pb-minor"}],
            "calls": CALLS,
            "leads": [{"call": None}],
            "rows": rows,
        },
    ).json()
    pid, pv = p["id"], p["version"]
    client.post(
        "/music-schemes",
        json={
            "id": "m6",
            "name": "六钟音乐",
            "stage": 6,
            "rules": [
                {"type": "run", "name": "back56", "points": 5,
                 "bells": [5, 6], "position": "back"}
            ],
        },
    )
    d = client.post(
        f"/prefixes/{pid}/versions/{pv}/continuation",
        json={"max_leads": 5, "music": {"id": "m6"}, "max_results": 20},
    ).json()
    assert "music_score" in d["sorted_by"]
    assert all("music_score" in x for x in d["results"])
    # 稳定排序键：到达目标 → lead 数 → call 数 → 拼接数 → 音乐分（高者优先）
    keys = [
        (
            not x["reached_target"],
            x["num_leads"],
            x["num_calls"],
            x["num_splices"],
            -x["music_score"],
        )
        for x in d["results"]
    ]
    assert keys == sorted(keys)
    # 门槛过滤
    d2 = client.post(
        f"/prefixes/{pid}/versions/{pv}/continuation",
        json={
            "max_leads": 5,
            "music": {"id": "m6"},
            "min_music_score": 10 ** 6,
            "max_results": 20,
        },
    ).json()
    assert d2["solutions_found"] == 0 and d2["filtered_by_music"] > 0


def test_continuation_music_threshold_requires_scheme(client, pb_touch):
    pid = _prefix_three_leads(client, pb_touch)
    r = client.post(
        f"/prefixes/{pid}/versions/1/continuation",
        json={"max_leads": 3, "min_music_score": 1},
    )
    assert r.status_code == 422


def test_continuation_unknown_methods_and_quotas(client, pb_touch):
    pid = _prefix_three_leads(client, pb_touch)
    r = client.post(
        f"/prefixes/{pid}/versions/1/continuation",
        json={"max_leads": 2, "methods": [{"id": "ghost"}]},
    )
    assert r.status_code == 404
    r2 = client.post(
        f"/prefixes/{pid}/versions/1/continuation",
        json={"max_leads": 2, "method_quotas": {"ghost": {"max": 1}}},
    )
    assert r2.status_code == 422 and r2.json()["detail"]["code"] == "UNKNOWN_METHOD"
    assert client.post(f"/prefixes/ghost/versions/1/continuation", json={}).status_code == 404


def test_continuation_freezes_method_and_music_versions(client, pb_touch):
    rows = _touch_rows(client, pb_touch)[:24]
    p = _explicit_prefix(client, rows, 2).json()
    pid, pv = p["id"], p["version"]
    # 前缀创建后新增方法新版本；续接未标版本时仍冻结为 v1
    client.post(
        "/methods",
        json={"id": "pb-minor", "name": "PB newer", "stage": 6, "notation": PB_MINOR},
    )
    d = client.post(
        f"/prefixes/{pid}/versions/{pv}/continuation", json={"max_leads": 4}
    ).json()
    assert [m["version"] for m in d["methods"]] == [1]

    # 评分方案同样随前缀版本冻结
    client.post(
        "/music-schemes",
        json={
            "id": "m",
            "name": "m",
            "stage": 6,
            "rules": [
                {"type": "run", "name": "b", "points": 1,
                 "bells": [5, 6], "position": "back"}
            ],
        },
    )
    a = client.post(
        f"/prefixes/{pid}/versions/{pv}/continuation",
        json={"max_leads": 4, "music": {"id": "m"}},
    ).json()
    client.post(
        "/music-schemes",
        json={
            "id": "m",
            "name": "m2",
            "stage": 6,
            "rules": [
                {"type": "run", "name": "b", "points": 99,
                 "bells": [5, 6], "position": "back"}
            ],
        },
    )
    b = client.post(
        f"/prefixes/{pid}/versions/{pv}/continuation",
        json={"max_leads": 4, "music": {"id": "m"}},
    ).json()
    assert a["music"]["version"] == b["music"]["version"] == 1
    assert a["results"] == b["results"]


def test_prefix_custom_start_row(client, pb_touch):
    rows = _touch_rows(client, pb_touch)
    # 以第 1 个 lead head 为起始，敲第 2 个 lead
    p = client.post(
        "/prefixes",
        json={
            "stage": 6,
            "methods": [{"id": "pb-minor"}],
            "calls": CALLS,
            "start_row": rows[11],
            "leads": [{"call": None}],
            "rows": rows[12:24],
        },
    )
    assert p.status_code == 201
    assert p.json()["start_row"] == rows[11]
    assert p.json()["current_row"] == rows[23]


def test_continuation_repeats_pruned_when_target_unreachable(client, pb_touch):
    # 完整 plain course 前缀（已见全部 60 row），目标改成非 rounds：
    # 任何尾段 lead 都撞前缀 row，全部按重复剪枝
    rows = _touch_rows(client, pb_touch)
    p = _explicit_prefix(client, rows, 5, id="done").json()
    d = client.post(
        f"/prefixes/done/versions/{p['version']}/continuation",
        json={"max_leads": 2, "target_row": "654321"},
    ).json()
    assert d["solutions_found"] == 0
    assert d["pruned_by_repeat"] > 0
    assert not d["truncated"] and "穷尽" in d["truncation_reason"]


def test_result_events_carry_full_provenance(client, pb_touch):
    pid = _prefix_three_leads(client, pb_touch)
    d = client.post(
        f"/prefixes/{pid}/versions/1/continuation", json={"max_leads": 3}
    ).json()
    ev = d["results"][0]["events"][0]
    assert ev["lead"] == 4
    assert ev["method_id"] == "pb-minor" and ev["method_version"] == 1
    assert ev["source"] == "method" and ev["call"] is None
    assert ev["splice"] is False
    assert {"notation", "places", "change_in_lead", "row"} <= set(ev)


def test_continuation_tail_calls_override(client, methods):
    # 前缀无 call 定义，续接请求自带 bob
    r = client.post(
        "/touches",
        json={
            "method_id": "pb-minor",
            "calls": CALLS,
            "sequence": [{"leads": [{"call": None}], "repeat": 2}],
        },
    )
    rows = _touch_rows(client, r.json()["id"])[:12]
    p = client.post(
        "/prefixes",
        json={
            "stage": 6,
            "methods": [{"id": "pb-minor"}],
            "leads": [{"call": None}],
            "rows": rows,
        },
    ).json()
    pid, pv = p["id"], p["version"]
    d = client.post(
        f"/prefixes/{pid}/versions/{pv}/continuation",
        json={
            "max_leads": 6,
            "calls": {"bob": {"notation": "14", "replace": 1}},
        },
    ).json()
    assert "bob" in d["calls"]
    assert any(
        l["call"] == "bob"
        for x in d["results"]
        for l in x["leads"]
    )
