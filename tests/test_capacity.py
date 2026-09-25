"""POST /capacity 的端到端测试。"""

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def base_payload(**overrides):
    """与 test_evaluate 相同的小树，外加 target_node：

    source --s1(0.1Ω, 20A)--> a --s2(0.2Ω, 10A)--> b
                              |
                              +--s3(0.05Ω, 15A)--> c

    基线：段电流 s1=15, s2=5, s3=8；节点电压 a=46.5, b=45.5, c=46.1
    """
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
        "target_node": "b",
    }
    payload.update(overrides)
    return payload


def test_segment_headroom_limits_and_tie():
    """路径线段 s1（余量 5A）与 s2（余量 5A）同时触限。"""
    resp = client.post("/capacity", json=base_payload())
    assert resp.status_code == 200
    data = resp.json()

    assert data["target_node"] == "b"
    assert data["max_additional_current"] == pytest.approx(5.0)
    assert data["limiting_factors"] == [
        {"type": "segment", "id": "s1"},
        {"type": "segment", "id": "s2"},
    ]


def test_added_current_keeps_baseline_ok():
    """把返回的最大增量加到目标节点后，/evaluate 仍全部合格（恰好触限不算越限）。"""
    payload = base_payload()
    data = client.post("/capacity", json=payload).json()

    payload["loads"].append({"node": "b", "current": data["max_additional_current"]})
    result = client.post("/evaluate", json=payload).json()
    assert result["ok"] is True
    currents = {s["segment_id"]: s["current"] for s in result["segments"]}
    assert currents["s1"] == pytest.approx(20.0)
    assert currents["s2"] == pytest.approx(10.0)


def test_sibling_branch_voltage_limits():
    """兄弟支路节点 c 经公共线段 s1 受压，成为限制因素。"""
    payload = base_payload()
    payload["loads"][2]["min_voltage"] = 46.05  # c 余量 0.05V / 公共电阻 0.1Ω = 0.5A
    resp = client.post("/capacity", json=payload)
    assert resp.status_code == 200
    data = resp.json()

    assert data["max_additional_current"] == pytest.approx(0.5)
    assert data["limiting_factors"] == [{"type": "node", "id": "c"}]


def test_ancestor_node_voltage_limits():
    """目标的上游节点 a 经公共线段 s1 受压，成为限制因素。"""
    payload = base_payload()
    payload["loads"][0]["min_voltage"] = 46.2  # a 余量 0.3V / 0.1Ω = 3A
    resp = client.post("/capacity", json=payload)
    data = resp.json()

    assert data["max_additional_current"] == pytest.approx(3.0)
    assert data["limiting_factors"] == [{"type": "node", "id": "a"}]


def test_target_self_voltage_limit_truncated():
    """目标节点自身欠压余量触限，结果向下截断至 6 位小数。"""
    payload = base_payload()
    payload["loads"][1]["min_voltage"] = 45.0  # b 余量 0.5V / 0.3Ω = 1.6666...A
    resp = client.post("/capacity", json=payload)
    data = resp.json()

    assert data["max_additional_current"] == pytest.approx(1.666666)
    assert data["limiting_factors"] == [{"type": "node", "id": "b"}]


def test_mid_tree_target_affects_descendants():
    """目标为中间节点 a 时，下游 b/c 经公共线段 s1 同样受压。"""
    resp = client.post("/capacity", json=base_payload(target_node="a"))
    data = resp.json()

    # 仅 s1 在路径上（余量 5A）；b 余量 55A、c 余量 61A、a 余量 65A
    assert data["max_additional_current"] == pytest.approx(5.0)
    assert data["limiting_factors"] == [{"type": "segment", "id": "s1"}]


def test_off_path_segments_do_not_limit():
    """不在目标路径上的线段不影响可增容量。"""
    payload = base_payload(target_node="c")
    payload["segments"][1]["rated_current"] = 6.0  # s2 余量仅 1A，但不在 c 的路径上
    resp = client.post("/capacity", json=payload)
    data = resp.json()

    assert data["max_additional_current"] == pytest.approx(5.0)
    assert data["limiting_factors"] == [{"type": "segment", "id": "s1"}]


def test_zero_resistance_path_only_segment_limits():
    """路径电阻全为零时不产生欠压约束，仅线段额定限制。"""
    payload = base_payload()
    payload["segments"][0]["resistance"] = 0.0
    payload["segments"][1]["resistance"] = 0.0
    resp = client.post("/capacity", json=payload)
    assert resp.status_code == 200
    data = resp.json()

    assert data["max_additional_current"] == pytest.approx(5.0)
    assert data["limiting_factors"] == [
        {"type": "segment", "id": "s1"},
        {"type": "segment", "id": "s2"},
    ]


def test_exact_limit_at_rated_is_allowed():
    """恰好触限：增量使 s3 恰好达到额定电流，仍判定可行。"""
    payload = base_payload(target_node="c")
    payload["segments"][0]["rated_current"] = 100.0  # s1 不再限制
    payload["segments"][2]["rated_current"] = 10.5  # s3 余量恰好 2.5A
    resp = client.post("/capacity", json=payload)
    data = resp.json()

    assert data["max_additional_current"] == pytest.approx(2.5)
    assert data["limiting_factors"] == [{"type": "segment", "id": "s3"}]

    payload["loads"].append({"node": "c", "current": data["max_additional_current"]})
    result = client.post("/evaluate", json=payload).json()
    assert result["ok"] is True


# ---------- 基线越限（400 baseline_violation） ----------


def test_baseline_over_current_returns_400():
    payload = base_payload()
    payload["segments"][0]["rated_current"] = 14.0  # s1 实际承载 15A
    resp = client.post("/capacity", json=payload)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "baseline_violation"
    assert any("s1" in d["message"] for d in error["details"])


def test_baseline_under_voltage_returns_400():
    payload = base_payload()
    payload["segments"][0]["resistance"] = 1.0  # a/b/c 全部欠压
    resp = client.post("/capacity", json=payload)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "baseline_violation"
    for node in ("a", "b", "c"):
        assert any(f"'{node}'" in d["message"] for d in error["details"])


# ---------- 目标节点校验 ----------


def test_target_source_rejected():
    resp = client.post("/capacity", json=base_payload(target_node="source"))
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "invalid_input"
    assert any("source" in d["message"] for d in error["details"])


def test_target_unknown_rejected():
    resp = client.post("/capacity", json=base_payload(target_node="ghost"))
    assert resp.status_code == 400
    assert any("不存在" in d["message"] for d in resp.json()["error"]["details"])


def test_target_missing_rejected():
    payload = base_payload()
    del payload["target_node"]
    resp = client.post("/capacity", json=payload)
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert any("目标节点" in d["message"] for d in error["details"])


def test_invalid_tree_still_rejected():
    payload = base_payload()
    payload["segments"].append(
        {"parent": "x", "child": "y", "resistance": 0.1, "rated_current": 10.0}
    )
    resp = client.post("/capacity", json=payload)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_input"
