"""POST /peak-risk 的端到端测试。

测试用树（与 test_evaluate 相同）：

source --s1(0.1Ω, 20A)--> a --s2(0.2Ω, 10A)--> b
                          |
                          +--s3(0.05Ω, 15A)--> c

负载（序号即 loads 数组下标）：
- 0: a, current=2, peak=6  -> Δ4
- 1: b, current=5, peak=9  -> Δ4
- 2: c, current=8（无 peak_current，Δ0）

基线：段电流 s1=15, s2=5, s3=8；节点电压 a=46.5, b=45.5, c=46.1
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
            {"node": "a", "current": 2.0, "peak_current": 6.0},
            {"node": "b", "current": 5.0, "peak_current": 9.0},
            {"node": "c", "current": 8.0},
        ],
        "max_simultaneous_peaks": 2,
    }
    payload.update(overrides)
    return payload


def by_id(data, key, items):
    return {item[key]: item for item in data[items]}


def test_worst_case_with_two_simultaneous_peaks():
    """K=2：s1 取 Δ4+Δ4 越限；各节点最低电压按公共路径电阻折算。"""
    resp = client.post("/peak-risk", json=base_payload())
    assert resp.status_code == 200
    data = resp.json()

    assert data["ok"] is False
    assert data["max_simultaneous_peaks"] == 2

    segments = by_id(data, "segment_id", "segments")
    # s1 下游全部负载：贡献 4/4/0 -> 取负载 0、1 -> 15+8=23
    assert segments["s1"]["current"] == pytest.approx(23.0)
    assert segments["s1"]["peak_loads"] == [0, 1]
    assert segments["s1"]["over_rated"] is True
    # s2 下游仅负载 1 -> 5+4=9；s3 下游负载 2 增量为零 -> 保持 8
    assert segments["s2"]["current"] == pytest.approx(9.0)
    assert segments["s2"]["peak_loads"] == [1]
    assert segments["s3"]["current"] == pytest.approx(8.0)
    assert segments["s3"]["peak_loads"] == []

    nodes = by_id(data, "node", "nodes")
    # a：负载 0/1 各经公共段 s1(0.1Ω) 贡献 0.4V -> 46.5-0.8=45.7
    assert nodes["a"]["voltage"] == pytest.approx(45.7)
    assert nodes["a"]["peak_loads"] == [0, 1]
    # b：负载 1 经 s1+s2(0.3Ω) 贡献 1.2V，负载 0 贡献 0.4V -> 45.5-1.6=43.9
    assert nodes["b"]["voltage"] == pytest.approx(43.9)
    assert nodes["b"]["peak_loads"] == [0, 1]
    # c：负载 0/1 各经公共段 s1 贡献 0.4V -> 46.1-0.8=45.3
    assert nodes["c"]["voltage"] == pytest.approx(45.3)
    assert nodes["c"]["peak_loads"] == [0, 1]
    # source 电压不受峰值影响
    assert nodes["source"]["voltage"] == pytest.approx(48.0)
    assert nodes["source"]["peak_loads"] == []

    assert data["over_current_segments"] == [
        {
            "segment_id": "s1",
            "parent": "source",
            "child": "a",
            "current": pytest.approx(23.0),
            "rated_current": pytest.approx(20.0),
            "excess": pytest.approx(3.0),
            "peak_loads": [0, 1],
        }
    ]
    assert data["under_voltage_nodes"] == []


def test_tie_broken_by_input_index_and_per_constraint_combinations():
    """K=1：s1 上负载 0/1 贡献相同（Δ4），按输入序号取 0；节点 b 则取负载 1。"""
    resp = client.post("/peak-risk", json=base_payload(max_simultaneous_peaks=1))
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True

    segments = by_id(data, "segment_id", "segments")
    assert segments["s1"]["current"] == pytest.approx(19.0)
    assert segments["s1"]["peak_loads"] == [0]  # 与负载 1 贡献相同，序号小者优先

    nodes = by_id(data, "node", "nodes")
    # 不同约束对应不同峰值组合：节点 b 的最坏组合是负载 1（贡献 1.2V > 0.4V）
    assert nodes["b"]["voltage"] == pytest.approx(44.3)
    assert nodes["b"]["peak_loads"] == [1]
    # 节点 a/c：负载 0/1 贡献均为 0.4V，按序号取 0
    assert nodes["a"]["voltage"] == pytest.approx(46.1)
    assert nodes["a"]["peak_loads"] == [0]
    assert nodes["c"]["voltage"] == pytest.approx(45.7)
    assert nodes["c"]["peak_loads"] == [0]


def test_default_peak_current_equals_current():
    """缺省 peak_current 时增量为零，任何 K 下都不会被选入峰值组合。"""
    payload = base_payload(max_simultaneous_peaks=3)
    data = client.post("/peak-risk", json=payload).json()

    segments = by_id(data, "segment_id", "segments")
    assert segments["s3"]["current"] == pytest.approx(8.0)
    assert segments["s3"]["peak_loads"] == []
    # K 超过正贡献负载数时只取正贡献者（负载 2 不计入）
    assert segments["s1"]["peak_loads"] == [0, 1]
    assert segments["s1"]["current"] == pytest.approx(23.0)


def test_same_node_loads_counted_separately():
    """同节点多条负载分别计数：K=1 时 b 上两条负载只能有一条升至峰值。"""
    payload = base_payload(
        loads=[
            {"node": "a", "current": 2.0, "peak_current": 6.0},
            {"node": "b", "current": 5.0, "peak_current": 9.0},  # Δ4
            {"node": "b", "current": 1.0, "peak_current": 4.0},  # Δ3
            {"node": "c", "current": 8.0},
        ],
        max_simultaneous_peaks=1,
    )
    data = client.post("/peak-risk", json=payload).json()

    segments = by_id(data, "segment_id", "segments")
    # s2 基线 6A；若两条负载合并计为一次峰值则为 6+7=13，分别计数时只能取 Δ4 -> 10
    assert segments["s2"]["current"] == pytest.approx(10.0)
    assert segments["s2"]["peak_loads"] == [1]

    payload["max_simultaneous_peaks"] = 2
    data = client.post("/peak-risk", json=payload).json()
    segments = by_id(data, "segment_id", "segments")
    assert segments["s2"]["current"] == pytest.approx(13.0)
    assert segments["s2"]["peak_loads"] == [1, 2]
    assert segments["s2"]["over_rated"] is True


def test_zero_simultaneous_peaks_matches_baseline():
    """K=0 时无负载可升至峰值，结果与 /evaluate 基线一致。"""
    data = client.post("/peak-risk", json=base_payload(max_simultaneous_peaks=0)).json()

    assert data["ok"] is True
    segments = by_id(data, "segment_id", "segments")
    assert segments["s1"]["current"] == pytest.approx(15.0)
    assert segments["s2"]["current"] == pytest.approx(5.0)
    assert segments["s3"]["current"] == pytest.approx(8.0)
    assert all(s["peak_loads"] == [] for s in data["segments"])
    nodes = by_id(data, "node", "nodes")
    assert nodes["b"]["voltage"] == pytest.approx(45.5)
    assert all(n["peak_loads"] == [] for n in data["nodes"])


def test_under_voltage_violation_with_per_node_override():
    """节点级 min_voltage 覆盖生效：b 最坏电压 44.3V 低于 45V 阈值。"""
    payload = base_payload(max_simultaneous_peaks=1)
    payload["loads"][1]["min_voltage"] = 45.0
    data = client.post("/peak-risk", json=payload).json()

    assert data["ok"] is False
    assert data["over_current_segments"] == []
    assert data["under_voltage_nodes"] == [
        {
            "node": "b",
            "voltage": pytest.approx(44.3),
            "min_voltage": pytest.approx(45.0),
            "deficit": pytest.approx(0.7),
            "peak_loads": [1],
        }
    ]


def test_exact_limit_is_not_violation():
    """恰好触限不算越限：最坏电流等于额定电流时判定合格。"""
    payload = base_payload()
    payload["segments"][0]["rated_current"] = 23.0  # s1 最坏恰好 23A
    data = client.post("/peak-risk", json=payload).json()

    assert data["ok"] is True
    segments = by_id(data, "segment_id", "segments")
    assert segments["s1"]["current"] == pytest.approx(23.0)
    assert segments["s1"]["over_rated"] is False
    assert data["over_current_segments"] == []


# ---------- 数值校验（422） ----------


def test_peak_below_current_rejected():
    payload = base_payload()
    payload["loads"][0]["peak_current"] = 1.0  # 小于 current=2
    resp = client.post("/peak-risk", json=payload)
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert any("峰值电流不能小于负载电流" in d["message"] for d in error["details"])


def test_negative_peak_current_rejected():
    payload = base_payload()
    payload["loads"][0]["peak_current"] = -1.0
    resp = client.post("/peak-risk", json=payload)
    assert resp.status_code == 422
    assert any("峰值电流不能为负" in d["message"] for d in resp.json()["error"]["details"])


def test_negative_max_simultaneous_peaks_rejected():
    resp = client.post("/peak-risk", json=base_payload(max_simultaneous_peaks=-1))
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert any("最大同时峰值数不能为负" in d["message"] for d in error["details"])


def test_missing_max_simultaneous_peaks_rejected():
    payload = base_payload()
    del payload["max_simultaneous_peaks"]
    resp = client.post("/peak-risk", json=payload)
    assert resp.status_code == 422
    assert any("最大同时峰值数" in d["message"] for d in resp.json()["error"]["details"])


# ---------- 树 / 负载校验（400） ----------


def test_invalid_tree_still_rejected():
    payload = base_payload()
    payload["segments"].append(
        {"parent": "x", "child": "y", "resistance": 0.1, "rated_current": 10.0}
    )
    resp = client.post("/peak-risk", json=payload)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_input"


def test_unknown_load_node_rejected():
    payload = base_payload()
    payload["loads"].append({"node": "ghost", "current": 1.0, "peak_current": 2.0})
    resp = client.post("/peak-risk", json=payload)
    assert resp.status_code == 400
    assert any("不存在" in d["message"] for d in resp.json()["error"]["details"])
