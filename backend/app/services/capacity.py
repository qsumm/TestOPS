"""磁盘容量趋势预测（D234）。

纯数学层：对每个挂载点的 `disk.<mnt>.percent` 时序做趋势拟合，输出斜率
（%/天、GB/天）与到达阈值线的预计日期。**确定性计算，不调 LLM**——
LLM 只在本层结果之上做归因解释（诊断报告的容量小节、告警事件的 AI 摘要）。

方法论（为什么这么做）：
  ① 按"自然日"聚合出日均值——消除 15s/1m 采样密度差异，压掉日内抖动；
  ② 星期哑变量联合最小二乘——斜率与"星期几截距"同时估计，消除备份/批处理
     集中在特定日的周波动（不能先扣星期均值再拟合：有趋势的窗口里偏移会
     吸收趋势分量，单测实测把 1.0%/天 拉低到 0.75）；
  ③ R² 衡量去周期后的线性贴合度；
  ④ ETA = (target − latest) / slope，仅 slope 方向正确且 R² 达标时给出。
     R² 不达标不是"不输出"，而是 confidence 降级为 low（趋势可见但别当真）；
     样本不足（<3 天）才直接 insufficient——刚接入的主机头几天没有预测资格。

数据源与 health_check 同源：ring 明细优先，metric_agg 1m 桶补齐
（14 天窗口几乎全走 agg；ring 只覆盖最近 ~2h）。取数逻辑复制自
health_check._agg_series 而非 import——health_check 也要 import 本模块
（check_disk_forecast），反向依赖会成环。

挂载点命名反向规则与 Agent 侧逐字对称（agent_main.py:160）：
  `/` → disk.root.percent；`/var/log` → disk.var_log.percent。
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from statistics import fmean

from sqlalchemy import text

from app.store import ring

log = logging.getLogger("capacity")

# ── 守卫常量（全部可在测试里逐字复述）──────────────────────────────────
MIN_SAMPLE_DAYS = 3.0   # 历史不足 3 天：insufficient，不拟合不预测
MIN_R2 = 0.6            # R² 低于此值：趋势仍输出但 confidence=low，告警不触发
MAX_ETA_DAYS = 30.0     # ETA 超过 30 天只标 slow（远期日期没有行动价值，是噪声）
CHART_HORIZON_DAYS = 14  # 前端预测虚线画多远
WARN_PCT = 85.0         # 预测参考线（与 FAIL_WARN_THRESHOLDS 的 85 同水位；90 是"快满"）
FAIL_PCT = 90.0

_EXPR_RE = re.compile(r"^\s*(>=|<=|>|<)\s*(\d+(?:\.\d+)?)\s*(?:@\s*(\d+)\s*d)?\s*$")


def parse_forecast_expr(expr: str) -> tuple[str, float, int]:
    """解析预测规则阈值表达式：`>90@7d` → ('>', 90.0, 7)。

    `@Nd` 是预测地平线（预计 N 天内到达即触发），缺省 7 天。
    语法错误抛 ValueError（调用方 catch 后按"永不触发"处理，规则可保存）。
    """
    m = _EXPR_RE.match(expr or "")
    if not m:
        raise ValueError(f"预测阈值表达式不合法：{expr!r}（示例：>90@7d）")
    horizon = int(m.group(3)) if m.group(3) else 7
    if horizon <= 0 or horizon > 365:
        raise ValueError(f"预测地平线应在 1~365 天之间：{expr!r}")
    return m.group(1), float(m.group(2)), horizon


def _ols_slope_intercept(X: list[list[float]], y: list[float]) -> tuple[float, float]:
    """最小二乘解正规方程（XᵀX·β = Xᵀy），返回 (斜率, 截距)。

    D235：纯 Python 实现，替代 sklearn/numpy（省 90MB 依赖 → 开源构建快一个数量级）。
    设计要点：
      · 部分主元（partial pivoting）——正规方程本身条件数偏大（天序号列与星期哑变量
        有相关性），不做主元选取容易出 NaN；
      · 奇异矩阵（样本少于参数个数等）→ 退化为无哑变量的简单 OLS，而不是抛异常
        （预测是"锦上添花"，不能因矩阵病态把整个检查项带崩）；
      · 只取我们需要的两列：常数项截距与天序号斜率。
    """
    n, k = len(X), len(X[0]) if X else 0
    if n < k:                      # 样本不足以估满参数 → 去掉哑变量的简单 OLS
        X = [[1.0, float(i)] for i in range(n)]
        k = 2
    # XᵀX 与 Xᵀy
    ata = [[sum(row[a] * row[b] for row in X) for b in range(k)] for a in range(k)]
    aty = [sum(row[a] * yy for row, yy in zip(X, y)) for a in range(k)]
    # 高斯消元（部分主元）
    for col in range(k):
        pivot = max(range(col, k), key=lambda r: abs(ata[r][col]))
        if abs(ata[pivot][col]) < 1e-12:
            return _simple_ols(X, y)      # 奇异 → 退化
        ata[col], ata[pivot] = ata[pivot], ata[col]
        aty[col], aty[pivot] = aty[pivot], aty[col]
        pv = ata[col][col]
        for r in range(col + 1, k):
            factor = ata[r][col] / pv
            if factor == 0:
                continue
            for c in range(col, k):
                ata[r][c] -= factor * ata[col][c]
            aty[r] -= factor * aty[col]
    beta = [0.0] * k
    for row in range(k - 1, -1, -1):
        s = aty[row] - sum(ata[row][c] * beta[c] for c in range(row + 1, k))
        beta[row] = s / ata[row][row] if abs(ata[row][row]) > 1e-12 else 0.0
    return beta[1], beta[0]          # (斜率, 截距)


def _simple_ols(X: list[list[float]], y: list[float]) -> tuple[float, float]:
    """退化路径：无哑变量的一元线性回归（斜率, 截距）。"""
    t = [row[1] for row in X]
    tbar, ybar = fmean(t), fmean(y)
    sxx = sum((v - tbar) ** 2 for v in t)
    sxy = sum((v - tbar) * (yy - ybar) for v, yy in zip(t, y))
    slope = sxy / sxx if sxx else 0.0
    return slope, ybar - slope * tbar


def fit_trend(points: list[tuple[datetime, float]]) -> dict:
    """对 (ts, value) 序列做星期去周期线性拟合。

    返回 {"ok", "reason", "slope_per_day", "r2", "n_days", "span_days",
          "daily": [(iso_date, mean), ...]}。ok=False 时 reason 为
    "no_data"（点太少）或 "insufficient"（跨度 < MIN_SAMPLE_DAYS）。
    slope 单位是 %/天（对 percent 序列）；负值表示使用率在下降。
    """
    pts = sorted((ts, float(v)) for ts, v in points if v is not None)
    if len(pts) < 2:
        return {"ok": False, "reason": "no_data"}
    span_days = (pts[-1][0] - pts[0][0]).total_seconds() / 86400.0
    if span_days < MIN_SAMPLE_DAYS:
        return {"ok": False, "reason": "insufficient", "span_days": round(span_days, 2)}

    # ① 日均值（同一自然日的点取均值——不同主机采样间隔不一，日粒度对齐）
    by_day: dict[date, list[float]] = defaultdict(list)
    for ts, v in pts:
        by_day[ts.date()].append(v)
    days = sorted(by_day)
    means = [fmean(by_day[d]) for d in days]

    # ②③ 联合最小二乘：斜率与星期截距**同时**估计。
    # 为什么不能"先扣星期均值再拟合"：有趋势的窗口里，星期均值本身吸收趋势分量
    # （14 天窗口每个星期几恰好一早一晚各出现一次，偏移里混着 (d−3) 的趋势项），
    # 两步法会把真实斜率拉低 ~25%（D234 单测实测 1.0 → 0.75）。哑变量回归一次到位。
    #
    # D235：**纯 Python 实现**（正规方程 + 部分主元高斯消元，7 列：常数/天序号/周1~6）。
    # 为什么不用 sklearn：scikit-learn 会拖进 numpy+scipy ≈ 90MB —— 开源部署实测在
    # 慢镜像源上光这一步就要十几分钟（D235 起因）。本回归只有几十行数据，纯 Python
    # 毫秒级且零依赖；sklearn 只留给「孤立森林」算法（缺失时自动降级 3σ，见 detect.py）。
    t = list(range(len(means)))                      # 天序号（日均值等间距）
    wk = [d.weekday() for d in days]
    X = [[1.0, float(i), *(1.0 if w == k else 0.0 for k in range(6))]  # 周日为基准组
         for i, w in zip(t, wk)]
    slope, intercept = _ols_slope_intercept(X, means)
    ybar = fmean(means)
    ss_res = sum((y - (intercept + slope * i)) ** 2 for i, y in zip(t, means))
    ss_tot = sum((y - ybar) ** 2 for y in means)
    r2 = (1 - ss_res / ss_tot) if ss_tot > 1e-9 else 1.0

    return {"ok": True, "slope_per_day": round(slope, 4), "r2": round(r2, 3),
            "n_days": len(means), "span_days": round(span_days, 2),
            "daily": [(d.isoformat(), round(m, 2)) for d, m in zip(days, means)]}


def days_to_threshold(latest: float, slope_per_day: float, target: float,
                      direction: str = "up") -> float | None:
    """预计多少天后到达 target。direction 与阈值表达式的比较符对应：
    'up'（>/>=，涨到线以上）/ 'down'（</<=，降到线以下）。

    已在阈值这一侧（上行且 latest≥target，或下行且 latest≤target）→ 0.0
    （"现在就在线上"，让告警地平线判定自然命中，语义同 threshold 规则）；
    斜率方向与预测方向相反（如上行规则但磁盘在降）→ None（永不触发）。
    """
    going_up = direction != "down"
    if going_up:
        if slope_per_day <= 1e-9:
            return None
        if latest >= target:
            return 0.0
        days = (target - latest) / slope_per_day
    else:
        if slope_per_day >= -1e-9:
            return None
        if latest <= target:
            return 0.0
        days = (target - latest) / slope_per_day   # 负 / 负 → 正
    return max(days, 0.0)


def _mount_of(metric: str) -> str:
    """disk.var_log.percent → /var/log（与 agent_main.py:160 的命名逐字互逆）。"""
    key = metric[len("disk."):-len(".percent")] if metric.startswith("disk.") else metric
    return "/" if key == "root" else "/" + key.replace("_", "/")


async def _agg_series(db, ident: str, metric: str, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
    """metric_agg 1m 桶（与 health_check._agg_series 同口径，另加 granularity 过滤：
    1m/5m 双粒度并存，不过滤会同一时刻取到两行、重复计入拟合）。
    注意：granularity 是 ENUM 列，seekdb/OceanBase 不接受 `granularity='1m'`
    的字符串绑定比较（(1210,'Invalid argument')，D234 集成测试抓到）——
    用 SUBSTR 比较，不依赖 ENUM 序数位置。"""
    rows = (await db.execute(text(
        "SELECT ts, value_avg FROM metric_agg WHERE ident=:i AND metric=:m "
        "AND SUBSTR(granularity, 1, 2) = '1m' AND ts BETWEEN :s AND :e ORDER BY ts"),
        {"i": ident, "m": metric, "s": start, "e": end})).fetchall()
    return [(r[0], float(r[1] or 0)) for r in rows]


def _hourly(pts: list[tuple[datetime, float]]) -> list[tuple[str, float]]:
    """历史曲线降采样：按小时取均值（14 天 × 24 = 336 点，前端一条序列足够顺滑）。"""
    by_h: dict[datetime, list[float]] = defaultdict(list)
    for ts, v in pts:
        by_h[ts.replace(minute=0, second=0, microsecond=0)].append(v)
    return [(ts.isoformat(), round(fmean(vs), 2)) for ts, vs in sorted(by_h.items())]


def _extrapolate(latest_ts: datetime, latest: float, slope: float,
                 horizon_days: int = CHART_HORIZON_DAYS) -> list[tuple[str, float]]:
    """预测虚线：从最新点沿斜率按小时外推；超过 100% 截断（磁盘不会超过满）。"""
    out = [(latest_ts.isoformat(), round(latest, 2))]
    if abs(slope) < 1e-9:
        return out
    step_h = 1.0
    per_step = slope * step_h / 24.0
    cur_v, cur_t = latest, latest_ts
    for _ in range(int(horizon_days * 24)):
        cur_v += per_step
        if cur_v >= 100.0:
            break
        cur_t = cur_t + timedelta(hours=step_h)
        out.append((cur_t.isoformat(), round(cur_v, 2)))
    return out


def _eta_payload(days: float | None) -> dict | None:
    """统一 ETA 形态：{"days", "date"}；> MAX_ETA_DAYS 只给 days 不给 date（slow）。"""
    if days is None:
        return None
    eta = {"days": round(days, 1), "date": None}
    if days <= MAX_ETA_DAYS:
        eta["date"] = (date.today() + timedelta(days=int(days + 0.999))).isoformat()
    return eta


# 伪文件系统 fstype 黑名单（与 ui.js REAL_FS_SKIP 同源）——容器/mock agent 里
# psutil.disk_partitions(all=True) 会把 /etc/hostname 之类的宿主 bind mount 也
# 当成"分区"上报（D234 部署验证抓出），不滤会把预测与告警灌满噪音。
_REAL_FS_SKIP = re.compile(
    r"^(tmpfs|devtmpfs|overlay|proc|sysfs|devpts|cgroup\d?|squashfs|ramfs|debugfs|tracefs"
    r"|securityfs|pstore|mqueue|autofs|configfs|fusectl|binfmt_misc|efivarfs|hugetlbfs)$")
_REAL_MOUNT_SKIP = ("/run", "/dev/shm", "/sys", "/proc", "/dev")


def _is_disk_pct_metric(m: str) -> bool:
    """分区使用率序列（disk.<mnt>.percent），排除：
    · io/inode 前缀（与 health_check._disk_mount_series 同口径）；
    · `disk.percent` 根分区**标量**（agent_main.py:158 的保守聚合值，不带挂载点段）——
      它会被 _mount_of 反解成 "/"，与 disk.root.percent 重复成两行。"""
    if not (m.startswith("disk.") and m.endswith(".percent")):
        return False
    if m.startswith(("disk.io.", "disk.inode.")):
        return False
    return bool(m[len("disk."):-len(".percent")])   # "disk.percent" key 为空 → 标量


# 伪挂载路径黑名单（**不依赖 ring 快照**的静态口径）：容器/mock agent 里
# psutil.disk_partitions(all=True) 会把宿主 bind mount（/etc/hostname、/etc/hosts…）
# 当成分区上报——它们全是 /etc 下的文件挂载，路径形态一眼可辨。
# 为什么必须静态：ring 是**进程内存**，平台重启即清空；告警引擎在启动瞬间就评估
# （窗口 14 天，agg 有历史），那时快照还没到 → 靠快照过滤会漏，伪分区照样成事件
# （真机实测：AI 建议里出现"根分区/etc/hostname 占用率…"）。
_PSEUDO_MOUNT_PREFIX = ("/etc", "/proc", "/sys", "/dev", "/run", "/snap", "/var/lib/docker/overlay")


def is_pseudo_mount_path(mount: str) -> bool:
    """静态判定伪挂载点（路径口径，不依赖任何内存态快照）。"""
    m = str(mount or "")
    if not m:
        return True
    return any(m == p or m.startswith(p + "/") for p in _PSEUDO_MOUNT_PREFIX)


def _real_mounts(ident: str) -> set[str] | None:
    """真实分区集合（按 ring 最新快照的 fstype/伪路径过滤，realDisks 口径 + /etc 兜底）。
    快照缺失（老 agent / 刚重启未上报）返回 None = 不过滤（放行，宁多勿漏）。"""
    parts = ring.get_disks(ident)
    if not parts:
        return None
    out = set()
    for p in parts:
        fstype = str(p.get("fstype") or "").lower()
        m = str(p.get("mount") or "")
        if fstype == "overlay" and m == "/":
            pass    # 容器化 agent（mock/容器内部署）的根分区就是 overlay——放行，
                    # 否则演示与容器场景整台主机没有可预测分区（D234 部署验证抓出）
        elif _REAL_FS_SKIP.match(fstype):
            continue
        if m in _REAL_MOUNT_SKIP or m.startswith("/etc"):
            continue
        if is_pseudo_mount_path(m):
            continue
        if m:
            out.add(m)
    return out


async def _disk_pct_names(db, ident: str) -> list[str]:
    """分区指标名发现：ring 内存 ∪ metric_agg 落库（D234 集成测试抓出的缺陷——
    Agent 断连超 2h 后 ring 明细全部过期，只看 ring 会漏掉整台主机）。"""
    names = {m for m in ring.list_metric_names(ident, "disk.") if _is_disk_pct_metric(m)}
    # 注意：LIKE 的 '%' 会被 text() 转义成 '%%' 发给服务端，OceanBase 直接报
    # (1210, 'Invalid argument')——通配匹配用 LEFT/RIGHT 表达，模式过滤回 Python 侧做。
    rows = (await db.execute(text(
        "SELECT DISTINCT metric FROM metric_agg WHERE ident=:i "
        "AND SUBSTR(granularity, 1, 2) = '1m' "
        "AND LEFT(metric, 5) = 'disk.' AND RIGHT(metric, 8) = '.percent'"),
        {"i": ident})).fetchall()
    names.update(r[0] for r in rows if _is_disk_pct_metric(r[0]))
    return sorted(names)


async def forecast_mounts(db, ident: str, days: int = 14) -> list[dict]:
    """主机全部真实分区的容量预测（前端卡片 / 诊断报告共用一个口径）。

    返回按"紧急度"排序的列表：有 ETA 的在前（到 90% 天数升序），
    其后按斜率降序（涨得最快的先看）。每项含 history/forecast 曲线。
    """
    now = datetime.utcnow()
    start = now - timedelta(days=days)
    names = await _disk_pct_names(db, ident)
    # ① 静态路径口径先过滤（**不依赖任何内存态**）：容器/mock agent 会把宿主 bind mount
    #    （/etc/hostname、/etc/hosts…）上报成 disk.<mnt>.percent——这些不是分区，
    #    预测与告警都不该看它们。必须在最前面做：平台重启后 ring 快照要等 agent 心跳
    #    （60s）才到，那之前若只靠快照过滤，伪分区会短暂冒出来（真机实测复现）。
    names = [m for m in names if not is_pseudo_mount_path(_mount_of(m))]
    # ② 快照口径再过滤（fstype/真实分区集合）；快照缺失（None）则不过滤
    real = _real_mounts(ident)
    if real is not None:
        names = [m for m in names if _mount_of(m) in real]
    if not names:
        return []
    disks = {d.get("mount", ""): d for d in ring.get_disks(ident)}

    out = []
    for m in names:
        pts = ring.query_points(ident, m, start, now)          # ring 只覆盖 ~2h
        # 历史补齐：**无条件**查 agg（14 天窗口下 ring 几乎总不够）。不能依赖
        # ring.has_points_before——平台/容器重启后 ring 只剩心跳新点，历史全在
        # agg 里，条件判断会漏掉整段历史（D234 部署验证抓出）。
        # 量级：1m 桶 14 天 ≈ 2 万行/序列，单查询 <100ms，拟合与降采样均可承受。
        cut = pts[0][0] if pts else now
        older = await _agg_series(db, ident, m, start, cut)
        pts = older + pts
        if not pts:
            continue
        latest_ts, latest = pts[-1][0], pts[-1][1]
        fit = fit_trend(pts)
        size_gb = None
        d = disks.get(_mount_of(m))
        if d and d.get("size"):
            size_gb = round(int(d["size"]) / (1024 ** 3), 1)

        item = {"metric": m, "mount": _mount_of(m), "latest_pct": round(latest, 1),
                "size_gb": size_gb, "sample_days": fit.get("span_days", 0.0),
                "n_days": fit.get("n_days", 0),
                "slope_pct_day": fit.get("slope_per_day"),
                "slope_gb_day": (round(fit["slope_per_day"] / 100.0 * size_gb, 2)
                                 if fit.get("ok") and size_gb else None),
                "r2": fit.get("r2"), "confidence": "insufficient",
                "eta_85": None, "eta_90": None,
                "history": _hourly(pts), "forecast": []}
        if fit.get("ok"):
            slope = fit["slope_per_day"]
            item["confidence"] = "high" if fit["r2"] >= MIN_R2 else "low"
            # 容量预测参考线只有"涨到线上"一个方向（磁盘只怕满不怕空）
            item["eta_85"] = _eta_payload(days_to_threshold(latest, slope, WARN_PCT, "up"))
            item["eta_90"] = _eta_payload(days_to_threshold(latest, slope, FAIL_PCT, "up"))
            item["forecast"] = _extrapolate(latest_ts, latest, slope)
        out.append(item)

    # 紧急度排序：有 90% ETA 的按天数升序在前；无 ETA 的按斜率降序
    def _urgency(it):
        e = it.get("eta_90")
        if e and e.get("days") is not None:
            return (0, e["days"])
        return (1, -(it.get("slope_pct_day") or 0))
    out.sort(key=_urgency)
    return out
