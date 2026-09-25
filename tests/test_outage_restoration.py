"""POST /outage-restoration 的端到端测试。

测试用树 A（与 test_evaluate 相同）：

source --s1(0.1Ω, 20A)--> a --s2(0.2Ω, 10A)--> b
                          |
                          +--s3(0.05Ω, 15A)--> c

负载：a=2A, b=5A, c=8A；基线：段电流 s1=15, s2=5, s3=8；节点电压 a=46.5, b=45.5, c=46.1

断开 s2 后失电分量为 {b}：
- t1: a->b (0.1Ω, 10A)：t1 承载 5A，b=46.0V，可恢复，最低电压余量 6.0V（节点 b）
- t2: c->b (0.5Ω, 10A)：s3 承载 13A，c=45.85V，b=43.35V，可恢复，余量 3.35V（节点 b）
- t3: a->b (0.1Ω, 4A)：t3 承载 5A 超额定，不可恢复

测试用树 B（链式，验证失电分量重新定向）：

source --s1(0.1Ω, 30A)--> a --s2(0.1Ω, 30A)--> b --s3(0.1Ω, 30A)--> c --s4(0.1Ω, 30A)--> d

负载：b=1A, c=1A, d=1A。断开 s2 后失电分量为 {b, c, d}；
跨接线 t1: a->d 接入后 s4 反转为 d->c、s3 反转为 c->b，
段电流 s1=3, t1=3, s4=2, s3=1；节点电压 a=47.7, d=47.4, c=47.2, b=47.1
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def base_payload(**overrides):
    payload = {
        "source_voltage": 48.0,
        "min_voltage": 40.0,
        "segments": [
            {"id": "s1", "parent": "source", "child": "a", "resistance": 0.1, "rated_current": 20.0},
            {"id": "s2", "parent": "a", "child": "b", "resistance": 0.2, "rated_current": 10.0},
            {"id": "s3", "parent": "a", "child": "c", "resistance": 0.05, "rated_current": 15.0},
        ],
        "loads": [
            {"node": "a", "current": 2.0},
            {"node": "b", "current": 5.0},
            {"node": "c", "current": 8.0},
        ],
        "fault_segment_id": "s2",
        "tie_lines": [
            {"id": "t1", "from_node": "a", "to_node": "b", "resistance": 0.1, "rated_current": 10.0},
            {"id": "t2", "from_node": "c", "to_node": "b", "resistance": 0.5, "rated_current": 10.0},
            {"id": "t3", "from_node": "a", "to_node": "b", "resistance": 0.1, "rated_current": 4.0},
        ],
    }
    payload.update(overrides)
    return payload


def chain_payload(**overrides):
    payload = {
        "source_voltage": 48.0,
        "min_voltage": 40.0,
        "segments": [
            {"id": "s1", "parent": "source", "child": "a", "resistance": 0.1, "rated_current": 30.0},
            {"id": "s2", "parent": "a", "child": "b", "resistance": 0.1, "rated_current": 30.0},
            {"id": "s3", "parent": "b", "child": "c", "resistance": 0.1, "rated_current": 30.0},
            {"id": "s4", "parent": "c", "child": "d", "resistance": 0.1, "rated_current": 30.0},
        ],
        "loads": [
            {"node": "b", "current": 1.0},
            {"node": "c", "current": 1.0},
            {"node": "d", "current": 1.0},
        ],
        "fault_segment_id": "s2",
        "tie_lines": [
            {"id": "t1", "from_node": "a", "to_node": "d", "resistance": 0.1, "rated_current": 30.0},
        ],
    }
    payload.update(overrides)
    return payload


def by_id(data, key, items):
    return {item[key]: item for item in data[items]}


def test_per_candidate_results_and_selection():
    """逐候选返回越限与可恢复标记；从可恢复候选中选最低电压余量最大的 t1。"""
    resp = client.post("/outage-restoration", json=base_payload())
    assert resp.status_code == 200
    data = resp.json()

    assert data["fault_segment_id"] == "s2"
    assert [c["tie_line_id"] for c in data["candidates"]] == ["t1", "t2", "t3"]
    assert data["selected_tie_line_id"] == "t1"

    candidates = by_id(data, "tie_line_id", "candidates")

    t1 = candidates["t1"]
    assert t1["restorable"] is True
    assert t1["min_voltage_margin"] == pytest.approx(6.0)
    assert t1["over_current_segments"] == []
    assert t1["under_voltage_nodes"] == []
    currents = {s["segment_id"]: s["current"] for s in t1["segments"]}
    assert currents == {"s1": pytest.approx(15.0), "s3": pytest.approx(8.0), "t1": pytest.approx(5.0)}
    voltages = {n["node"]: n["voltage"] for n in t1["nodes"]}
    assert voltages["b"] == pytest.approx(46.0)
    assert voltages["c"] == pytest.approx(46.1)

    t2 = candidates["t2"]
    assert t2["restorable"] is True
    assert t2["min_voltage_margin"] == pytest.approx(3.35)
    currents = {s["segment_id"]: s["current"] for s in t2["segments"]}
    assert currents["s3"] == pytest.approx(13.0)  # c 支路转供 b
    voltages = {n["node"]: n["voltage"] for n in t2["nodes"]}
    assert voltages["c"] == pytest.approx(45.85)
    assert voltages["b"] == pytest.approx(43.35)

    t3 = candidates["t3"]
    assert t3["restorable"] is False
    assert t3["over_current_segments"] == [
        {
            "segment_id": "t3",
            "parent": "a",
            "child": "b",
            "current": pytest.approx(5.0),
            "rated_current": pytest.approx(4.0),
            "excess": pytest.approx(1.0),
        }
    ]


def test_outage_component_reoriented_from_connection_point():
    """失电分量从接入点重新定向：s4 反转为 d->c、s3 反转为 c->b，全树重算。"""
    resp = client.post("/outage-restoration", json=chain_payload())
    assert resp.status_code == 200
    data = resp.json()

    assert data["selected_tie_line_id"] == "t1"
    candidate = data["candidates"][0]
    assert candidate["restorable"] is True

    segments = {s["segment_id"]: s for s in candidate["segments"]}
    # 故障线段 s2 已断开，不出现在结果中
    assert set(segments) == {"s1", "s3", "s4", "t1"}
    # 原根 b 到接入点 d 路径上的线段方向反转
    assert (segments["s4"]["parent"], segments["s4"]["child"]) == ("d", "c")
    assert (segments["s3"]["parent"], segments["s3"]["child"]) == ("c", "b")
    assert (segments["t1"]["parent"], segments["t1"]["child"]) == ("a", "d")
    assert segments["s1"]["current"] == pytest.approx(3.0)
    assert segments["t1"]["current"] == pytest.approx(3.0)
    assert segments["s4"]["current"] == pytest.approx(2.0)
    assert segments["s3"]["current"] == pytest.approx(1.0)

    voltages = {n["node"]: n["voltage"] for n in candidate["nodes"]}
    assert voltages["a"] == pytest.approx(47.7)
    assert voltages["d"] == pytest.approx(47.4)
    assert voltages["c"] == pytest.approx(47.2)
    assert voltages["b"] == pytest.approx(47.1)


def test_tie_line_may_connect_to_outage_root_and_source():
    """接入点可为失电分量任意节点（含原根 b）；供电侧可为 source 本身。"""
    payload = base_payload(
        tie_lines=[
            {"id": "t1", "from_node": "source", "to_node": "b", "resistance": 0.1, "rated_current": 10.0},
        ]
    )
    data = client.post("/outage-restoration", json=payload).json()
    candidate = data["candidates"][0]
    assert candidate["restorable"] is True
    voltages = {n["node"]: n["voltage"] for n in candidate["nodes"]}
    assert voltages["b"] == pytest.approx(47.5)  # 48 - 5×0.1，不再经过 s1
    assert data["selected_tie_line_id"] == "t1"


def test_selection_tie_broken_by_input_order():
    """两条参数相同的可恢复跨接线余量相同，按输入顺序选先出现者。"""
    payload = base_payload(
        tie_lines=[
            {"id": "t1", "from_node": "a", "to_node": "b", "resistance": 0.1, "rated_current": 10.0},
            {"id": "t2", "from_node": "a", "to_node": "b", "resistance": 0.1, "rated_current": 10.0},
        ]
    )
    data = client.post("/outage-restoration", json=payload).json()
    assert all(c["restorable"] for c in data["candidates"])
    assert data["selected_tie_line_id"] == "t1"


def test_no_restorable_candidate_selects_null():
    """全部候选越限时选中项为 null。"""
    payload = base_payload(
        tie_lines=[
            {"id": "t1", "from_node": "a", "to_node": "b", "resistance": 0.1, "rated_current": 4.0},
        ]
    )
    data = client.post("/outage-restoration", json=payload).json()
    assert data["candidates"][0]["restorable"] is False
    assert data["selected_tie_line_id"] is None


def test_empty_tie_lines_returns_null_selection():
    payload = base_payload(tie_lines=[])
    data = client.post("/outage-restoration", json=payload).json()
    assert data["candidates"] == []
    assert data["selected_tie_line_id"] is None


def test_under_voltage_candidate_not_restorable_with_per_node_override():
    """节点级 min_voltage 覆盖生效：b 阈值提高到 46.5V，恢复后 46.0V 欠压。"""
    payload = base_payload(
        tie_lines=[
            {"id": "t1", "from_node": "a", "to_node": "b", "resistance": 0.1, "rated_current": 10.0},
        ]
    )
    payload["loads"][1]["min_voltage"] = 46.5
    data = client.post("/outage-restoration", json=payload).json()

    candidate = data["candidates"][0]
    assert candidate["restorable"] is False
    assert candidate["over_current_segments"] == []
    assert candidate["under_voltage_nodes"] == [
        {
            "node": "b",
            "voltage": pytest.approx(46.0),
            "min_voltage": pytest.approx(46.5),
            "deficit": pytest.approx(0.5),
        }
    ]
    assert data["selected_tie_line_id"] is None


def test_exact_limit_is_restorable():
    """恰好触限不算越限：跨接线电流等于额定电流时仍可恢复。"""
    payload = base_payload(
        tie_lines=[
            {"id": "t1", "from_node": "a", "to_node": "b", "resistance": 0.1, "rated_current": 5.0},
        ]
    )
    data = client.post("/outage-restoration", json=payload).json()
    candidate = data["candidates"][0]
    assert candidate["restorable"] is True
    segments = {s["segment_id"]: s for s in candidate["segments"]}
    assert segments["t1"]["current"] == pytest.approx(5.0)
    assert segments["t1"]["over_rated"] is False
    assert data["selected_tie_line_id"] == "t1"


# ---------- 数值校验（422） ----------


def test_negative_tie_line_resistance_rejected():
    payload = base_payload()
    payload["tie_lines"][0]["resistance"] = -0.1
    resp = client.post("/outage-restoration", json=payload)
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert any("跨接线电阻" in d["message"] for d in error["details"])


def test_non_positive_tie_line_rated_current_rejected():
    payload = base_payload()
    payload["tie_lines"][0]["rated_current"] = 0.0
    resp = client.post("/outage-restoration", json=payload)
    assert resp.status_code == 422
    assert any("跨接线额定电流" in d["message"] for d in resp.json()["error"]["details"])


def test_missing_fault_segment_id_rejected():
    payload = base_payload()
    del payload["fault_segment_id"]
    resp = client.post("/outage-restoration", json=payload)
    assert resp.status_code == 422
    assert any("故障线段" in d["message"] for d in resp.json()["error"]["details"])


# ---------- 故障线段与跨接线校验（400） ----------


def test_unknown_fault_segment_rejected():
    resp = client.post("/outage-restoration", json=base_payload(fault_segment_id="ghost"))
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "invalid_input"
    assert any("故障线段" in d["message"] and "不存在" in d["message"] for d in error["details"])


def test_from_node_in_outage_component_rejected():
    payload = base_payload()
    payload["tie_lines"][0]["from_node"] = "b"  # b 位于失电分量
    resp = client.post("/outage-restoration", json=payload)
    assert resp.status_code == 400
    assert any("供电侧节点" in d["message"] and "失电分量" in d["message"] for d in resp.json()["error"]["details"])


def test_to_node_in_source_component_rejected():
    payload = base_payload()
    payload["tie_lines"][0]["to_node"] = "c"  # c 位于 source 所在分量
    resp = client.post("/outage-restoration", json=payload)
    assert resp.status_code == 400
    assert any("失电侧节点" in d["message"] and "不在失电分量" in d["message"] for d in resp.json()["error"]["details"])


def test_unknown_tie_line_node_rejected():
    payload = base_payload()
    payload["tie_lines"][0]["from_node"] = "ghost"
    resp = client.post("/outage-restoration", json=payload)
    assert resp.status_code == 400
    assert any("不存在于树中" in d["message"] for d in resp.json()["error"]["details"])


def test_duplicate_tie_line_id_rejected():
    payload = base_payload()
    payload["tie_lines"][1]["id"] = "t1"  # 与其他候选重复
    resp = client.post("/outage-restoration", json=payload)
    assert resp.status_code == 400
    assert any("重复" in d["message"] for d in resp.json()["error"]["details"])

    payload = base_payload()
    payload["tie_lines"][0]["id"] = "s1"  # 与已有线段重复
    resp = client.post("/outage-restoration", json=payload)
    assert resp.status_code == 400
    assert any("重复" in d["message"] for d in resp.json()["error"]["details"])


def test_invalid_tree_still_rejected():
    payload = base_payload()
    payload["segments"].append(
        {"parent": "x", "child": "y", "resistance": 0.1, "rated_current": 10.0}
    )
    resp = client.post("/outage-restoration", json=payload)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_input"
