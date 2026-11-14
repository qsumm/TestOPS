"""认证路由（FR-A1；05 §1）。"""
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import (
    audit,
    capabilities,
    create_access_token,
    get_current_user,
    hash_password,
    require,
    verify_password,
)
from app.db.session import get_db
from app.models.shell import User

router = APIRouter(prefix="/api/auth", tags=["auth"])
settings = get_settings()


class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(body: LoginIn, request: Request, db=Depends(get_db)):
    user = (await db.execute(select(User).where(User.username == body.username))).scalar_one_or_none()
    if not user or not verify_password(body.password, user.password_hash):
        # D181：登录失败也必须有痕（爆破尝试的唯一定位证据）；审计不可用不影响 401。
        await audit(db=db, request=request, user=None, action="login_failed",
                    resource="-", detail={"username": body.username})
        raise HTTPException(401, "用户名或密码错误")
    user.last_login = datetime.utcnow()
    await db.commit()
    # D181：审计覆盖「登录」此前只有文案没有实现——现在成功登录落 login（含来源 IP）。
    await audit(db=db, request=request, user=user, action="login",
                resource=f"user:{user.id}", detail={"username": user.username})
    return {"code": 0, "msg": "ok", "data": {
        "access_token": create_access_token(user.id, user.role),
        "role": user.role,
        "preferred_lang": user.preferred_lang,   # D97：UI 中英文切换
    }}


@router.post("/demo")
async def demo_login(request: Request, db=Depends(get_db)):
    if not settings.demo_enabled:
        raise HTTPException(403, "演示模式未开放")
    user = (await db.execute(select(User).where(User.role == "demo"))).scalar_one_or_none()
    if user is None:
        raise HTTPException(403, "演示用户未初始化")
    await audit(db=db, request=request, user=user, action="login",
                resource=f"user:{user.id}", detail={"username": user.username})
    return {"code": 0, "msg": "ok", "data": {
        "access_token": create_access_token(user.id, "demo"), "role": "demo", "username": user.username,
        "preferred_lang": user.preferred_lang}}


@router.get("/me")
async def me(user: User = Depends(get_current_user)):
    return {"code": 0, "msg": "ok", "data": {
        "username": user.username, "role": user.role, "last_login": str(user.last_login or ""),
        "preferred_lang": user.preferred_lang,   # D97
    }}


class LangIn(BaseModel):
    preferred_lang: str   # "zh" | "en"


@router.patch("/me/lang")
async def set_my_lang(body: LangIn, db=Depends(get_db), user: User = Depends(get_current_user)):
    """D97：右上角"中/EN"切换调用。写库保证刷新仍生效；不要求 write 权限（用户改自己）。"""
    v = (body.preferred_lang or "").strip().lower()
    if v not in ("zh", "en"):
        raise HTTPException(400, "preferred_lang 只能是 zh 或 en")
    user.preferred_lang = v
    await db.commit()
    return {"code": 0, "msg": "ok", "data": {"preferred_lang": v}}


@router.get("/perms")
async def perms(user: User = Depends(get_current_user)):
    """当前用户能力集——前端按钮可见性的**唯一来源**（D8：不再在前端按 role 硬编码）。"""
    return {"code": 0, "msg": "ok", "data": {
        "username": user.username, "role": user.role, "can": capabilities(user)}}


# SSE 换票（05 §8 / 03 §1.22）：路径为 /api/sse-ticket，故单独一个不带 /auth 前缀的路由
api_router = APIRouter(prefix="/api", tags=["sse"])


@api_router.get("/sse-ticket")
async def sse_ticket(user: User = Depends(get_current_user)):
    from app.services import sse_ticket as tickets

    return {"code": 0, "msg": "ok", "data": {"ticket": tickets.mint(user.id, user.role)}}


class PasswordIn(BaseModel):
    old_password: str
    new_password: str


@router.post("/password")
async def change_password(body: PasswordIn, request: Request = None, db=Depends(get_db), user: User = Depends(get_current_user)):
    """自助改密（D18 补：手册 §1 承诺「首次登录后请立即修改」，但平台原先**没有改密能力**——
    买家部署到公网却改不掉默认口令 admin/admin123，属安全缺口）。

    规则：校验原密码 → 新密码 ≥8 位且不同于原密码 → bcrypt 落库 → 写审计。
    """
    if not verify_password(body.old_password, user.password_hash):
        raise HTTPException(400, "原密码不正确")
    if len(body.new_password or "") < 8:
        raise HTTPException(400, "新密码至少 8 位")
    if body.new_password == body.old_password:
        raise HTTPException(400, "新密码不能与原密码相同")
    user.password_hash = hash_password(body.new_password)
    await db.commit()
    await audit(db=db, request=request, user=user, action="password_change", resource=f"user:{user.id}")
    return {"code": 0, "msg": "ok", "data": {"username": user.username}}


