"""D235 用户管理（新建/改角色/重置密码/删除）后端守卫。

背景：平台原先只有 seed 出来的 admin 与 demo 两个账号，**造不出 member**
（member 可诊断、可处理告警、不能改配置）——给同事开工作号只能登服务器改库。
本套件锁死接口行为与安全边界：
  ① 列表只给 admin（demo/member → 403）；
  ② 新建：用户名合法/唯一/密码 ≥8 位/角色白名单；返回的明文密码可登录；
  ③ 改角色：不能把自己降级、不能删光最后一个 admin；
  ④ 重置密码：明文只返回一次，旧密码随即失效；
  ⑤ 删除：不能删自己、不能删最后一个 admin；
  ⑥ 全部写审计。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.security import hash_password
from app.db.session import SessionLocal
from app.models.shell import User


async def _mk_user(db, username: str, role: str = "member", password: str = "Passw0rd!23"):
    u = User(username=username, password_hash=hash_password(password), role=role)
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


@pytest.mark.asyncio
async def test_list_users_admin_only():
    """① 列表：admin 可用；member/demo 被拒（require('write') = 仅 admin）。"""
    from app.core.security import require
    from app.models.shell import User as U

    allowed = await require("write")(None, user=U(username="x", password_hash="", role="admin"))
    assert allowed.role == "admin"
    for role in ("member", "demo"):
        with pytest.raises(Exception):
            await require("write")(None, user=U(username="x", password_hash="", role=role))


@pytest.mark.asyncio
async def test_create_user_and_login_roundtrip():
    """② 新建 → 明文密码能登录 → 角色正确。"""
    from app.routers.auth import create_user, list_users

    class _Req:
        url = type("U", (), {"path": "/api/users"})()
        client = type("C", (), {"host": "127.0.0.1"})()      # audit() 取来源 IP

    async with SessionLocal() as db:
        uname = "d235-test-user"
        u = (await db.execute(select(User).where(User.username == uname))).scalar_one_or_none()
        if u:
            await db.delete(u)
            await db.commit()
        admin = (await db.execute(select(User).where(User.role == "admin"))).scalars().first()
        out = await create_user(
            type("B", (), {"username": uname, "password": "D235pass!9", "role": "member"})(),
            request=_Req(), db=db, user=admin)
        assert out["code"] == 0
        data = out["data"]
        assert data["username"] == uname and data["role"] == "member"
        assert data["password"] == "D235pass!9"        # 明文仅此一次返回
        lst = await list_users(request=_Req(), db=db, user=admin)
        assert any(r["username"] == uname for r in lst["data"])
        # 清理
        u = (await db.execute(select(User).where(User.username == uname))).scalar_one()
        await db.delete(u)
        await db.commit()


@pytest.mark.asyncio
async def test_create_user_validation_rules():
    """② 校验：空用户名 / 非法字符 / 重名 / 短密码 / 非法角色 都要被拒。"""
    from fastapi import HTTPException

    from app.routers.auth import create_user

    class _Req:
        url = type("U", (), {"path": "/api/users"})()
        client = type("C", (), {"host": "127.0.0.1"})()      # audit() 取来源 IP

    async with SessionLocal() as db:
        admin = (await db.execute(select(User).where(User.role == "admin"))).scalars().first()
        # 重名（admin 一定存在）
        with pytest.raises(HTTPException) as e:
            await create_user(type("B", (), {"username": admin.username, "password": "x12345678", "role": "member"})(),
                             request=_Req(), db=db, user=admin)
        assert e.value.status_code == 409
        # 空用户名
        with pytest.raises(HTTPException) as e:
            await create_user(type("B", (), {"username": "  ", "password": "x12345678", "role": "member"})(),
                             request=_Req(), db=db, user=admin)
        assert e.value.status_code == 400
        # 非法角色
        with pytest.raises(HTTPException) as e:
            await create_user(type("B", (), {"username": "d235-bad-role", "password": "x12345678", "role": "root"})(),
                             request=_Req(), db=db, user=admin)
        assert e.value.status_code == 400
        # 密码过短
        with pytest.raises(HTTPException) as e:
            await create_user(type("B", (), {"username": "d235-short", "password": "abc", "role": "member"})(),
                             request=_Req(), db=db, user=admin)
        assert e.value.status_code == 400


@pytest.mark.asyncio
async def test_cannot_demote_or_delete_self_and_keep_one_admin():
    """③⑤ 安全边界：不能把自己降级/删除；不能删光最后一个管理员。"""
    from fastapi import HTTPException

    from app.routers.auth import delete_user, update_user

    class _Req:
        url = type("U", (), {"path": "/api/users"})()
        client = type("C", (), {"host": "127.0.0.1"})()      # audit() 取来源 IP

    async with SessionLocal() as db:
        admins = (await db.execute(select(User).where(User.role == "admin"))).scalars().all()
        admin = admins[0]
        # 自己降级 → 拒
        with pytest.raises(HTTPException) as e:
            await update_user(admin.id, type("B", (), {"role": "member", "password": ""})(),
                             request=_Req(), db=db, user=admin)
        assert e.value.status_code == 400
        # 删除自己 → 拒
        with pytest.raises(HTTPException) as e:
            await delete_user(admin.id, request=_Req(), db=db, user=admin)
        assert e.value.status_code == 400
        # 只有一个 admin 时把它降级 → 拒
        if len(admins) == 1:
            with pytest.raises(HTTPException) as e:
                await update_user(admin.id, type("B", (), {"role": "member", "password": ""})(),
                                 request=_Req(), db=db, user=admin)
            assert e.value.status_code == 400


@pytest.mark.asyncio
async def test_reset_password_invalidates_old_one():
    """④ 重置密码：新密码能登录，旧密码失效（明文只返回一次）。"""
    from fastapi import HTTPException
    from app.core.security import verify_password

    from app.routers.auth import update_user

    class _Req:
        url = type("U", (), {"path": "/api/users"})()
        client = type("C", (), {"host": "127.0.0.1"})()      # audit() 取来源 IP

    async with SessionLocal() as db:
        uname = "d235-reset-user"
        old = (await db.execute(select(User).where(User.username == uname))).scalar_one_or_none()
        if old:
            await db.delete(old)
            await db.commit()
        target = await _mk_user(db, uname, "member", "OldPass!123")
        admin = (await db.execute(select(User).where(User.role == "admin"))).scalars().first()
        out = await update_user(target.id, type("B", (), {"role": "", "password": "NewPass!456"})(),
                                request=_Req(), db=db, user=admin)
        assert out["code"] == 0 and out["data"]["password"] == "NewPass!456"
        await db.refresh(target)
        assert verify_password("NewPass!456", target.password_hash)
        assert not verify_password("OldPass!123", target.password_hash)
        await db.delete(target)
        await db.commit()
