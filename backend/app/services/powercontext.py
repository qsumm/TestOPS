"""PowerContext 客户端（试点，D233）——跨会话运维经验的读写。

PowerContext（OceanBase 开源，Apache-2.0）是独立的上下文服务：
  · 本地栈以独立容器运行（aiops-demo_default 网络，http://powercontext:8000）
  · 未配置 POWERCONTEXT_BASE_URL 时全部函数静默降级（试点默认关闭，不影响 OSS 部署）

两个用途：
  · memory_search：诊断开始时召回相关运维经验（向量 + 文本混合检索）
  · remember：诊断结束后把本次经验写入（显式写入不需要生成模型）
"""

import os
import time

import httpx

_BASE = (os.environ.get("POWERCONTEXT_BASE_URL") or "").rstrip("/")
_SCOPE = os.environ.get("POWERCONTEXT_SCOPE_ID") or ""
_TIMEOUT = float(os.environ.get("POWERCONTEXT_TIMEOUT_SECONDS", "6"))


def enabled() -> bool:
    """试点开关：未配置 BASE_URL 或 SCOPE_ID 时为 False，所有调用方静默跳过。"""
    return bool(_BASE and _SCOPE)


async def memory_search(query: str, top_k: int = 3) -> list[dict]:
    """按语义召回运维经验。失败/未启用返回 []（调用方降级，不阻塞诊断）。"""
    if not enabled():
        return []
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(f"{_BASE}/v1/memory/search", json={
                "scope_id": _SCOPE, "query": query})
        hits = (r.json() or {}).get("hits") or []
        out = []
        for h in hits[:top_k]:
            m = h.get("memory") or {}
            if (m.get("state") or "active") != "active":
                continue
            out.append({"text": m.get("text") or "", "kind": m.get("kind") or "",
                        "score": h.get("score")})
        return out
    except Exception:  # noqa: BLE001 —— 试点：任何失败都降级，不影响诊断主流程
        return []


async def remember(text: str, kind: str = "ops-lesson", reason: str = "",
                   description: str = "") -> bool:
    """诊断结束后写入一条经验（显式写入，不需要生成模型）。失败返回 False。"""
    if not enabled() or not (text or "").strip():
        return False
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(f"{_BASE}/v1/memory/remember", json={
                "scope_id": _SCOPE, "kind": kind, "text": text[:4000],
                "reason": reason[:500] or None,
                "description": description[:200] or None})
        return r.status_code == 200 and bool((r.json() or {}).get("changed"))
    except Exception:  # noqa: BLE001
        return False


def started_at_note() -> str:
    """诊断备注里标注接入状态（可观测性）。"""
    return f"PowerContext 接入={'开' if enabled() else '关'}（启动于 {time.strftime('%m-%d %H:%M')}）"