# ── D235：用户管理（新建/改角色/重置密码/删除）───────────────────────────
#
# 缺口：平台原先只有 seed 出来的 admin 与 demo 两个账号，**没有任何创建用户的
# 入口**——而权限模型里明明定义了 member 角色（可诊断、可处理告警、不能改配置），
# 却造不出这种账号 → 想给同事开一个"只干活不改配置"的工作号，当时只能登服务器改库。
# 这里补上最小自洽闭环（全部 admin only + 写审计）。

VALID_ROLES = {"admin", "member", "demo"}


class UserCreateIn(BaseModel):
    username: str
    password: str = ""          # 空 = 服务端生成随机密码（只返回一次）
    role: str = "member"


class UserUpdateIn(BaseModel):
    role: str = ""              # 空 = 不改
    password: str = ""          # 空 = 不改


def _gen_password() -> str:
    import secrets as _s
    return _s.token_urlsafe(9)[:12]


@api_router.get("/users")
async def list_users(request: Request = None, db=Depends(get_db), user: User = Depends(require("write"))):
    """用户列表（仅 admin）。前端「系统设置 → 用户」页签的数据源。"""
    rows = (await db.execute(select(User).order_by(User.id))).scalars().all()
    return {"code": 0, "msg": "ok", "data": [
        {"id": u.id, "username": u.username, "role": u.role,
         "last_login": str(u.last_login or ""), "created_at": str(u.created_at or "")}
        for u in rows
    ]}


@api_router.post("/users")
async def create_user(body: UserCreateIn, request: Request = None,
                      db=Depends(get_db), user: User = Depends(require("write"))):
    """新建用户（仅 admin）。密码为空则生成随机密码，**仅此一次返回**。"""
    name = (body.username or "").strip()
    if not name or len(name) > 64:
        raise HTTPException(400, "用户名必填且不超过 64 字符")
    if not name.replace("_", "").replace("-", "").replace(".", "").isalnum():
        raise HTTPException(400, "用户名只能包含字母、数字、下划线、中划线和点")
    role = (body.role or "member").strip()
    if role not in VALID_ROLES:
        raise HTTPException(400, "角色只允许 admin / member / demo")
    dup = (await db.execute(select(User).where(User.username == name))).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(409, f"用户名 {name} 已存在")
    pwd = (body.password or "").strip() or _gen_password()
    if len(pwd) < 8:
        raise HTTPException(400, "密码至少 8 位")
    u = User(username=name, password_hash=hash_password(pwd), role=role)
    db.add(u)
    await db.commit()
    await audit(db=db, request=request, user=user, action="user_create",
                resource=f"user:{u.id}", detail={"username": name, "role": role})
    return {"code": 0, "msg": "ok", "data": {"id": u.id, "username": name, "role": role, "password": pwd}}


@api_router.put("/users/{uid}")
async def update_user(uid: int, body: UserUpdateIn, request: Request = None,
                      db=Depends(get_db), user: User = Depends(require("write"))):
    """改角色 / 重置密码（仅 admin）。密码为空则不修改；返回明文仅在重置时给出一次。"""
    target = await db.get(User, uid)
    if target is None:
        raise HTTPException(404, "用户不存在")
    changed = {}
    if body.role:
        role = body.role.strip()
        if role not in VALID_ROLES:
            raise HTTPException(400, "角色只允许 admin / member / demo")
        if target.id == user.id and role != "admin":
            raise HTTPException(400, "不能把自己降级（会失去管理员权限且无法再改回）")
        if target.role == "admin" and role != "admin":
            admins = (await db.execute(select(User).where(User.role == "admin"))).scalars().all()
            if len(admins) <= 1:
                raise HTTPException(400, "系统至少要保留一个管理员")
        target.role = role
        changed["role"] = role
    out_pwd = ""
    if body.password:
        pwd = body.password.strip()
        if len(pwd) < 8:
            raise HTTPException(400, "密码至少 8 位")
        target.password_hash = hash_password(pwd)
        out_pwd = pwd
        changed["password_reset"] = True
    await db.commit()
    await audit(db=db, request=request, user=user, action="user_update",
                resource=f"user:{target.id}", detail={"username": target.username, **changed})
    return {"code": 0, "msg": "ok", "data": {"id": target.id, "username": target.username,
                                             "role": target.role, "password": out_pwd}}


@api_router.delete("/users/{uid}")
async def delete_user(uid: int, request: Request = None,
                      db=Depends(get_db), user: User = Depends(require("write"))):
    """删除用户（仅 admin）。不能删自己、不能删最后一个管理员。"""
    target = await db.get(User, uid)
    if target is None:
        raise HTTPException(404, "用户不存在")
    if target.id == user.id:
        raise HTTPException(400, "不能删除自己")
    if target.role == "admin":
        admins = (await db.execute(select(User).where(User.role == "admin"))).scalars().all()
        if len(admins) <= 1:
            raise HTTPException(400, "系统至少要保留一个管理员")
    name = target.username
    await db.delete(target)
    await db.commit()
    await audit(db=db, request=request, user=user, action="user_delete",
                resource=f"user:{uid}", detail={"username": name})
    return {"code": 0, "msg": "ok", "data": {"username": name}}
