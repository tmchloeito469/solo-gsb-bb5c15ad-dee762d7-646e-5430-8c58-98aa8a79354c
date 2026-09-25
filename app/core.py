"""直流配电树压降校核核心算法。

计算流程：
1. 校验输入构成以 ``source`` 为根的连通树；
2. 自叶向根累计每段承载的全部下游电流；
3. 自根向叶计算各段压降与各节点电压；
4. 汇总超额定电流线段与欠压节点。

``capacity`` 在基线校核通过的前提下，评估目标节点可追加的最大恒定负载电流：
目标到 source 路径上的线段电流同步增加，任一节点的新增压降等于它与目标节点
两条根路径的公共线段电阻之和乘新增电流（兄弟支路经公共上游线段同样受影响）。

``unregistered_load`` 假定恰有一个非 source 节点存在未登记的正恒定负载，
将各测点实测电流与未舍入基线线段电流比较：显著正差（> 1e-6 A）须在 1e-6 A
内相互一致，据此估计负载电流，并按测点是否位于候选节点的祖先路径筛选相容节点。

``peak_risk`` 在至多 K 条负载同时升至峰值（peak_current，缺省等于 current）的
约束下，对每条线段 / 每个节点独立选取使其最坏化的峰值负载组合（峰值增量贡献
降序，贡献相同按负载输入序号升序，零贡献不计入），给出各线段最大承载电流、
各节点最低电压及相应的越限列表；越限判定与 ``evaluate`` 相同。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from typing import Optional, Sequence

ROOT_NODE = "source"
EPS = 1e-9
ROUND_DIGITS = 6
MEASURE_TOL = 1e-6  # 实测差值容差 (A)：|实测 - 基线| ≤ 1e-6 视为无差异


class InputValidationError(ValueError):
    """输入未通过业务校验（无效树、负载引用未知节点等）。"""

    def __init__(self, message: str, details: Optional[list[str]] = None):
        super().__init__(message)
        self.message = message
        self.details = details or []


class BaselineViolationError(ValueError):
    """基线校核已存在超额定电流或欠压，无法评估可增容量。"""

    def __init__(self, message: str, details: Optional[list[str]] = None):
        super().__init__(message)
        self.message = message
        self.details = details or []


@dataclass
class Segment:
    id: str
    parent: str
    child: str
    resistance: float
    rated_current: float


@dataclass
class _Tree:
    """校验后的配电树与基线潮流（电流、电压均为未舍入值）。"""

    segments: list[Segment]
    parent_of: dict[str, str]
    children_map: dict[str, list[Segment]]
    order: list[str]  # 自根向叶 BFS 顺序
    load_map: dict[str, float]
    min_voltage_override: dict[str, float]
    node_total: dict[str, float]
    seg_current: dict[str, float]
    node_voltage: dict[str, float]


def _round(value: float) -> float:
    return round(value, ROUND_DIGITS)


def _truncate(value: float) -> float:
    """向下截断至 6 位小数（先按 1e-9 容差吸附浮点噪声）。"""
    snapped = Decimal(repr(round(value, ROUND_DIGITS + 3)))
    return float(snapped.quantize(Decimal("0.000001"), rounding=ROUND_FLOOR))


def _validate_and_build_tree(segments_in: Sequence) -> tuple[list[Segment], dict[str, str]]:
    """校验树结构，返回线段列表与 child -> parent 映射。

    校验规则：
    - 线段 id 唯一（缺省自动生成 seg-<序号>）；
    - 不存在自环（父节点与子节点相同）；
    - 每个节点至多有一个父节点；
    - 根节点 source 不能作为子节点；
    - 所有父节点必须是 source 或某个子节点（连通性）；
    - 不存在环路。
    """
    segments: list[Segment] = []
    for index, s in enumerate(segments_in):
        seg_id = s.id or f"seg-{index + 1}"
        segments.append(
            Segment(
                id=seg_id,
                parent=s.parent,
                child=s.child,
                resistance=s.resistance,
                rated_current=s.rated_current,
            )
        )

    errors: list[str] = []

    seen_ids: set[str] = set()
    for seg in segments:
        if seg.id in seen_ids:
            errors.append(f"线段 id 重复: '{seg.id}'")
        seen_ids.add(seg.id)

    for seg in segments:
        if seg.parent == seg.child:
            errors.append(f"线段 '{seg.id}' 的父节点与子节点相同（'{seg.parent}'），存在自环")

    parent_of: dict[str, str] = {}
    for seg in segments:
        if seg.child in parent_of:
            errors.append(
                f"节点 '{seg.child}' 存在多个父节点（'{parent_of[seg.child]}' 与 '{seg.parent}'）"
            )
        else:
            parent_of[seg.child] = seg.parent

    if ROOT_NODE in parent_of:
        errors.append(f"根节点 '{ROOT_NODE}' 不能作为子节点")

    child_nodes = set(parent_of)
    for seg in segments:
        if seg.parent != ROOT_NODE and seg.parent not in child_nodes:
            errors.append(f"线段 '{seg.id}' 的父节点 '{seg.parent}' 未在树中定义，树不连通")

    if errors:
        raise InputValidationError(f"输入未构成以 '{ROOT_NODE}' 为根的连通树", errors)

    # 环路检测：沿父指针上溯必须到达根
    for seg in segments:
        visited: set[str] = set()
        node = seg.child
        while node != ROOT_NODE:
            if node in visited:
                raise InputValidationError(
                    f"输入未构成以 '{ROOT_NODE}' 为根的连通树",
                    [f"检测到环路，涉及节点 '{node}'"],
                )
            visited.add(node)
            node = parent_of[node]

    return segments, parent_of


def _aggregate_loads(loads_in: Sequence, known_nodes: set[str]) -> tuple[dict[str, float], dict[str, float]]:
    """汇总各节点负载电流（同节点多条负载累加）及节点级最低允许电压。"""
    errors: list[str] = []
    load_map: dict[str, float] = {}
    min_voltage_map: dict[str, float] = {}

    for load in loads_in:
        if load.node not in known_nodes:
            errors.append(f"负载节点 '{load.node}' 不存在于树中")
            continue
        load_map[load.node] = load_map.get(load.node, 0.0) + load.current
        if load.min_voltage is not None:
            previous = min_voltage_map.get(load.node)
            if previous is not None and abs(previous - load.min_voltage) > EPS:
                errors.append(f"节点 '{load.node}' 的最低允许电压被重复定义且不一致")
            else:
                min_voltage_map[load.node] = load.min_voltage

    if errors:
        raise InputValidationError("负载定义无效", errors)

    return load_map, min_voltage_map


def _prepare_tree(source_voltage: float, segments_in: Sequence, loads_in: Sequence) -> _Tree:
    """校验输入并计算基线潮流（各段电流、各节点电压，未舍入）。"""
    segments, parent_of = _validate_and_build_tree(segments_in)

    children_map: dict[str, list[Segment]] = {}
    for seg in segments:
        children_map.setdefault(seg.parent, []).append(seg)

    nodes = set(parent_of) | {ROOT_NODE}
    load_map, min_voltage_override = _aggregate_loads(loads_in, nodes)

    # 自根向叶 BFS 排序（父节点一定先于子节点出现）
    order = [ROOT_NODE]
    queue: deque[str] = deque([ROOT_NODE])
    while queue:
        node = queue.popleft()
        for seg in children_map.get(node, []):
            order.append(seg.child)
            queue.append(seg.child)

    # 自叶向根累计每段承载的全部下游电流
    node_total = {node: load_map.get(node, 0.0) for node in nodes}
    seg_current: dict[str, float] = {}
    for node in reversed(order):
        for seg in children_map.get(node, []):
            seg_current[seg.id] = node_total[seg.child]
            node_total[node] += node_total[seg.child]

    # 自根向叶计算各节点电压
    node_voltage = {ROOT_NODE: source_voltage}
    for node in order:
        for seg in children_map.get(node, []):
            node_voltage[seg.child] = node_voltage[node] - seg_current[seg.id] * seg.resistance

    return _Tree(
        segments=segments,
        parent_of=parent_of,
        children_map=children_map,
        order=order,
        load_map=load_map,
        min_voltage_override=min_voltage_override,
        node_total=node_total,
        seg_current=seg_current,
        node_voltage=node_voltage,
    )


def _node_threshold(tree: _Tree, node: str, min_voltage: float) -> float:
    """节点生效的最低允许电压（节点级覆盖优先于全局值）。"""
    return tree.min_voltage_override.get(node, min_voltage)


def _find_violations(tree: _Tree, min_voltage: float) -> tuple[list[dict], list[dict]]:
    """按 EPS 容差判定超额定电流线段与欠压节点（恰好触限不算越限）。"""
    over_current_segments = []
    for seg in tree.segments:  # 保持输入顺序
        current = tree.seg_current[seg.id]
        if current > seg.rated_current + EPS:
            over_current_segments.append(
                {
                    "segment_id": seg.id,
                    "parent": seg.parent,
                    "child": seg.child,
                    "current": _round(current),
                    "rated_current": seg.rated_current,
                    "excess": _round(current - seg.rated_current),
                }
            )

    under_voltage_nodes = []
    for node in tree.order:  # BFS 顺序，根在前
        threshold = _node_threshold(tree, node, min_voltage)
        voltage = tree.node_voltage[node]
        if voltage < threshold - EPS:
            under_voltage_nodes.append(
                {
                    "node": node,
                    "voltage": _round(voltage),
                    "min_voltage": threshold,
                    "deficit": _round(threshold - voltage),
                }
            )

    return over_current_segments, under_voltage_nodes


def evaluate(source_voltage: float, min_voltage: float, segments_in: Sequence, loads_in: Sequence) -> dict:
    """执行压降校核，返回可 JSON 序列化的结果字典。"""
    tree = _prepare_tree(source_voltage, segments_in, loads_in)
    over_current_segments, under_voltage_nodes = _find_violations(tree, min_voltage)

    segment_results = []
    for seg in tree.segments:  # 保持输入顺序
        current = tree.seg_current[seg.id]
        segment_results.append(
            {
                "segment_id": seg.id,
                "parent": seg.parent,
                "child": seg.child,
                "current": _round(current),
                "rated_current": seg.rated_current,
                "resistance": seg.resistance,
                "voltage_drop": _round(current * seg.resistance),
                "over_rated": current > seg.rated_current + EPS,
            }
        )

    node_results = []
    for node in tree.order:  # BFS 顺序，根在前
        threshold = _node_threshold(tree, node, min_voltage)
        voltage = tree.node_voltage[node]
        node_results.append(
            {
                "node": node,
                "voltage": _round(voltage),
                "min_voltage": threshold,
                "load_current": _round(tree.load_map.get(node, 0.0)),
                "under_voltage": voltage < threshold - EPS,
            }
        )

    return {
        "ok": not over_current_segments and not under_voltage_nodes,
        "source_voltage": source_voltage,
        "min_voltage": min_voltage,
        "total_source_current": _round(tree.node_total[ROOT_NODE]),
        "segments": segment_results,
        "nodes": node_results,
        "over_current_segments": over_current_segments,
        "under_voltage_nodes": under_voltage_nodes,
    }


def capacity(
    source_voltage: float,
    min_voltage: float,
    segments_in: Sequence,
    loads_in: Sequence,
    target_node: str,
) -> dict:
    """评估目标节点可追加的最大恒定负载电流。

    新增电流 ΔI 仅改变目标到 source 路径上的线段电流；任一节点 n 的新增压降
    = R_common(n, target) × ΔI，R_common 为两条根路径公共线段的电阻之和
    （兄弟支路经公共上游线段同样受压）。最大 ΔI 受限于：
    - 路径上每段的额定电流余量；
    - 每个 R_common > 0 节点的欠压余量（零公共电阻节点不受影响）。
    """
    tree = _prepare_tree(source_voltage, segments_in, loads_in)

    if target_node == ROOT_NODE:
        raise InputValidationError("目标节点无效", [f"目标节点不能为根节点 '{ROOT_NODE}'"])
    if target_node not in tree.parent_of:
        raise InputValidationError("目标节点无效", [f"目标节点 '{target_node}' 不存在于树中"])

    over_current_segments, under_voltage_nodes = _find_violations(tree, min_voltage)
    if over_current_segments or under_voltage_nodes:
        details = [
            f"线段 '{v['segment_id']}' 已超额定电流：{v['current']} A（额定 {v['rated_current']} A）"
            for v in over_current_segments
        ]
        details += [
            f"节点 '{v['node']}' 已欠压：{v['voltage']} V（最低允许 {v['min_voltage']} V）"
            for v in under_voltage_nodes
        ]
        raise BaselineViolationError("基线校核已存在超额定电流或欠压，无法评估可增容量", details)

    # 目标到 source 路径上的线段
    seg_by_child = {seg.child: seg for seg in tree.segments}
    path_seg_ids: set[str] = set()
    node = target_node
    while node != ROOT_NODE:
        seg = seg_by_child[node]
        path_seg_ids.add(seg.id)
        node = seg.parent

    # 各节点与目标节点两条根路径的公共线段电阻之和（自根向叶累计）
    common_resistance = {ROOT_NODE: 0.0}
    for node in tree.order:
        for seg in tree.children_map.get(node, []):
            shared = seg.resistance if seg.id in path_seg_ids else 0.0
            common_resistance[seg.child] = common_resistance[node] + shared

    # 候选上限：(可追加电流, 限制类型, 标识)
    limits: list[tuple[float, str, str]] = []
    for seg in tree.segments:  # 保持输入顺序
        if seg.id not in path_seg_ids:
            continue
        limits.append((seg.rated_current - tree.seg_current[seg.id], "segment", seg.id))
    for node in tree.order:  # BFS 顺序，根在前
        r_common = common_resistance[node]
        if r_common <= 0.0:
            continue  # 零公共电阻：该节点电压不受新增负载影响
        headroom = tree.node_voltage[node] - _node_threshold(tree, node, min_voltage)
        limits.append((headroom / r_common, "node", node))

    raw_max = min(limit for limit, _, _ in limits)
    limiting_factors = [
        {"type": kind, "id": ident}
        for limit, kind, ident in limits
        if limit <= raw_max + EPS
    ]

    return {
        "target_node": target_node,
        "max_additional_current": _truncate(max(0.0, raw_max)),
        "limiting_factors": limiting_factors,
    }


def unregistered_load(
    source_voltage: float,
    segments_in: Sequence,
    loads_in: Sequence,
    measurements_in: Sequence,
) -> dict:
    """定位唯一未登记正恒定负载，返回可 JSON 序列化的结果字典。

    假定恰有一个非 source 节点存在未登记的正恒定负载（测点可不覆盖全树）。
    对每个测点计算 差值 = 实测电流 - 未舍入基线线段电流：

    - 差值 < -1e-6 A（实测低于基线），或显著正差（> 1e-6 A）相互差异超过
      1e-6 A → ``inconsistent``；
    - 无显著正差 → ``undetermined``；
    - 否则负载电流取显著正差的均值（按 6 位小数返回），并筛选候选节点：
      显著正差测点须位于候选节点的祖先路径上，零差测点须不在其祖先路径上；
      未测支路允许多解，无相容节点 → ``inconsistent``。
    """
    tree = _prepare_tree(source_voltage, segments_in, loads_in)

    # 测点校验：线段 id 唯一且存在于树中
    known_seg_ids = {seg.id for seg in tree.segments}
    errors: list[str] = []
    seen_ids: set[str] = set()
    for m in measurements_in:
        if m.segment_id in seen_ids:
            errors.append(f"测量线段 id 重复: '{m.segment_id}'")
        seen_ids.add(m.segment_id)
        if m.segment_id not in known_seg_ids:
            errors.append(f"测量线段 '{m.segment_id}' 不存在于树中")
    if errors:
        raise InputValidationError("实测电流定义无效", errors)

    # 与未舍入基线比较（差值 = 实测 - 基线）
    diffs: list[tuple[str, float]] = []
    measurement_results = []
    for m in measurements_in:
        baseline = tree.seg_current[m.segment_id]
        diff = m.current - baseline
        diffs.append((m.segment_id, diff))
        measurement_results.append(
            {
                "segment_id": m.segment_id,
                "baseline_current": _round(baseline),
                "measured_current": m.current,
                "difference": _round(diff),
            }
        )

    def _result(status: str, load_current: Optional[float], candidates: list[str], reasons: list[str]) -> dict:
        return {
            "status": status,
            "load_current": load_current,
            "candidate_nodes": candidates,
            "measurements": measurement_results,
            "reasons": reasons,
        }

    # 负差：实测低于基线，与“仅新增正负载”的前提矛盾
    negative = [(seg_id, diff) for seg_id, diff in diffs if diff < -MEASURE_TOL]
    if negative:
        reasons = [
            f"线段 '{seg_id}' 实测电流低于基线电流（差值 {_round(diff)} A）"
            for seg_id, diff in negative
        ]
        return _result("inconsistent", None, [], reasons)

    positives = [(seg_id, diff) for seg_id, diff in diffs if diff > MEASURE_TOL]
    if not positives:
        return _result("undetermined", None, [], [])

    # 显著正差须在 1e-6 A 内相互一致
    spread = max(diff for _, diff in positives) - min(diff for _, diff in positives)
    if spread > MEASURE_TOL:
        detail = "、".join(f"线段 '{seg_id}' 为 {_round(diff)} A" for seg_id, diff in positives)
        return _result("inconsistent", None, [], [f"显著正差相互矛盾（{detail}），差异超过 1e-6 A"])

    load_current = sum(diff for _, diff in positives) / len(positives)

    # 候选筛选：显著正差测点须在候选节点祖先路径上，零差测点须不在
    seg_by_child = {seg.child: seg for seg in tree.segments}
    positive_ids = {seg_id for seg_id, _ in positives}
    zero_ids = {seg_id for seg_id, diff in diffs if abs(diff) <= MEASURE_TOL}

    candidates: list[str] = []
    for node in tree.order:  # BFS 顺序，根在前
        if node == ROOT_NODE:
            continue
        path_ids: set[str] = set()
        current = node
        while current != ROOT_NODE:
            seg = seg_by_child[current]
            path_ids.add(seg.id)
            current = seg.parent
        if positive_ids <= path_ids and zero_ids.isdisjoint(path_ids):
            candidates.append(node)

    if not candidates:
        return _result(
            "inconsistent",
            None,
            [],
            ["测点位置相互矛盾：不存在使显著正差测点位于祖先路径且零差测点不在祖先路径的节点"],
        )

    return _result("located", _round(load_current), candidates, [])


def peak_risk(
    source_voltage: float,
    min_voltage: float,
    segments_in: Sequence,
    loads_in: Sequence,
    max_simultaneous_peaks: int,
) -> dict:
    """评估至多 K 条负载同时升至峰值时的最坏越限风险，返回可 JSON 序列化的结果字典。

    每条负载的峰值增量 ΔI_i = peak_current - current（缺省 peak_current = current，
    增量为零）；同节点多条负载分别计数。对每条线段 / 每个节点独立选取使其最坏化的
    至多 K 条峰值负载（贡献降序，贡献相同按负载输入序号升序，零贡献不计入），
    不同约束可对应不同峰值组合：

    - 线段 s：负载 i 位于 s 下游（s 在其根路径上）时贡献 ΔI_i，
      最大承载电流 = 基线电流 + 所选 ΔI_i 之和；
    - 节点 n：负载 i 贡献 ΔI_i × R_common(n, node_i)，R_common 为两条根路径
      公共线段电阻之和，最低电压 = 基线电压 - 所选贡献之和。

    越限判定沿用 ``evaluate`` 的额定电流、节点最低允许电压与 EPS 容差规则。
    """
    tree = _prepare_tree(source_voltage, segments_in, loads_in)

    deltas: list[float] = []
    for load in loads_in:
        peak = load.current if load.peak_current is None else load.peak_current
        deltas.append(peak - load.current)

    k = max(0, max_simultaneous_peaks)

    seg_by_child = {seg.child: seg for seg in tree.segments}

    # 每条负载的根路径线段集合，以及各节点与该负载节点两条根路径的公共电阻
    load_paths: list[set[str]] = []
    load_common_resistance: list[dict[str, float]] = []
    for load in loads_in:
        path_ids: set[str] = set()
        node = load.node
        while node != ROOT_NODE:
            seg = seg_by_child[node]
            path_ids.add(seg.id)
            node = seg.parent
        load_paths.append(path_ids)

        common = {ROOT_NODE: 0.0}
        for parent in tree.order:
            for seg in tree.children_map.get(parent, []):
                shared = seg.resistance if seg.id in path_ids else 0.0
                common[seg.child] = common[parent] + shared
        load_common_resistance.append(common)

    def _top_k(contribs: list[tuple[float, int]]) -> tuple[list[int], float]:
        """取贡献为正的至多 K 条（贡献降序、序号升序），返回升序序号列表与贡献和。"""
        positive = [(c, i) for c, i in contribs if c > 0.0]
        positive.sort(key=lambda item: (-item[0], item[1]))
        chosen = positive[:k]
        return sorted(i for _, i in chosen), sum(c for c, _ in chosen)

    segment_results = []
    over_current_segments = []
    for seg in tree.segments:  # 保持输入顺序
        contribs = [(deltas[i], i) for i, path in enumerate(load_paths) if seg.id in path]
        peak_loads, extra = _top_k(contribs)
        worst = tree.seg_current[seg.id] + extra
        over = worst > seg.rated_current + EPS
        segment_results.append(
            {
                "segment_id": seg.id,
                "parent": seg.parent,
                "child": seg.child,
                "current": _round(worst),
                "rated_current": seg.rated_current,
                "peak_loads": peak_loads,
                "over_rated": over,
            }
        )
        if over:
            over_current_segments.append(
                {
                    "segment_id": seg.id,
                    "parent": seg.parent,
                    "child": seg.child,
                    "current": _round(worst),
                    "rated_current": seg.rated_current,
                    "excess": _round(worst - seg.rated_current),
                    "peak_loads": peak_loads,
                }
            )

    node_results = []
    under_voltage_nodes = []
    for node in tree.order:  # BFS 顺序，根在前
        threshold = _node_threshold(tree, node, min_voltage)
        contribs = [
            (deltas[i] * load_common_resistance[i][node], i)
            for i in range(len(deltas))
        ]
        peak_loads, drop = _top_k(contribs)
        worst = tree.node_voltage[node] - drop
        under = worst < threshold - EPS
        node_results.append(
            {
                "node": node,
                "voltage": _round(worst),
                "min_voltage": threshold,
                "peak_loads": peak_loads,
                "under_voltage": under,
            }
        )
        if under:
            under_voltage_nodes.append(
                {
                    "node": node,
                    "voltage": _round(worst),
                    "min_voltage": threshold,
                    "deficit": _round(threshold - worst),
                    "peak_loads": peak_loads,
                }
            )

    return {
        "ok": not over_current_segments and not under_voltage_nodes,
        "max_simultaneous_peaks": k,
        "segments": segment_results,
        "nodes": node_results,
        "over_current_segments": over_current_segments,
        "under_voltage_nodes": under_voltage_nodes,
    }
