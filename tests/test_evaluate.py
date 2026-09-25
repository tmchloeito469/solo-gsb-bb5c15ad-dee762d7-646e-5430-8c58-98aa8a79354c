"""POST /evaluate 的端到端测试。"""

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def base_payload(**overrides):
    """一棵三层小树：

    source --s1(0.1Ω, 20A)--> a --s2(0.2Ω, 10A)--> b
                              |
                              +--s3(0.05Ω, 15A)--> c

    负载：a=2A, b=5A, c=8A
    段电流：s2=5, s3=8, s1=15；节点电压：a=46.5, b=45.5, c=46.1
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
    }
    payload.update(overrides)
    return payload


def test_happy_path_currents_and_voltages():
    resp = client.post("/evaluate", json=base_payload())
    assert resp.status_code == 200
    data = resp.json()

    assert data["ok"] is True
    assert data["total_source_current"] == pytest.approx(15.0)
    assert data["over_current_segments"] == []
    assert data["under_voltage_nodes"] == []

    currents = {s["segment_id"]: s["current"] for s in data["segments"]}
    assert currents == {"s1": pytest.approx(15.0), "s2": pytest.approx(5.0), "s3": pytest.approx(8.0)}

    drops = {s["segment_id"]: s["voltage_drop"] for s in data["segments"]}
    assert drops["s1"] == pytest.approx(1.5)
    assert drops["s2"] == pytest.approx(1.0)
    assert drops["s3"] == pytest.approx(0.4)

    voltages = {n["node"]: n["voltage"] for n in data["nodes"]}
    assert voltages["source"] == pytest.approx(48.0)
    assert voltages["a"] == pytest.approx(46.5)
    assert voltages["b"] == pytest.approx(45.5)
    assert voltages["c"] == pytest.approx(46.1)


def test_over_current_detected():
    payload = base_payload()
    payload["segments"][0]["rated_current"] = 14.0  # s1 实际承载 15A
    resp = client.post("/evaluate", json=payload)
    assert resp.status_code == 200
    data = resp.json()

    assert data["ok"] is False
    assert len(data["over_current_segments"]) == 1
    violation = data["over_current_segments"][0]
    assert violation["segment_id"] == "s1"
    assert violation["current"] == pytest.approx(15.0)
    assert violation["rated_current"] == pytest.approx(14.0)
    assert violation["excess"] == pytest.approx(1.0)


def test_under_voltage_detected():
    payload = base_payload()
    payload["segments"][0]["resistance"] = 1.0  # a 点压降 15V -> 33V
    resp = client.post("/evaluate", json=payload)
    assert resp.status_code == 200
    data = resp.json()

    assert data["ok"] is False
    under = {v["node"]: v for v in data["under_voltage_nodes"]}
    assert set(under) == {"a", "b", "c"}
    assert under["a"]["voltage"] == pytest.approx(33.0)
    assert under["a"]["deficit"] == pytest.approx(7.0)
    assert under["b"]["voltage"] == pytest.approx(32.0)
    assert under["c"]["voltage"] == pytest.approx(32.6)


def test_per_node_min_voltage_override():
    payload = base_payload()
    payload["loads"][2]["min_voltage"] = 47.0  # 仅 c 节点阈值提高到 47V（实际 46.1V）
    resp = client.post("/evaluate", json=payload)
    assert resp.status_code == 200
    data = resp.json()

    assert data["ok"] is False
    assert [v["node"] for v in data["under_voltage_nodes"]] == ["c"]
    node_c = next(n for n in data["nodes"] if n["node"] == "c")
    assert node_c["min_voltage"] == pytest.approx(47.0)


def test_duplicate_loads_are_summed():
    payload = base_payload()
    payload["loads"].append({"node": "b", "current": 1.0})  # b: 5 + 1 = 6A
    resp = client.post("/evaluate", json=payload)
    assert resp.status_code == 200
    data = resp.json()

    currents = {s["segment_id"]: s["current"] for s in data["segments"]}
    assert currents["s2"] == pytest.approx(6.0)
    assert currents["s1"] == pytest.approx(16.0)


def test_segment_ids_auto_generated():
    payload = base_payload()
    for seg in payload["segments"]:
        del seg["id"]
    resp = client.post("/evaluate", json=payload)
    assert resp.status_code == 200
    ids = [s["segment_id"] for s in resp.json()["segments"]]
    assert ids == ["seg-1", "seg-2", "seg-3"]


# ---------- 数值校验（422） ----------


@pytest.mark.parametrize("voltage", [0.0, -48.0])
def test_non_positive_source_voltage_rejected(voltage):
    resp = client.post("/evaluate", json=base_payload(source_voltage=voltage))
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert any("电源电压" in d["message"] for d in error["details"])


def test_negative_resistance_rejected():
    payload = base_payload()
    payload["segments"][0]["resistance"] = -0.1
    resp = client.post("/evaluate", json=payload)
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert any("电阻不能为负" in d["message"] for d in error["details"])


def test_negative_load_rejected():
    payload = base_payload()
    payload["loads"][0]["current"] = -1.0
    resp = client.post("/evaluate", json=payload)
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert any("负载电流不能为负" in d["message"] for d in error["details"])


def test_empty_segments_rejected():
    resp = client.post("/evaluate", json=base_payload(segments=[]))
    assert resp.status_code == 422


# ---------- 树结构校验（400） ----------


def test_cycle_rejected():
    payload = base_payload()
    payload["segments"] = [
        {"parent": "source", "child": "a", "resistance": 0.1, "rated_current": 10.0},
        {"parent": "a", "child": "b", "resistance": 0.1, "rated_current": 10.0},
        {"parent": "b", "child": "a", "resistance": 0.1, "rated_current": 10.0},
    ]
    resp = client.post("/evaluate", json=payload)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "invalid_input"
    assert any("环路" in d["message"] or "多个父节点" in d["message"] for d in error["details"])


def test_disconnected_segment_rejected():
    payload = base_payload()
    payload["segments"].append(
        {"parent": "x", "child": "y", "resistance": 0.1, "rated_current": 10.0}
    )
    resp = client.post("/evaluate", json=payload)
    assert resp.status_code == 400
    assert any("不连通" in d["message"] for d in resp.json()["error"]["details"])


def test_multiple_parents_rejected():
    payload = base_payload()
    payload["segments"].append(
        {"parent": "source", "child": "b", "resistance": 0.1, "rated_current": 10.0}
    )
    resp = client.post("/evaluate", json=payload)
    assert resp.status_code == 400
    assert any("多个父节点" in d["message"] for d in resp.json()["error"]["details"])


def test_source_as_child_rejected():
    payload = base_payload()
    payload["segments"].append(
        {"parent": "a", "child": "source", "resistance": 0.1, "rated_current": 10.0}
    )
    resp = client.post("/evaluate", json=payload)
    assert resp.status_code == 400
    assert any("不能作为子节点" in d["message"] for d in resp.json()["error"]["details"])


def test_duplicate_segment_id_rejected():
    payload = base_payload()
    payload["segments"][1]["id"] = "s1"
    resp = client.post("/evaluate", json=payload)
    assert resp.status_code == 400
    assert any("重复" in d["message"] for d in resp.json()["error"]["details"])


def test_unknown_load_node_rejected():
    payload = base_payload()
    payload["loads"].append({"node": "ghost", "current": 1.0})
    resp = client.post("/evaluate", json=payload)
    assert resp.status_code == 400
    assert any("不存在" in d["message"] for d in resp.json()["error"]["details"])


def test_health_and_index():
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/").status_code == 200
