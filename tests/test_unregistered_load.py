"""POST /unregistered-load 的端到端测试。"""

import json

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def base_payload(**overrides):
    """与 test_evaluate 相同的小树，外加 measurements：

    source --s1(0.1Ω, 20A)--> a --s2(0.2Ω, 10A)--> b
                              |
                              +--s3(0.05Ω, 15A)--> c

    基线：段电流 s1=15, s2=5, s3=8
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
        "measurements": [],
    }
    payload.update(overrides)
    return payload


# ---------- located ----------


def test_located_single_candidate():
    """仅在 s2 测得 +3A：未登记负载只能位于 b。"""
    payload = base_payload(measurements=[{"segment_id": "s2", "current": 8.0}])
    resp = client.post("/unregistered-load", json=payload)
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] == "located"
    assert data["candidate_nodes"] == ["b"]
    assert data["load_current"] == pytest.approx(3.0)
    assert data["reasons"] == []

    m = data["measurements"][0]
    assert m["segment_id"] == "s2"
    assert m["baseline_current"] == pytest.approx(5.0)
    assert m["measured_current"] == pytest.approx(8.0)
    assert m["difference"] == pytest.approx(3.0)


def test_root_segment_measurement_leaves_multiple_candidates():
    """仅在根线段 s1 测得 +3A：树下所有节点均相容（未测支路多解）。"""
    payload = base_payload(measurements=[{"segment_id": "s1", "current": 18.0}])
    data = client.post("/unregistered-load", json=payload).json()

    assert data["status"] == "located"
    assert data["candidate_nodes"] == ["a", "b", "c"]
    assert data["load_current"] == pytest.approx(3.0)


def test_zero_diff_measurement_excludes_subtree():
    """s1 正差、s3 零差：候选须排除 c 支路。"""
    payload = base_payload(
        measurements=[
            {"segment_id": "s1", "current": 18.0},
            {"segment_id": "s3", "current": 8.0},
        ]
    )
    data = client.post("/unregistered-load", json=payload).json()

    assert data["status"] == "located"
    assert data["candidate_nodes"] == ["a", "b"]
    assert data["load_current"] == pytest.approx(3.0)


def test_two_positive_measurements_pinpoint_single_node():
    """s1 与 s2 均为 +3A：负载定位于两者共同的下游 b。"""
    payload = base_payload(
        measurements=[
            {"segment_id": "s1", "current": 18.0},
            {"segment_id": "s2", "current": 8.0},
        ]
    )
    data = client.post("/unregistered-load", json=payload).json()

    assert data["status"] == "located"
    assert data["candidate_nodes"] == ["b"]
    assert data["load_current"] == pytest.approx(3.0)


def test_located_in_sibling_branch():
    """s1 与 s3 均为 +3A：负载定位于 c。"""
    payload = base_payload(
        measurements=[
            {"segment_id": "s1", "current": 18.0},
            {"segment_id": "s3", "current": 11.0},
        ]
    )
    data = client.post("/unregistered-load", json=payload).json()

    assert data["status"] == "located"
    assert data["candidate_nodes"] == ["c"]
    assert data["load_current"] == pytest.approx(3.0)


def test_positive_diffs_within_tolerance_are_consistent():
    """两个正差相差 9e-7 A（< 1e-6）：视为一致，负载取均值后按六位小数返回。"""
    payload = base_payload(
        measurements=[
            {"segment_id": "s1", "current": 18.0},
            {"segment_id": "s2", "current": 8.0000009},
        ]
    )
    data = client.post("/unregistered-load", json=payload).json()

    assert data["status"] == "located"
    assert data["candidate_nodes"] == ["b"]
    assert data["load_current"] == pytest.approx(3.0)  # 均值 3.00000045 舍入为 3.0


def test_load_current_rounded_to_six_decimals():
    """负载电流按六位小数返回。"""
    payload = base_payload(measurements=[{"segment_id": "s2", "current": 5.0 + 1.0 / 3.0}])
    data = client.post("/unregistered-load", json=payload).json()

    assert data["status"] == "located"
    assert data["candidate_nodes"] == ["b"]
    assert data["load_current"] == pytest.approx(0.333333)


def test_measurement_with_auto_generated_segment_ids():
    """线段缺省 id 时，测点可引用自动生成的 seg-<序号>。"""
    payload = base_payload()
    for seg in payload["segments"]:
        del seg["id"]
    payload["measurements"] = [{"segment_id": "seg-2", "current": 8.0}]
    data = client.post("/unregistered-load", json=payload).json()

    assert data["status"] == "located"
    assert data["candidate_nodes"] == ["b"]


# ---------- undetermined ----------


def test_zero_diff_everywhere_undetermined():
    """全部测点与基线一致：无显著正差，返回 undetermined。"""
    payload = base_payload(
        measurements=[
            {"segment_id": "s1", "current": 15.0},
            {"segment_id": "s2", "current": 5.0},
            {"segment_id": "s3", "current": 8.0},
        ]
    )
    data = client.post("/unregistered-load", json=payload).json()

    assert data["status"] == "undetermined"
    assert data["load_current"] is None
    assert data["candidate_nodes"] == []
    assert data["reasons"] == []


def test_empty_measurements_undetermined():
    """未传任何测点：无法确定。"""
    data = client.post("/unregistered-load", json=base_payload()).json()
    assert data["status"] == "undetermined"


def test_tiny_diff_below_tolerance_is_not_significant():
    """差值 5e-7 A（< 1e-6）不视为显著正差。"""
    payload = base_payload(measurements=[{"segment_id": "s2", "current": 5.0000005}])
    data = client.post("/unregistered-load", json=payload).json()
    assert data["status"] == "undetermined"


def test_diff_just_above_tolerance_is_significant():
    """差值 2e-6 A（> 1e-6）视为显著正差。"""
    payload = base_payload(measurements=[{"segment_id": "s2", "current": 5.000002}])
    data = client.post("/unregistered-load", json=payload).json()

    assert data["status"] == "located"
    assert data["candidate_nodes"] == ["b"]
    assert data["load_current"] == pytest.approx(0.000002)


def test_baseline_compared_unrounded():
    """基线比较使用未舍入电流：基线 1/3 A 时，实测 0.3333341 的真实差值 < 1e-6。

    若与六位小数舍入后的基线（0.333333）比较，差值将达 1.1e-6 而误判为显著正差。
    """
    payload = base_payload()
    payload["loads"] = [{"node": "b", "current": 1.0 / 3.0}]
    payload["measurements"] = [{"segment_id": "s2", "current": 0.3333341}]
    data = client.post("/unregistered-load", json=payload).json()
    assert data["status"] == "undetermined"


# ---------- inconsistent ----------


def test_negative_diff_inconsistent():
    """实测低于基线：负差矛盾。"""
    payload = base_payload(measurements=[{"segment_id": "s2", "current": 4.0}])
    data = client.post("/unregistered-load", json=payload).json()

    assert data["status"] == "inconsistent"
    assert data["load_current"] is None
    assert data["candidate_nodes"] == []
    assert any("s2" in r for r in data["reasons"])


def test_disagreeing_positive_diffs_inconsistent():
    """两个显著正差相差超过 1e-6 A：相互矛盾。"""
    payload = base_payload(
        measurements=[
            {"segment_id": "s1", "current": 18.0},
            {"segment_id": "s2", "current": 10.0},
        ]
    )
    data = client.post("/unregistered-load", json=payload).json()

    assert data["status"] == "inconsistent"
    assert data["load_current"] is None
    assert data["candidate_nodes"] == []
    assert any("矛盾" in r for r in data["reasons"])


def test_conflicting_measurement_positions_inconsistent():
    """s2 正差要求负载在其下游，但零差的 s1 是其必经上游：无相容节点。"""
    payload = base_payload(
        measurements=[
            {"segment_id": "s1", "current": 15.0},
            {"segment_id": "s2", "current": 8.0},
        ]
    )
    data = client.post("/unregistered-load", json=payload).json()

    assert data["status"] == "inconsistent"
    assert data["candidate_nodes"] == []
    assert any("矛盾" in r for r in data["reasons"])


# ---------- 测点校验（400 invalid_input） ----------


def test_duplicate_measurement_segment_rejected():
    payload = base_payload(
        measurements=[
            {"segment_id": "s1", "current": 15.0},
            {"segment_id": "s1", "current": 16.0},
        ]
    )
    resp = client.post("/unregistered-load", json=payload)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "invalid_input"
    assert any("重复" in d["message"] for d in error["details"])


def test_unknown_measurement_segment_rejected():
    payload = base_payload(measurements=[{"segment_id": "ghost", "current": 1.0}])
    resp = client.post("/unregistered-load", json=payload)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "invalid_input"
    assert any("不存在" in d["message"] for d in error["details"])


# ---------- 读数校验（422 validation_error） ----------


def test_negative_reading_rejected():
    payload = base_payload(measurements=[{"segment_id": "s1", "current": -0.5}])
    resp = client.post("/unregistered-load", json=payload)
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert any("实测电流" in d["message"] for d in error["details"])


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
def test_non_finite_reading_rejected(bad):
    payload = base_payload(measurements=[{"segment_id": "s1", "current": bad}])
    # httpx 的 json= 不允许非有限浮点，改用原始请求体发送 Infinity / NaN 字面量
    resp = client.post(
        "/unregistered-load",
        content=json.dumps(payload),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert any("实测电流" in d["message"] for d in error["details"])


def test_missing_segment_id_rejected():
    payload = base_payload(measurements=[{"current": 1.0}])
    resp = client.post("/unregistered-load", json=payload)
    assert resp.status_code == 422
    assert any("线段标识" in d["message"] for d in resp.json()["error"]["details"])


# ---------- 沿用 /evaluate 的输入校验 ----------


def test_invalid_tree_still_rejected():
    payload = base_payload()
    payload["segments"].append(
        {"parent": "x", "child": "y", "resistance": 0.1, "rated_current": 10.0}
    )
    resp = client.post("/unregistered-load", json=payload)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_input"


def test_unknown_load_node_still_rejected():
    payload = base_payload()
    payload["loads"].append({"node": "ghost", "current": 1.0})
    resp = client.post("/unregistered-load", json=payload)
    assert resp.status_code == 400
    assert any("不存在" in d["message"] for d in resp.json()["error"]["details"])
