"""POST /outage-restoration 的端到端测试。

测试用树：

source --s1(0.1Ω, 30A)--> a --s2(0.2Ω, 10A)--> b
                          |
                          +--s3(0.05Ω, 15A)--> c --s4(0.1Ω, 10A)--> d

负载：a=2, b=3, c=3, d=4
基线：段电流 s1=12, s2=3, s3=7, s4=4；节点电压 a=46.8, b=46.2, c=46.45, d=46.05
故障线段 s3 断开 -> 失电分量 {c, d}，source 所在分量 {source, a, b}
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
            {"id": "s1", "parent": "source", "child": "a", "resistance": 0.1, "rated_current": 30.0},
            {"id": "s2", "parent": "a", "child": "b", "resistance": 0.2, "rated_current": 10.0},
            {"id": "s3", "parent": "a", "child": "c", "resistance": 0.05, "rated_current": 15.0},
            {"id": "s4", "parent": "c", "child": "d", "resistance": 0.1, "rated_current": 10.0},
        ],
        "loads": [
            {"node": "a", "current": 2.0},
            {"node": "b", "current": 3.0},
            {"node": "c", "current": 3.0},
            {"node": "d", "current": 4.0},
        ],
        "faulty_segment_id": "s3",
        "jumpers": [
            {"id": "j1", "supply_node": "a", "outage_node": "c", "resistance": 0.05, "rated_current": 15.0},
            {"id": "j2", "supply_node": "a", "outage_node": "d", "resistance": 0.05, "rated_current": 15.0},
        ],
    }
    payload.update(overrides)
    return payload


def by_id(data, key, items):
    return {item[key]: item for item in data[items]}


def test_reorientation_and_best_margin_selection():
    """j1 恢复原拓扑（余量 6.05）；j2 接入 d 点、失电分量重新定向（余量 6.15），选中 j2。"""
    resp = client.post("/outage-restoration", json=base_payload())
    assert resp.status_code == 200
    data = resp.json()

    assert data["faulty_segment_id"] == "s3"
    assert [c["jumper_id"] for c in data["candidates"]] == ["j1", "j2"]

    j1, j2 = data["candidates"]
    # j1：a->c，拓扑与基线一致
    assert j1["restorable"] is True
    assert j1["min_voltage_margin"] == pytest.approx(6.05)
    assert j1["over_current_segments"] == []
    assert j1["under_voltage_nodes"] == []
    segments = by_id(j1, "segment_id", "segments")
    assert [s["segment_id"] for s in j1["segments"]] == ["s1", "s2", "s4", "j1"]  # 故障段退出，跨接线列末
    assert segments["j1"]["parent"] == "a" and segments["j1"]["child"] == "c"
    assert segments["j1"]["current"] == pytest.approx(7.0)
    assert segments["s4"]["current"] == pytest.approx(4.0)
    nodes = by_id(j1, "node", "nodes")
    assert nodes["c"]["voltage"] == pytest.approx(46.45)
    assert nodes["d"]["voltage"] == pytest.approx(46.05)

    # j2：a->d，s4 方向翻转为 d->c（仅承载 c 的 3A），c 电压 46.45-0.3=46.15
    assert j2["restorable"] is True
    assert j2["min_voltage_margin"] == pytest.approx(6.15)
    segments = by_id(j2, "segment_id", "segments")
    assert segments["s4"]["parent"] == "d" and segments["s4"]["child"] == "c"
    assert segments["s4"]["current"] == pytest.approx(3.0)
    assert segments["j2"]["current"] == pytest.approx(7.0)
    nodes = by_id(j2, "node", "nodes")
    assert nodes["d"]["voltage"] == pytest.approx(46.45)
    assert nodes["c"]["voltage"] == pytest.approx(46.15)

    # 均可恢复，选中最低节点电压余量最大者（6.15 > 6.05）
    assert data["selected_jumper_id"] == "j2"


def test_tie_broken_by_input_order():
    """两条参数相同的跨接线余量相同，平局按输入顺序选中先出现者。"""
    jumpers = [
        {"id": "j1", "supply_node": "a", "outage_node": "c", "resistance": 0.05, "rated_current": 15.0},
        {"id": "j1x", "supply_node": "a", "outage_node": "c", "resistance": 0.05, "rated_current": 15.0},
    ]
    data = client.post("/outage-restoration", json=base_payload(jumpers=jumpers)).json()
    assert data["candidates"][0]["min_voltage_margin"] == pytest.approx(
        data["candidates"][1]["min_voltage_margin"]
    )
    assert data["selected_jumper_id"] == "j1"

    data = client.post("/outage-restoration", json=base_payload(jumpers=list(reversed(jumpers)))).json()
    assert data["selected_jumper_id"] == "j1x"


def test_over_current_candidate_not_restorable():
    """跨接线承载 7A 超过额定 5A -> 不可恢复，且为唯一候选时选中项为 null。"""
    payload = base_payload(
        jumpers=[{"id": "j1", "supply_node": "a", "outage_node": "c", "resistance": 0.05, "rated_current": 5.0}]
    )
    data = client.post("/outage-restoration", json=payload).json()

    candidate = data["candidates"][0]
    assert candidate["restorable"] is False
    assert candidate["over_current_segments"] == [
        {
            "segment_id": "j1",
            "parent": "a",
            "child": "c",
            "current": pytest.approx(7.0),
            "rated_current": pytest.approx(5.0),
            "excess": pytest.approx(2.0),
        }
    ]
    assert candidate["under_voltage_nodes"] == []
    assert data["selected_jumper_id"] is None


def test_under_voltage_candidate_not_restorable():
    """跨接线电阻过大（2Ω）-> c/d 欠压，不可恢复。"""
    payload = base_payload(
        jumpers=[{"id": "j1", "supply_node": "a", "outage_node": "c", "resistance": 2.0, "rated_current": 15.0}]
    )
    data = client.post("/outage-restoration", json=payload).json()

    candidate = data["candidates"][0]
    assert candidate["restorable"] is False
    assert candidate["over_current_segments"] == []
    under = by_id(candidate, "node", "under_voltage_nodes")
    assert set(under) == {"c", "d"}
    assert under["c"]["voltage"] == pytest.approx(32.8)
    assert under["c"]["deficit"] == pytest.approx(7.2)
    assert under["d"]["voltage"] == pytest.approx(32.4)
    assert data["selected_jumper_id"] is None


def test_per_node_min_voltage_override_applies():
    """节点级 min_voltage 覆盖生效：c 阈值 46.4V 时 j2 的 c=46.15V 欠压，j1 勉强合格。"""
    payload = base_payload()
    payload["loads"][2]["min_voltage"] = 46.4  # c 节点阈值提高
    data = client.post("/outage-restoration", json=payload).json()

    j1, j2 = data["candidates"]
    # j1：c=46.45 ≥ 46.4 合格；最低余量 = 46.45-46.4 = 0.05（余量同样按覆盖阈值计算）
    assert j1["restorable"] is True
    assert j1["min_voltage_margin"] == pytest.approx(0.05)
    # j2：c=46.15 < 46.4 欠压，不可恢复
    assert j2["restorable"] is False
    assert [v["node"] for v in j2["under_voltage_nodes"]] == ["c"]
    assert data["selected_jumper_id"] == "j1"


def test_exact_rating_is_restorable():
    """恰好触限不算越限：跨接线额定 7A、承载恰为 7A 时仍可恢复。"""
    payload = base_payload(
        jumpers=[{"id": "j1", "supply_node": "a", "outage_node": "c", "resistance": 0.05, "rated_current": 7.0}]
    )
    data = client.post("/outage-restoration", json=payload).json()

    candidate = data["candidates"][0]
    segments = by_id(candidate, "segment_id", "segments")
    assert segments["j1"]["current"] == pytest.approx(7.0)
    assert segments["j1"]["over_rated"] is False
    assert candidate["restorable"] is True
    assert data["selected_jumper_id"] == "j1"


def test_supply_from_source_allowed():
    """供电侧节点可为 source 本身：失电分量直接由电源馈电。"""
    payload = base_payload(
        jumpers=[
            {"id": "j1", "supply_node": "source", "outage_node": "c", "resistance": 0.1, "rated_current": 15.0}
        ]
    )
    data = client.post("/outage-restoration", json=payload).json()

    candidate = data["candidates"][0]
    assert candidate["restorable"] is True
    segments = by_id(candidate, "segment_id", "segments")
    assert segments["s1"]["current"] == pytest.approx(5.0)  # 仅剩 a、b 负载
    assert segments["j1"]["current"] == pytest.approx(7.0)
    nodes = by_id(candidate, "node", "nodes")
    assert nodes["a"]["voltage"] == pytest.approx(47.5)
    assert nodes["c"]["voltage"] == pytest.approx(47.3)
    assert nodes["d"]["voltage"] == pytest.approx(46.9)
    assert candidate["min_voltage_margin"] == pytest.approx(6.9)
    assert data["selected_jumper_id"] == "j1"


def test_faulty_leaf_segment():
    """故障段为叶子段（s4）时失电分量仅 {d}，跨接线 c->d 恢复原拓扑。"""
    payload = base_payload(
        faulty_segment_id="s4",
        jumpers=[{"id": "j1", "supply_node": "c", "outage_node": "d", "resistance": 0.1, "rated_current": 10.0}],
    )
    data = client.post("/outage-restoration", json=payload).json()

    candidate = data["candidates"][0]
    assert candidate["restorable"] is True
    assert [s["segment_id"] for s in candidate["segments"]] == ["s1", "s2", "s3", "j1"]
    segments = by_id(candidate, "segment_id", "segments")
    assert segments["s3"]["current"] == pytest.approx(7.0)
    assert segments["j1"]["current"] == pytest.approx(4.0)
    nodes = by_id(candidate, "node", "nodes")
    assert nodes["d"]["voltage"] == pytest.approx(46.05)
    assert data["selected_jumper_id"] == "j1"


def test_deeper_reorientation_path():
    """更长链路上的重新定向：接入 e 点时 s4、s5 均翻转，电流方向随之反转。"""
    payload = base_payload(
        segments=[
            {"id": "s1", "parent": "source", "child": "a", "resistance": 0.1, "rated_current": 30.0},
            {"id": "s2", "parent": "a", "child": "b", "resistance": 0.2, "rated_current": 10.0},
            {"id": "s3", "parent": "a", "child": "c", "resistance": 0.05, "rated_current": 15.0},
            {"id": "s4", "parent": "c", "child": "d", "resistance": 0.1, "rated_current": 10.0},
            {"id": "s5", "parent": "d", "child": "e", "resistance": 0.1, "rated_current": 10.0},
        ],
        loads=[
            {"node": "a", "current": 2.0},
            {"node": "b", "current": 3.0},
            {"node": "c", "current": 3.0},
            {"node": "d", "current": 4.0},
            {"node": "e", "current": 3.0},
        ],
        jumpers=[{"id": "j1", "supply_node": "a", "outage_node": "e", "resistance": 0.05, "rated_current": 15.0}],
    )
    data = client.post("/outage-restoration", json=payload).json()

    candidate = data["candidates"][0]
    assert candidate["restorable"] is True
    segments = by_id(candidate, "segment_id", "segments")
    # 路径 c->d->e 上的 s4、s5 翻转：e->d->c
    assert segments["s5"]["parent"] == "e" and segments["s5"]["child"] == "d"
    assert segments["s4"]["parent"] == "d" and segments["s4"]["child"] == "c"
    assert segments["j1"]["current"] == pytest.approx(10.0)  # c+d+e
    assert segments["s5"]["current"] == pytest.approx(7.0)  # c+d
    assert segments["s4"]["current"] == pytest.approx(3.0)  # c
    nodes = by_id(candidate, "node", "nodes")
    assert nodes["e"]["voltage"] == pytest.approx(46.0)
    assert nodes["d"]["voltage"] == pytest.approx(45.3)
    assert nodes["c"]["voltage"] == pytest.approx(45.0)
    assert data["selected_jumper_id"] == "j1"


def test_empty_jumpers_gives_null_selection():
    """无候选跨接线：候选列表为空，选中项为 null。"""
    data = client.post("/outage-restoration", json=base_payload(jumpers=[])).json()
    assert data["candidates"] == []
    assert data["selected_jumper_id"] is None


# ---------- 跨接线 / 故障线段校验（400） ----------


def test_unknown_faulty_segment_rejected():
    resp = client.post("/outage-restoration", json=base_payload(faulty_segment_id="ghost"))
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "invalid_input"
    assert any("故障线段" in d["message"] and "不存在" in d["message"] for d in error["details"])


def test_supply_node_in_lost_component_rejected():
    payload = base_payload(
        jumpers=[{"id": "j1", "supply_node": "c", "outage_node": "d", "resistance": 0.1, "rated_current": 10.0}]
    )
    resp = client.post("/outage-restoration", json=payload)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "invalid_input"
    assert any("供电侧节点" in d["message"] and "失电分量" in d["message"] for d in error["details"])


def test_outage_node_in_source_component_rejected():
    payload = base_payload(
        jumpers=[{"id": "j1", "supply_node": "a", "outage_node": "b", "resistance": 0.1, "rated_current": 10.0}]
    )
    resp = client.post("/outage-restoration", json=payload)
    assert resp.status_code == 400
    assert any("失电侧节点" in d["message"] for d in resp.json()["error"]["details"])


def test_unknown_jumper_nodes_rejected():
    payload = base_payload(
        jumpers=[
            {"id": "j1", "supply_node": "ghost", "outage_node": "c", "resistance": 0.1, "rated_current": 10.0},
            {"id": "j2", "supply_node": "a", "outage_node": "ghost", "resistance": 0.1, "rated_current": 10.0},
        ]
    )
    resp = client.post("/outage-restoration", json=payload)
    assert resp.status_code == 400
    details = resp.json()["error"]["details"]
    assert any("供电侧节点" in d["message"] and "不存在" in d["message"] for d in details)
    assert any("失电侧节点" in d["message"] and "不存在" in d["message"] for d in details)


def test_duplicate_jumper_id_rejected():
    payload = base_payload(
        jumpers=[
            {"id": "j1", "supply_node": "a", "outage_node": "c", "resistance": 0.05, "rated_current": 15.0},
            {"id": "j1", "supply_node": "a", "outage_node": "d", "resistance": 0.05, "rated_current": 15.0},
        ]
    )
    resp = client.post("/outage-restoration", json=payload)
    assert resp.status_code == 400
    assert any("重复" in d["message"] for d in resp.json()["error"]["details"])


def test_jumper_id_collides_with_segment_rejected():
    payload = base_payload(
        jumpers=[{"id": "s1", "supply_node": "a", "outage_node": "c", "resistance": 0.05, "rated_current": 15.0}]
    )
    resp = client.post("/outage-restoration", json=payload)
    assert resp.status_code == 400
    assert any("重复" in d["message"] for d in resp.json()["error"]["details"])


# ---------- 数值校验（422） ----------


def test_negative_jumper_resistance_rejected():
    payload = base_payload()
    payload["jumpers"][0]["resistance"] = -0.1
    resp = client.post("/outage-restoration", json=payload)
    assert resp.status_code == 422
    assert any("电阻不能为负" in d["message"] for d in resp.json()["error"]["details"])


def test_non_positive_jumper_rated_current_rejected():
    payload = base_payload()
    payload["jumpers"][0]["rated_current"] = 0.0
    resp = client.post("/outage-restoration", json=payload)
    assert resp.status_code == 422
    assert any("额定电流必须为正数" in d["message"] for d in resp.json()["error"]["details"])


def test_missing_faulty_segment_id_rejected():
    payload = base_payload()
    del payload["faulty_segment_id"]
    resp = client.post("/outage-restoration", json=payload)
    assert resp.status_code == 422
    assert any("故障线段标识" in d["message"] for d in resp.json()["error"]["details"])


def test_missing_jumpers_rejected():
    payload = base_payload()
    del payload["jumpers"]
    resp = client.post("/outage-restoration", json=payload)
    assert resp.status_code == 422
    assert any("跨接线列表" in d["message"] for d in resp.json()["error"]["details"])


def test_empty_jumper_id_rejected():
    payload = base_payload()
    payload["jumpers"][0]["id"] = ""
    resp = client.post("/outage-restoration", json=payload)
    assert resp.status_code == 422
    assert any("跨接线标识" in d["message"] for d in resp.json()["error"]["details"])


def test_missing_supply_node_rejected():
    payload = base_payload()
    del payload["jumpers"][0]["supply_node"]
    resp = client.post("/outage-restoration", json=payload)
    assert resp.status_code == 422
    assert any("供电侧节点" in d["message"] for d in resp.json()["error"]["details"])
