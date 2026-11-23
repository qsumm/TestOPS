"""检测算法（FR-C3；03 §1.7）：threshold / 3sigma / isolation_forest（降级 3sigma）/ forecast（D234）。"""
import logging
import re
from statistics import mean, stdev

log = logging.getLogger("detect")

_OPS = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: abs(a - b) < 1e-9,
    "!=": lambda a, b: abs(a - b) >= 1e-9,
}

# D234：预测阈值表达式允许 @Nd 地平线后缀（'>90@7d'）。普通阈值表达式不带后缀，
# 两条消费路径（本模块与 chat_suggest 的阈值速览）都必须能解析，故并入主正则。
_EXPR_RE = re.compile(r"^\s*(>=|<=|==|!=|>|<)\s*([-\d.eE+]+)\s*(?:@\s*\d+\s*d)?\s*$")


def _parse_expr(expr: str) -> tuple[str, float]:
    m = _EXPR_RE.match(expr or "")
    if not m:
        raise ValueError(f"表达式格式错误: {expr!r}（示例 '>90'）")
    return m.group(1), float(m.group(2))


def detect(algorithm: str, expr: str, points: list[tuple]) -> list[tuple[dict, float]]:
    """输入 [(ts, value)]，返回异常点 [(labels={}, value)]——labels 由调用方补 host/metric。"""
    if not points:
        return []
    if algorithm == "3sigma" or (algorithm == "isolation_forest" and not _sklearn_ok()):
        vals = [v for _, v in points]
        if len(vals) < 5:
            return []
        mu, sg = mean(vals), (stdev(vals) if len(vals) > 1 else 0.0)
        if sg == 0:
            return []
        return [({}, v) for _, v in points if abs(v - mu) > 3 * sg]
    if algorithm == "isolation_forest":
        from sklearn.ensemble import IsolationForest

        vals = [[v] for _, v in points]
        model = IsolationForest(n_estimators=50, contamination=0.05).fit(vals)
        return [({}, points[i][1]) for i, p in enumerate(model.predict(vals)) if p == -1]
    if algorithm == "forecast":
        return _detect_forecast(expr, points)   # D234：容量趋势预测（见函数注释）
    # threshold
    op, thr = _parse_expr(expr)
    fn = _OPS[op]
    return [({}, v) for _, v in points if fn(v, thr)]


def _detect_forecast(expr: str, points: list[tuple]) -> list[tuple[dict, float]]:
    """D234 容量趋势预测：拟合历史斜率，预计 horizon 天内到达阈值即触发。

    expr 形如 '>90@7d'（预计 7 天内涨破 90%）。与逐点算法不同，forecast 是
    **整段序列一个判定**：触发时返回 [({}, eta_days)]——value 是"预计天数"
    （事件/通知里可读作"预计 N 天后到达"），不是某个异常点的值。

    守卫（宁缺毋滥，预测告警最怕狼来了）：
      · 样本 <3 天 → 不判（insufficient）；
      · R² < 0.6 → 不判（趋势不可信）；
      · 斜率方向与比较符不符（'>90' 但磁盘在降）→ 不判；
      · 语法错 → 不判（规则可保存，评估时静默跳过并记日志）。
    """
    from app.services.capacity import (
        MIN_R2,
        days_to_threshold,
        fit_trend,
        parse_forecast_expr,
    )

    try:
        op, thr, horizon = parse_forecast_expr(expr)
    except ValueError as e:
        log.warning("forecast 规则表达式不合法，本轮跳过：%s", e)
        return []
    fit = fit_trend(points)
    if not fit.get("ok") or fit["r2"] < MIN_R2:
        return []
    slope = fit["slope_per_day"]
    direction = "up" if op in (">", ">=") else "down"
    eta = days_to_threshold(points[-1][1], slope, thr, direction)
    if eta is None or eta > horizon:
        return []
    return [({}, round(eta, 2))]


def detect_labeled(algorithm: str, expr: str, points: list[tuple[dict, float]]) -> list[tuple[dict, float]]:
    """带标签的检测（轨 B / Prom 专用）：points = [(labels, value)]，返回保留 labels 的异常点。

    与 detect() 的区别：detect() 丢弃 labels（轨 A 由调用方按 host 补齐），
    而 Prom 一个查询可返回多条序列（不同 instance），必须回指原序列标签，否则无法定位主机。
    """
    if not points:
        return []
    if algorithm == "forecast":
        # D234：预测只支持指标轨（scope=metric）——Prom 序列的生命周期与标签集
        # 都不适合做长期趋势拟合，误配时静默跳过而不是拿 threshold 语义硬算。
        log.warning("forecast 算法仅支持 scope=metric，prom 规则本轮跳过")
        return []
    if algorithm in ("3sigma",) or (algorithm == "isolation_forest" and not _sklearn_ok()):
        vals = [v for _, v in points]
        if len(vals) < 5:
            return []
        mu, sg = mean(vals), (stdev(vals) if len(vals) > 1 else 0.0)
        if sg == 0:
            return []
        return [(lb, v) for lb, v in points if abs(v - mu) > 3 * sg]
    if algorithm == "isolation_forest":
        from sklearn.ensemble import IsolationForest

        vals = [[v] for _, v in points]
        model = IsolationForest(n_estimators=50, contamination=0.05).fit(vals)
        return [points[i] for i, p in enumerate(model.predict(vals)) if p == -1]
    op, thr = _parse_expr(expr)
    fn = _OPS[op]
    return [(lb, v) for lb, v in points if fn(v, thr)]


def _sklearn_ok() -> bool:
    try:
        import sklearn  # noqa: F401

        return True
    except ImportError:
        return False


def algo_available(algorithm: str) -> str:
    """返回实际生效算法（隔离森林不可用时降级）。"""
    if algorithm == "isolation_forest" and not _sklearn_ok():
        return "3sigma"
    return algorithm
