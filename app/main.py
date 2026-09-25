"""直流配电树压降校核 API。"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .core import (
    BaselineViolationError,
    InputValidationError,
    capacity,
    evaluate,
    peak_risk,
    unregistered_load,
)
from .models import (
    CapacityRequest,
    CapacityResponse,
    EvaluateRequest,
    EvaluateResponse,
    PeakRiskRequest,
    PeakRiskResponse,
    UnregisteredLoadRequest,
    UnregisteredLoadResponse,
)

app = FastAPI(
    title="直流配电树压降校核 API",
    description=(
        "接收以 source 为根的配电树（线段电阻 / 额定电流、节点恒定负载），"
        "自叶向根累计各段电流，自根向叶计算压降与节点电压，"
        "返回超额定电流与欠压位置；并可评估指定节点可追加的最大恒定负载电流，"
        "根据线段实测电流定位唯一未登记正恒定负载的候选节点，"
        "或评估至多 K 条负载同时升至峰值时的最坏越限风险。"
    ),
    version="1.3.0",
)


def _error_body(code: str, message: str, details: list) -> dict:
    return {"error": {"code": code, "message": message, "details": details}}


# 字段中文标签与常见校验错误的定制提示
_FIELD_LABELS = {
    "source_voltage": "电源电压",
    "min_voltage": "最低允许电压",
    "segments": "线段列表",
    "loads": "负载列表",
    "id": "线段标识",
    "parent": "父节点",
    "child": "子节点",
    "resistance": "电阻",
    "rated_current": "额定电流",
    "node": "节点",
    "current": "负载电流",
    "target_node": "目标节点",
    "measurements": "测点列表",
    "segment_id": "线段标识",
    "peak_current": "峰值电流",
    "max_simultaneous_peaks": "最大同时峰值数",
}

# measurements 内字段的中文标签（与负载字段同名时区分为“实测”）
_MEASUREMENT_FIELD_LABELS = {
    "current": "实测电流",
    "segment_id": "测点线段标识",
}

_CUSTOM_MESSAGES = {
    ("source_voltage", "greater_than"): "电源电压必须为正数（> 0）",
    ("resistance", "greater_than_equal"): "电阻不能为负（须 ≥ 0）",
    ("current", "greater_than_equal"): "负载电流不能为负（须 ≥ 0）",
    ("rated_current", "greater_than"): "额定电流必须为正数（> 0）",
    ("min_voltage", "greater_than_equal"): "最低允许电压不能为负（须 ≥ 0）",
    ("peak_current", "greater_than_equal"): "峰值电流不能为负（须 ≥ 0）",
    ("max_simultaneous_peaks", "greater_than_equal"): "最大同时峰值数不能为负（须 ≥ 0）",
}


def _translate_error(err: dict) -> dict:
    loc = [str(x) for x in err.get("loc", []) if x != "body"]
    field = loc[-1] if loc else ""
    in_measurements = "measurements" in loc
    if in_measurements:
        label = _MEASUREMENT_FIELD_LABELS.get(field, field or "请求体")
    else:
        label = _FIELD_LABELS.get(field, field or "请求体")
    err_type = err.get("type", "")
    ctx = err.get("ctx", {})

    custom = _CUSTOM_MESSAGES.get((field, err_type)) if not in_measurements else None
    if custom:
        message = custom
    elif err_type == "missing":
        message = f"缺少必填字段：{label}"
    elif err_type == "greater_than":
        message = f"{label}必须大于 {ctx.get('gt')}"
    elif err_type == "greater_than_equal":
        message = f"{label}不能小于 {ctx.get('ge')}"
    elif err_type == "finite_number":
        message = f"{label}必须为有限数值"
    elif err_type == "string_too_short":
        message = f"{label}不能为空字符串"
    elif err_type in ("too_short", "list_too_short"):
        message = f"{label}不能为空"
    elif err_type == "value_error":
        message = err.get("msg", "参数无效")
        if message.startswith("Value error, "):
            message = message[len("Value error, "):]
    elif err_type.endswith("_parsing") or err_type.endswith("_type"):
        message = f"{label}的类型无效"
    else:
        message = err.get("msg", "参数无效")
    return {"field": ".".join(loc), "message": message}


@app.exception_handler(RequestValidationError)
async def request_validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    details = [_translate_error(err) for err in exc.errors()]
    return JSONResponse(
        status_code=422,
        content=_error_body("validation_error", "请求参数校验失败", details),
    )


@app.exception_handler(InputValidationError)
async def input_validation_handler(request: Request, exc: InputValidationError) -> JSONResponse:
    details = [{"field": None, "message": item} for item in exc.details]
    return JSONResponse(
        status_code=400,
        content=_error_body("invalid_input", exc.message, details),
    )


@app.exception_handler(BaselineViolationError)
async def baseline_violation_handler(request: Request, exc: BaselineViolationError) -> JSONResponse:
    details = [{"field": None, "message": item} for item in exc.details]
    return JSONResponse(
        status_code=400,
        content=_error_body("baseline_violation", exc.message, details),
    )


@app.post(
    "/evaluate",
    response_model=EvaluateResponse,
    summary="校核直流配电树的压降、线段电流与电压越限",
)
def evaluate_endpoint(payload: EvaluateRequest) -> dict:
    return evaluate(
        source_voltage=payload.source_voltage,
        min_voltage=payload.min_voltage,
        segments_in=payload.segments,
        loads_in=payload.loads,
    )


@app.post(
    "/capacity",
    response_model=CapacityResponse,
    summary="评估目标节点可追加的最大恒定负载电流",
)
def capacity_endpoint(payload: CapacityRequest) -> dict:
    return capacity(
        source_voltage=payload.source_voltage,
        min_voltage=payload.min_voltage,
        segments_in=payload.segments,
        loads_in=payload.loads,
        target_node=payload.target_node,
    )


@app.post(
    "/unregistered-load",
    response_model=UnregisteredLoadResponse,
    summary="根据线段实测电流定位唯一未登记正恒定负载的候选节点",
)
def unregistered_load_endpoint(payload: UnregisteredLoadRequest) -> dict:
    return unregistered_load(
        source_voltage=payload.source_voltage,
        segments_in=payload.segments,
        loads_in=payload.loads,
        measurements_in=payload.measurements,
    )


@app.post(
    "/peak-risk",
    response_model=PeakRiskResponse,
    summary="评估至多 K 条负载同时升至峰值时的最坏越限风险",
)
def peak_risk_endpoint(payload: PeakRiskRequest) -> dict:
    return peak_risk(
        source_voltage=payload.source_voltage,
        min_voltage=payload.min_voltage,
        segments_in=payload.segments,
        loads_in=payload.loads,
        max_simultaneous_peaks=payload.max_simultaneous_peaks,
    )


@app.get("/health", summary="健康检查")
def health() -> dict:
    return {"status": "ok"}


@app.get("/", summary="服务信息")
def index() -> dict:
    return {
        "service": "dc-tree-voltage-drop-api",
        "evaluate": "POST /evaluate",
        "capacity": "POST /capacity",
        "unregistered_load": "POST /unregistered-load",
        "peak_risk": "POST /peak-risk",
        "docs": "/docs",
        "health": "/health",
    }
