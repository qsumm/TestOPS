#!/usr/bin/env bash
# create-user.sh —— 命令行创建/更新平台用户（D235）
#
# 为什么有它：平台此前只有 seed 出来的 admin 与 demo 两个账号，**造不出 member**
# （member 可诊断、可处理告警、不能改配置）——给同事开工作号只能登服务器改库。
# 现在界面可建（系统设置 → 用户），本脚本给运维一条 CLI 通道（无前端/应急场景）。
#
# 用法：
#   bash deploy/create-user.sh --user zhangsan --role member            # 随机密码
#   bash deploy/create-user.sh --user zhangsan --role member --password Member2026
#   bash deploy/create-user.sh --user zhangsan --role admin              # 已存在则改角色
#
# 角色：admin（全权）/ member（可诊断+可处理告警，不能改配置）/ demo（只读）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd -P)" || {
  echo "✗ 无法定位脚本目录（当前 shell 的工作目录可能已被删除）。" >&2
  echo "  解决：cd ~ 后重新执行 bash ~/TestOPS/deploy/create-user.sh" >&2
  exit 1
}
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd -P)" || exit 1
cd "$ROOT_DIR" || exit 1

USERNAME=""
PASSWORD=""
ROLE="member"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --user)     USERNAME="${2:?--user 需要参数}"; shift 2 ;;
    --password) PASSWORD="${2:?--password 需要参数}"; shift 2 ;;
    --role)     ROLE="${2:?--role 需要参数}"; shift 2 ;;
    -h|--help)  sed -n '2,16p' "$0"; exit 0 ;;
    \#*)        echo "  提示：参数里的「# …」看起来是注释，已忽略（zsh 不把 # 当注释）"; shift ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

if [[ -z "${USERNAME}" ]]; then
  echo "用法：bash deploy/create-user.sh --user <用户名> [--role admin|member|demo] [--password <密码>]" >&2
  exit 1
fi
case "${ROLE}" in
  admin|member|demo) ;;
  *) echo "✗ 角色只允许 admin / member / demo" >&2; exit 1 ;;
esac

ENV_ARGS=()
if [[ -f .env ]]; then
  ENV_ARGS=(--env-file .env)
elif [[ -f backend/.env ]]; then
  ENV_ARGS=(--env-file backend/.env)
else
  echo "  （未找到 .env，直接用现有容器执行）"
fi

echo "==> 创建/更新用户 ${USERNAME}（角色 ${ROLE}）…"
docker compose "${ENV_ARGS[@]}" -f deploy/docker-compose.yaml exec -T \
  -e NEW_USER="$USERNAME" -e NEW_PWD="$PASSWORD" -e NEW_ROLE="$ROLE" platform \
  python - <<'PY'
import asyncio, os, secrets, sys

async def main() -> int:
    from sqlalchemy import select
    from app.core.security import hash_password
    from app.db.session import SessionLocal
    from app.models.shell import User

    uname = os.environ["NEW_USER"].strip()
    pwd = (os.environ.get("NEW_PWD") or "").strip()
    role = os.environ.get("NEW_ROLE") or "member"
    async with SessionLocal() as db:
        u = (await db.execute(select(User).where(User.username == uname))).scalar_one_or_none()
        if u is None:
            final = pwd or secrets.token_urlsafe(9)[:12]
            db.add(User(username=uname, password_hash=hash_password(final), role=role))
            print(f"已创建 {uname}（角色 {role}）")
        else:
            final = ""
            if pwd:
                u.password_hash = hash_password(pwd)
                final = pwd
            u.role = role
            print(f"已更新 {uname}（角色 {role}{'，密码已重设' if final else '，密码未改'}）")
        await db.commit()
        if final:
            print(f"密码：{final}")
    return 0

sys.exit(asyncio.run(main()))
PY
