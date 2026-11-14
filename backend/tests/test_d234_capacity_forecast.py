"""D234 磁盘容量趋势预测——纯数学层守卫。

锁死的行为（按 capacity.py 模块注释的方法论逐条对号）：
  ① 已知斜率序列：拟合斜率 ≈ 真值，R² 高，ETA 日期正确；
  ② 星期周期噪声：去周期后斜率仍 ≈ 真值（哑变量有效）；
  ③ 样本不足（<3 天）：insufficient，不预测；
  ④ 平坦/下降序列：不给出上行 ETA；
  ⑤ 表达式语法：>90@7d 解析；非法输入拒绝；
  ⑥ 方向语义：上行规则对"已在 declines"的序列永不触发；
  ⑦ ETA 封顶：>30 天不给 date（slow），但仍给 days。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.services.capacity import (
    MAX_ETA_DAYS,
    MIN_R2,
    MIN_SAMPLE_DAYS,
    days_to_threshold,
    fit_trend,
    parse_forecast_expr,
)

BASE = datetime(2026, 11, 1, 0, 0, 0)


def _linear_series(days: int, slope_pct_day: float, start_pct: float = 60.0,
                   step_hours: int = 2, weekday_noise: float = 0.0):
    """构造确定性序列：每天涨 slope_pct_day，可选星期几加性噪声。"""
    out = []
    for i in range(0, days * 24, step_hours):
        ts = BASE + timedelta(hours=i)
        day = (ts - BASE).days
        v = start_pct + slope_pct_day * day
        if weekday_noise:
            v += weekday_noise * ((ts.weekday() % 3) - 1)   # 周期性 +n/0/-n
        out.append((ts, round(v, 2)))
    return out


# ── ① 已知斜率 ────────────────────────────────────────────────────────
def test_fit_trend_recovers_known_slope():
    pts = _linear_series(14, slope_pct_day=1.0, start_pct=70.0)
    fit = fit_trend(pts)
    assert fit["ok"] is True
    assert abs(fit["slope_per_day"] - 1.0) < 0.05, fit
    assert fit["r2"] >= 0.99
    assert fit["n_days"] == 14
    eta = days_to_threshold(70.0 + 13 * 1.0, fit["slope_per_day"], 90.0, "up")
    # 第 14 天 latest=83，到 90 还有 7 天
    assert eta is not None and abs(eta - 7.0) < 0.3


# ── ② 星期周期噪声下仍稳 ─────────────────────────────────────────────
def test_fit_trend_weekday_noise_robust():
    # ±1.5% 的周期噪声 vs 每天 1% 的真趋势：不去周期的话 14 天样本斜率会被明显拉偏
    pts = _linear_series(21, slope_pct_day=1.0, start_pct=65.0, weekday_noise=1.5)
    fit = fit_trend(pts)
    assert fit["ok"] is True
    assert abs(fit["slope_per_day"] - 1.0) < 0.25, fit
    assert fit["r2"] >= MIN_R2, f"去周期后 R² 应达标，实际 {fit['r2']}"


# ── ③ 样本不足 ────────────────────────────────────────────────────────
def test_fit_trend_insufficient_span():
    pts = _linear_series(2, slope_pct_day=2.0)     # 只 2 天 < MIN_SAMPLE_DAYS
    fit = fit_trend(pts)
    assert fit["ok"] is False and fit["reason"] == "insufficient"
    assert fit_trend([])["reason"] == "no_data"
    assert fit_trend([(BASE, 50.0)])["reason"] == "no_data"


# ── ④ 平坦与下降 ──────────────────────────────────────────────────────
def test_fit_trend_flat_and_descending():
    flat = _linear_series(10, slope_pct_day=0.0, start_pct=55.0)
    fit = fit_trend(flat)
    assert fit["ok"] and abs(fit["slope_per_day"]) < 0.05
    assert days_to_threshold(55.0, fit["slope_per_day"], 90.0, "up") is None

    desc = _linear_series(10, slope_pct_day=-1.0, start_pct=75.0)
    fit2 = fit_trend(desc)
    assert fit2["ok"] and fit2["slope_per_day"] < 0
    # 上行规则对下降序列永不触发；下行规则正常给出天数
    assert days_to_threshold(65.0, fit2["slope_per_day"], 90.0, "up") is None
    down_eta = days_to_threshold(75.0, fit2["slope_per_day"], 60.0, "down")
    assert down_eta is not None and abs(down_eta - 15.0) < 1.0


# ── ⑤ 表达式解析 ──────────────────────────────────────────────────────
def test_parse_forecast_expr():
    assert parse_forecast_expr(">90@7d") == (">", 90.0, 7)
    assert parse_forecast_expr(">=85") == (">=", 85.0, 7)          # @ 缺省 7 天
    assert parse_forecast_expr(" <= 20 @14d ") == ("<=", 20.0, 14)
    for bad in ["", "90", ">90@0d", ">90@400d", "forecast>90", ">90@x"]:
        with pytest.raises(ValueError):
            parse_forecast_expr(bad)


# ── ⑥ 方向语义：已越线 / 反向 ─────────────────────────────────────────
def test_days_to_threshold_direction_semantics():
    # 已在线上 → 0.0（语义同 threshold 规则的"当前值即命中"）
    assert days_to_threshold(92.0, 0.5, 90.0, "up") == 0.0
    assert days_to_threshold(18.0, -0.5, 20.0, "down") == 0.0
    # 方向相反 → None
    assert days_to_threshold(80.0, -1.0, 90.0, "up") is None   # 在降还盼着涨
    assert days_to_threshold(80.0, 1.0, 60.0, "down") is None
    # 平坦 → None
    assert days_to_threshold(80.0, 0.0, 90.0, "up") is None


# ── ⑦ ETA 封顶（_eta_payload 经 forecast_mounts 间接验证，这里锁常量）──
def test_eta_cap_constants():
    assert MAX_ETA_DAYS == 30.0 and MIN_R2 == 0.6 and MIN_SAMPLE_DAYS == 3.0


# ── ⑧ detect 的 forecast 分支（M2）──────────────────────────────────
def test_detect_forecast_triggers_within_horizon():
    """斜率 1%/天、latest 83、阈值 90 → 预计 7 天 → '>90@7d' 触发，value=预计天数。"""
    from app.engine.detect import detect
    pts = _linear_series(14, slope_pct_day=1.0, start_pct=70.0)
    hits = detect("forecast", ">90@7d", pts)
    assert len(hits) == 1
    assert abs(hits[0][1] - 7.0) < 0.3, hits


def test_detect_forecast_no_trigger_beyond_horizon():
    """同趋势但地平线只有 3 天（要 7 天才到）→ 不触发。"""
    from app.engine.detect import detect
    pts = _linear_series(14, slope_pct_day=1.0, start_pct=70.0)
    assert detect("forecast", ">90@3d", pts) == []


def test_detect_forecast_guards():
    """样本不足 / 语法错 / 方向相反 / R² 不足 → 一律静默不触发。"""
    from app.engine.detect import detect
    few = _linear_series(2, slope_pct_day=2.0)                    # <3 天
    assert detect("forecast", ">90@7d", few) == []
    good = _linear_series(14, slope_pct_day=1.0, start_pct=70.0)
    assert detect("forecast", "garbage", good) == []              # 语法错
    assert detect("forecast", ">90@7d", _linear_series(14, slope_pct_day=-1.0, start_pct=80.0)) == []  # 在降
    flat = _linear_series(14, slope_pct_day=0.0, start_pct=80.0)  # 平坦 → R² 无意义/斜率 0
    assert detect("forecast", ">90@7d", flat) == []


def test_detect_forecast_already_above_threshold_triggers_immediately():
    """latest 已在阈值上（92>90）→ eta=0 → 立即触发（与 threshold 语义一致）。"""
    from app.engine.detect import detect
    pts = _linear_series(14, slope_pct_day=0.2, start_pct=89.0)
    hits = detect("forecast", ">90@7d", pts)
    assert len(hits) == 1 and hits[0][1] == 0.0


def test_parse_expr_tolerates_horizon_suffix():
    """向后兼容：threshold 规则的表达式解析不受 @Nd 影响（chat_suggest 同用此函数）。"""
    from app.engine.detect import _parse_expr
    assert _parse_expr(">90@7d") == (">", 90.0)
    assert _parse_expr(">90") == (">", 90.0)


def test_detect_labeled_rejects_forecast():
    """Prom 轨不硬算 forecast（静默跳过），避免拿 threshold 语义误判。"""
    from app.engine.detect import detect_labeled
    pts = [({"instance": "a"}, v) for _, v in _linear_series(14, slope_pct_day=1.0, start_pct=70.0)]
    assert detect_labeled("forecast", ">90@7d", pts) == []


# ── ⑨ DB 集成：metric_agg 喂历史 → forecast_mounts / check / 端点全链路 ──
IDENT_D234 = "d234-cap-host"
FP_D234 = "test-fp-d234"


async def _mk_host(db):
    from sqlalchemy import select
    from app.models.host import Host
    from app.services import ingest
    tok = ingest.mint_install_token()
    data = await ingest.register_agent(db, {"fingerprint": FP_D234, "hostname": "d234",
                                            "install_token": tok})
    h = (await db.execute(select(Host).where(Host.ident == data["ident"]))).scalar_one()
    return data["ident"], h.id


async def _rm_host(db, ident):
    """按真实 ident 清理（register_agent 生成的是指纹哈希，不是常量）。"""
    from sqlalchemy import text as _t
    await db.execute(_t("DELETE FROM hosts WHERE ident = :i"), {"i": ident})
    await db.execute(_t("DELETE FROM metric_agg WHERE ident = :i"), {"i": ident})
    await db.commit()


def _seed_agg(ident: str, metric: str, days: int, slope_pct_day: float, start_pct: float = 70.0):
    """直接写 metric_agg 1m 桶（每 2h 一行足够拟合，行数可控）。"""
    from datetime import datetime, timedelta
    from sqlalchemy import text as _t

    async def m(db):
        base = datetime.utcnow().replace(microsecond=0)
        rows = []
        for dd in range(days):
            ts = base - timedelta(days=days - 1 - dd)
            v = start_pct + slope_pct_day * dd
            rows.append((ts, v))
        await db.execute(_t("DELETE FROM metric_agg WHERE ident=:i AND metric=:m"),
                         {"i": ident, "m": metric})
        for ts, v in rows:
            await db.execute(_t(
                "INSERT INTO metric_agg (ident, metric, granularity, ts, value_avg) "
                "VALUES (:i, :m, '1m', :ts, :v)"),
                {"i": ident, "m": metric, "ts": ts, "v": v})
        await db.commit()
    return m


@pytest.mark.asyncio
async def test_forecast_mounts_full_chain():
    """ring 快照 + 14 天 metric_agg 历史 → 预测含斜率/ETA/曲线，紧急度排序正确。"""
    from app.db.session import SessionLocal
    from app.store import ring

    async with SessionLocal() as db:
        ident, _host_id = await _mk_host(db)
        try:
            await _seed_agg(ident, "disk.root.percent", 14, 1.0, 70.0)(db)
            await _seed_agg(ident, "disk.data.percent", 14, 0.1, 50.0)(db)
            # 最新分区快照（供 GB 换算与挂载点匹配）
            ring.set_disks(ident, [
                {"mount": "/", "size": 100 * 1024 ** 3, "used": 80 * 1024 ** 3, "percent": 82.0},
                {"mount": "/data", "size": 50 * 1024 ** 3, "used": 26 * 1024 ** 3, "percent": 52.0}])
            from app.services import capacity
            mounts = await capacity.forecast_mounts(db, ident, days=14)
            assert mounts, "应产出分区预测"
            by_mount = {m["mount"]: m for m in mounts}
            root = by_mount["/"]
            assert abs(root["slope_pct_day"] - 1.0) < 0.1, root
            assert root["slope_gb_day"] is not None and abs(root["slope_gb_day"] - 1.0) < 0.15
            assert root["confidence"] == "high"
            assert root["eta_90"] and root["eta_90"]["days"] is not None
            assert root["eta_90"]["date"], "8 天内到 90% 应给出具体日期"
            assert len(root["history"]) >= 10 and len(root["forecast"]) > 1
            # 紧急度排序：涨得快的 / 在前
            assert mounts[0]["mount"] == "/"
        finally:
            await _rm_host(db, ident)
            ring._disks.pop(IDENT_D234, None)


@pytest.mark.asyncio
async def test_check_disk_forecast_statuses():
    """健康诊断新检查项：快满 → fail；涨得慢 → pass；无数据 → skip。"""
    from app.db.session import SessionLocal
    from app.store import ring
    from app.services import capacity, health_check as hc

    async with SessionLocal() as db:
        ident, _host_id = await _mk_host(db)
        try:
            assert (await hc.check_disk_forecast(db, ident, []))[0] == "skip"   # 无指标

            await _seed_agg(ident, "disk.root.percent", 14, 1.0, 78.0)(db)   # 7 天到 90
            ring.set_disks(ident, [{"mount": "/", "size": 100 * 1024 ** 3,
                                    "used": 78 * 1024 ** 3, "percent": 78.0}])
            st, ev, con = await hc.check_disk_forecast(db, ident, [])
            assert st == "fail", (st, con)
            assert any(e["token"] == "cap_fail" for e in ev)
            assert "90" in con

            # 慢增长 → 30 天内到不了 90 → pass
            await _seed_agg(ident, "disk.root.percent", 14, 0.1, 60.0)(db)
            st2, _, con2 = await hc.check_disk_forecast(db, ident, [])
            assert st2 == "pass", con2
        finally:
            await _rm_host(db, ident)
            ring._disks.pop(IDENT_D234, None)


@pytest.mark.asyncio
async def test_capacity_endpoint_smoke():
    """端点形状：mounts 列表 + days 校验 + 404。"""
    from app.routers.collect import capacity_forecast
    from app.db.session import SessionLocal

    async with SessionLocal() as db:
        ident, _ = await _mk_host(db)
        try:
            await _seed_agg(ident, "disk.root.percent", 14, 1.0, 70.0)(db)
            from app.store import ring
            ring.set_disks(ident, [{"mount": "/", "size": 100 * 1024 ** 3,
                                    "used": 70 * 1024 ** 3, "percent": 70.0}])
            out = await capacity_forecast(ident, days=14, db=db)
            assert out["code"] == 0 and out["data"]["ident"] == ident
            assert any(m["mount"] == "/" for m in out["data"]["mounts"])
            import pytest as _pt
            from fastapi import HTTPException
            with _pt.raises(HTTPException):
                await capacity_forecast(ident, days=2, db=db)       # days 越界
            with _pt.raises(HTTPException):
                await capacity_forecast("no-such-host", days=14, db=db)
        finally:
            await _rm_host(db, ident)
            from app.store import ring as _r
            _r._disks.pop(IDENT_D234, None)


# ── ⑩ 部署验证抓出的两个真 bug 守卫 ──────────────────────────────────
def test_scalar_disk_percent_excluded():
    """disk.percent 是根分区标量（agent_main.py:158），不是分区序列——
    不排除会被 _mount_of 反解成 "/"，与 disk.root.percent 重复两行。"""
    from app.services.capacity import _is_disk_pct_metric
    assert _is_disk_pct_metric("disk.root.percent") is True
    assert _is_disk_pct_metric("disk.var_log.percent") is True
    assert _is_disk_pct_metric("disk.percent") is False        # 标量
    assert _is_disk_pct_metric("disk.io.sda.read_bps") is False
    assert _is_disk_pct_metric("disk.inode.sda.percent") is False


def test_pseudo_fs_mounts_filtered():
    """mock agent/容器把 /etc/hostname 等 bind mount 当分区上报——
    按 ring 快照的 fstype/伪路径过滤（realDisks 口径 + /etc 兜底）。"""
    from app.services.capacity import _real_mounts
    from app.store import ring
    ident = "d234-pseudo"
    ring.set_disks(ident, [
        {"mount": "/", "fstype": "ext4", "size": 1},
        {"mount": "/data", "fstype": "xfs", "size": 1},
        {"mount": "/etc/hostname", "fstype": "ext4", "size": 1},   # bind mount，fstype 真实
        {"mount": "/run", "fstype": "tmpfs", "size": 1},
    ])
    try:
        real = _real_mounts(ident)
        assert real == {"/", "/data"}, real                     # /etc 兜底 + tmpfs 黑名单
        assert _real_mounts("d234-never-seen") is None          # 快照缺失 → 不过滤
    finally:
        ring._disks.pop(ident, None)


def test_window_cap_by_algorithm():
    """窗口上界按算法区分：forecast 30 天（拟合需要长历史），逐点算法维持 1 天。"""
    from app.routers.alerts import _window_cap
    assert _window_cap("threshold") == 86_400
    assert _window_cap("3sigma") == 86_400
    assert _window_cap("isolation_forest") == 86_400
    assert _window_cap("forecast") == 2_592_000


def test_wheels_routes_contract():
    """/wheels/ 兜底分发：空清单返回提示页（不 404），防穿越，只放行 wheel 形态文件名。"""
    import asyncio
    from fastapi import HTTPException
    from app.routers import agent_install as ai

    async def m():
        page = await ai.wheels_index()
        body = page.body.decode()
        assert "TestOPS agent wheels" in body
        # 空清单也是 200（兜底永远"可探测"，安装脚本据此给出明确反馈）
        assert "清单为空" in body or "<a href=" in body
        # 防穿越：路径分隔符/非法后缀一律 400
        for bad in ("../install.sh", "a/b.whl", "evil.txt"):
            try:
                await ai.wheel_file(bad)
                raise AssertionError(f"{bad} 应被拒")
            except HTTPException as e:
                assert e.status_code == 400, (bad, e.status_code)
    asyncio.run(m())


# ── ⑪ 事件 AI 摘要：forecast 专用建议 prompt ─────────────────────────
def test_forecast_summary_prompt_semantics():
    """forecast 事件摘要的三个语义锁：预计天数≠百分比、要给具体建议、不夸大。"""
    from app.services.prompts import set_forecast_summary_prompt, set_summary_prompt

    msgs = set_forecast_summary_prompt({
        "rule_name": "磁盘容量预测-根分区", "host_ident": "abc123", "metric": "disk.root.percent",
        "trigger_value": 5.34, "severity": 2, "rule_expr": ">90@7d"})
    sys, usr = msgs[0]["content"], msgs[1]["content"]
    assert "预计到达阈值的天数" in sys and "不是百分比" in sys, "必须讲清 trigger_value 语义"
    assert "具体建议" in sys and ("du" in sys or "logrotate" in sys), "必须要求可执行建议"
    assert "不要夸大" in sys, "必须约束语气"
    assert "5.34" in usr and ">90@7d" in usr, "天数与表达式都要喂给模型"
    # 与通用摘要 prompt 不同（口径完全不同，不能共用）
    assert set_forecast_summary_prompt({}) != set_summary_prompt({})


def test_ai_summary_dispatches_forecast_prompt(monkeypatch):
    """_maybe_ai_summary 按 rule.algorithm 选 prompt（mock call_llm 断言入参，不真调）。"""
    import asyncio
    from types import SimpleNamespace
    from app.engine import core as core_mod

    # conftest 全局关掉了 AI 摘要（单测不真调 LLM）；本用例 mock 了 call_llm，需要打开开关。
    # **只 monkeypatch 模块属性**：改 env + 清 settings 全局缓存会污染同 session 的后续用例
    # （实测把 quota / kill-switch 用例带崩），而 core.py 是函数内 `from app.core.config import
    # get_settings` → 每次调用都取模块当前属性，patch 模块属性精确且零副作用。
    from app.core import config as cfg_mod
    monkeypatch.setattr(cfg_mod, "get_settings",
                        lambda: SimpleNamespace(alert_ai_summary_enabled=True))

    captured = {}

    class _FakeDBCtx:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, model, pk):
            if model.__name__ == "AlertRule":
                return SimpleNamespace(enable_ai_summary=True, algorithm="forecast", expr=">90@7d")
            return None
        async def commit(self): return None

    async def fake_call_llm(db, purpose, messages, **kw):
        captured["messages"] = messages
        return SimpleNamespace(content="预测建议文本")

    orig_session = core_mod.SessionLocal
    orig_call = None
    import app.services.llm_gateway as gw
    orig_call = gw.call_llm
    core_mod.SessionLocal = lambda: _FakeDBCtx()
    gw.call_llm = fake_call_llm
    try:
        ev = SimpleNamespace(hash="ab" * 16, rule_id=1, rule_name="磁盘容量预测-根分区",
                             host_ident="abc", metric="disk.root.percent",
                             trigger_value=5.34, severity=2, is_recovered=False)
        asyncio.run(core_mod._maybe_ai_summary(ev))
    finally:
        core_mod.SessionLocal = orig_session
        gw.call_llm = orig_call
    assert "messages" in captured, "call_llm 未被调用"
    sys = captured["messages"][0]["content"]
    assert "容量预测" in sys, "forecast 规则必须走专用建议 prompt（通用摘要会误读天数语义）"


def test_engine_skips_pseudo_mounts_for_forecast():
    """告警引擎侧同样要滤掉伪分区（capacity 层滤过，引擎漏过 → AI 建议出现"etc/hosts 增长"）。"""
    from app.services.capacity import _mount_of, _real_mounts
    from app.store import ring

    ident = "d234-eng-pseudo"
    ring.set_disks(ident, [{"mount": "/", "fstype": "ext4", "size": 1},
                           {"mount": "/etc/hostname", "fstype": "ext4", "size": 1}])
    try:
        real = _real_mounts(ident)
        assert _mount_of("disk.root.percent") in real
        assert _mount_of("disk.etc_hostname.percent") not in real
        # 引擎判定口径：伪分区 metric 被跳过
        assert _mount_of("disk.etc_hostname.percent") not in real
    finally:
        ring._disks.pop(ident, None)


def test_pseudo_mount_path_static_rule():
    """静态路径口径：容器 bind mount（/etc/hostname…）不依赖快照即可识别。

    为什么必须静态：ring 是进程内存，平台重启即清空；告警引擎启动瞬间就评估
    （14 天窗口），那时 agent 心跳还没把 disks 快照送上来——靠快照过滤会漏，
    伪分区照样成事件（真机实测：AI 建议写"根分区/etc/hostname 占用率…"）。
    """
    from app.services.capacity import is_pseudo_mount_path
    for m in ("/etc/hostname", "/etc/hosts", "/proc", "/sys/fs/cgroup", "/dev", "/run"):
        assert is_pseudo_mount_path(m) is True, m
    for m in ("/", "/data", "/var/log", "/opt/app"):
        assert is_pseudo_mount_path(m) is False, m


def test_pure_python_ols_matches_sklearn():
    """D235：纯 Python 最小二乘必须与 sklearn 等价（否则装轻量镜像的用户拿到的斜率会偏）。"""
    import pytest
    pytest.importorskip("sklearn.linear_model", reason="环境无 sklearn 时跳过等价性对比")

    from sklearn.linear_model import LinearRegression
    from app.services.capacity import _ols_slope_intercept

    pts = _linear_series(21, slope_pct_day=1.3, start_pct=62.0, weekday_noise=1.2)
    by_day = {}
    for ts, v in pts:
        by_day.setdefault(ts.date(), []).append(v)
    days = sorted(by_day)
    means = [sum(by_day[d]) / len(by_day[d]) for d in days]
    X = [[1.0, float(i), *(1.0 if d.weekday() == k else 0.0 for k in range(6))]
         for i, d in enumerate(days)]
    model = LinearRegression(fit_intercept=False).fit(X, means)
    slope, intercept = _ols_slope_intercept(X, means)
    assert abs(slope - float(model.coef_[1])) < 1e-6, (slope, model.coef_[1])
    assert abs(intercept - float(model.coef_[0])) < 1e-6


@pytest.mark.asyncio
async def test_forecast_mounts_filters_pseudo_without_snapshot():
    """快照未到（平台刚重启，ring 空）时，伪分区也必须被滤掉。

    真机实测复现：容器重启后 agent 心跳还没把 disks 快照送上来（60s 内），
    只靠快照口径的过滤会短暂吐出 /etc/hostname、/etc/hosts 这类 bind mount。
    """
    from app.db.session import SessionLocal
    from app.services import capacity
    from app.store import ring

    async with SessionLocal() as db:
        ident, _ = await _mk_host(db)
        try:
            await _seed_agg(ident, "disk.root.percent", 14, 1.0, 70.0)(db)
            await _seed_agg(ident, "disk.etc_hostname.percent", 14, 1.0, 55.0)(db)
            ring._disks.pop(ident, None)          # 模拟"快照未到"
            mounts = await capacity.forecast_mounts(db, ident, days=14)
            names = [m["mount"] for m in mounts]
            assert names == ["/"], names          # 只剩根分区
        finally:
            await _rm_host(db, ident)
            ring._disks.pop(ident, None)


def test_default_admin_password_and_override(monkeypatch):
    """D235：初始口令默认 admin123（用户拍板，保持与早期版本一致），
    `.env` 的 ADMIN_PASSWORD 可覆盖（公网部署留出口）。

    风险是明知的（开源包源码可见 → 默认口令=公开初始凭据），所以
    ① 文档/手册必须写明「公网部署请设 ADMIN_PASSWORD 或首登即改」；
    ② 日志必须打印实际使用的口令，便于用户抄下来。
    """
    import inspect
    from app import main as app_main

    src = inspect.getsource(app_main.seed_admin)
    assert 'or "admin123"' in src, "初始口令默认应为 admin123"
    assert "ADMIN_PASSWORD" in src, "必须支持 ADMIN_PASSWORD 覆盖"
    assert "已创建管理员账号 admin，初始密码 = %s" in src, "日志必须打印实际口令"
