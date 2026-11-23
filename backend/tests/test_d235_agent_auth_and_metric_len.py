"""D235 Agent 认证死循环 + 超长指标名 两个真机故障的守卫。

① 超长指标名（K8s 节点）：agent 把 kubelet 子路径整段塞进指标名
   （disk.var_lib_kubelet_pods_<uuid>_volume-subpaths_... .percent，100+ 字符），
   而 metric_agg.metric 是 VARCHAR(64) → 落库 1406，且 executemany **整批回滚**
   （一行超长 → 那批所有指标全丢，聚合每分钟失败一次，指标持续缺失）。
   守卫：agent 侧限长、平台 ingest 侧规范化、flush 侧最后防线跳过超长行。

② 认证死循环：同机跑着多份 agent（systemd 服务 + 手工实例）→ 共用同一机器指纹 →
   任一份 401 就重新注册 → 平台每次注册换发新 token → 另一份的旧 token 立刻失效
   → 无限「认证失效 → 重注册」循环（真机日志每 2 秒一轮）。
   守卫：同一 ident 在冷却窗口内重复注册**复用同一 token**（不换发）→ 不再互踢。
"""
from __future__ import annotations

import pytest

from app.services.ingest import METRIC_MAXLEN, _normalize_metric

LONG_METRIC = ("disk.var_lib_kubelet_pods_ea8a207a-b933-4f51-be50-58f6edbad93f_"
               "volume-subpaths_ks-console-config_ks-console_0.percent")


# ── ① 超长指标名 ───────────────────────────────────────────────────────
def test_normalize_metric_limits_length():
    """平台侧规范化：超长 → 截断 + 8 位 hash，稳定且唯一。"""
    n = _normalize_metric(LONG_METRIC)
    assert len(n) <= METRIC_MAXLEN, len(n)
    assert n != LONG_METRIC
    assert _normalize_metric(LONG_METRIC) == n, "同名必须规范化到同一结果（否则指标分裂）"
    assert _normalize_metric("disk.root.percent") == "disk.root.percent", "正常名不得改动"


def test_normalize_metric_distinguishes_different_long_names():
    """两个不同的超长名不能撞成同一个（否则两条序列合并 → 数据失真）。"""
    a = _normalize_metric(LONG_METRIC)
    b = _normalize_metric(LONG_METRIC.replace("ks-console_0", "ks-console_1"))
    assert a != b


def test_agent_metric_key_is_length_limited():
    """Agent 侧同样限长（口径与平台一致：前缀 + hash）。"""
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "agent" / "agent_main.py").read_text(encoding="utf-8")
    m = re.search(r"def _metric_key\(mount: str\) -> str:.*?\n\n\n", src, re.S)
    assert m, "agent 侧缺少 _metric_key（超长指标名会把整批落库打挂）"
    ns: dict = {}
    exec(m.group(0), ns)
    key = ns["_metric_key"]("/var/lib/kubelet/pods/ea8a207a-b933-4f51-be50-58f6edbad93f/"
                            "volume-subpaths/ks-console-config/ks-console-0")
    name = f"disk.{key}.percent"
    assert len(name) <= 64, f"指标名仍超长：{len(name)}"
    assert ns["_metric_key"]("/data") == "data", "普通挂载点不得被改写"


def test_flush_skips_oversized_metric_names():
    """flush 侧最后防线：超长名**跳过**而不是让整批 executemany 失败。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "backend" / "app" / "store" / "ring.py").read_text(encoding="utf-8")
    assert "跳过超长指标名" in src, "flush_aggregates 必须跳过超长指标名（整批失败会丢光那批数据）"
    assert "跳过超长维度序列指标名" in src, "flush_series 同样要兜底"


# ── ② 认证死循环 ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_repeated_register_reuses_token_within_cooldown():
    """同一 ident 在冷却窗口内重复注册 → 复用同一 token（不换发）→ 同机多实例不再互踢。"""
    from app.db.session import SessionLocal
    from app.models.shell import User
    from app.services import ingest

    async with SessionLocal() as db:
        admin = (await db.execute(
            __import__("sqlalchemy").select(User).where(User.role == "admin"))).scalars().first()
        if admin is None:
            pytest.skip("库里没有 admin")

        fp = "d235-fp-token-reuse"
        ident = __import__("hashlib").sha256(fp.encode()).hexdigest()[:32]
        payload = {"fingerprint": fp, "hostname": "d235-reuse", "install_token": ""}
        ingest._TOKEN_ISSUE_AT.pop(ident, None)

        # 第一次：正常签发（该 ident 未注册过 → 需要令牌，这里直接塞一枚）
        tok = ingest.mint_install_token()
        payload["install_token"] = tok
        r1 = await ingest.register_agent(db, payload)
        token1 = r1["token"]

        # 冷却窗口内再次注册（模拟同机第二份 agent 跟着 401 重注册）
        r2 = await ingest.register_agent(db, payload)
        assert r2["token"] == token1, "冷却窗口内必须复用同一 token，否则同机多实例互相顶掉凭据"

        # 冷却窗口外 → 允许换发（凭据轮换能力仍在）
        ingest._TOKEN_ISSUE_AT[ident] = ingest._TOKEN_ISSUE_AT.get(ident, None) or __import__("datetime").datetime.utcnow()
        ingest._TOKEN_ISSUE_AT[ident] -= __import__("datetime").timedelta(seconds=ingest.TOKEN_ISSUE_COOLDOWN_SEC + 1)
        r3 = await ingest.register_agent(db, payload)
        assert r3["token"] != token1, "冷却窗口外应换发新 token"

        # 清理（users 表按 username 定位；ident 在 hosts 表）
        row = (await db.execute(
            __import__("sqlalchemy").select(User).where(User.username == "d235-reuse"))).scalar_one_or_none()
        if row is not None:
            await db.delete(row)
            await db.commit()
        ingest._TOKEN_ISSUE_AT.pop(ident, None)


@pytest.mark.asyncio
async def test_install_token_can_be_minted_in_batch():
    """批量令牌：count=N → N 枚独立令牌（此前只有一枚 → 多台机器只有第一台能注册）。"""
    from app.services import ingest

    tokens = [ingest.mint_install_token() for _ in range(3)]
    assert len(set(tokens)) == 3, "每枚令牌必须唯一"
    assert all(ingest.INSTALL_TOKENS.get(t) == "unused" for t in tokens)


def test_agent_has_auth_backoff():
    """Agent 侧必须有退避（真机上每 2 秒一轮把日志冲爆）。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "agent" / "agent_main.py").read_text(encoding="utf-8")
    assert "_auth_backoff_seconds" in src, "agent 缺少认证退避"
    assert "连续认证失效" in src or "排查提示" in src, "agent 应给出同机多实例的排查提示"
