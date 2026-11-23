#!/usr/bin/env bash
# reset-admin-password.sh —— 重置管理员密码（D235）
#
# 为什么需要：管理员初始密码**只在首次启动时打印一次**（安全设计：开源包源码可见，
# 不能有固定默认口令）。但如果：
#   · 当时没抄下来 / 日志被轮转 / 容器重建过（管理员已存在，不再打印）
#   · 忘了改过的密码
# 就再也没法登录了。本脚本直接改数据库里的密码哈希，给你一个可用的新口令。
#
# 用法：
#   bash deploy/reset-admin-password.sh                 # 生成随机 14 位密码并打印
#   bash deploy/reset-admin-password.sh --password 我的密码   # 指定密码
#   bash deploy/reset-admin-password.sh --user demo --password x  # 重置其它账号
#
# 说明：只改 users 表的 password_hash，不动任何业务数据；当前登录会话仍然有效。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd -P)" || {
  echo "✗ 无法定位脚本目录（当前 shell 的工作目录可能已被删除）。" >&2
  echo "  解决：cd ~ 后重新执行 bash ~/TestOPS/deploy/reset-admin-password.sh" >&2
  exit 1
}
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd -P)" || exit 1
cd "$ROOT_DIR" || exit 1

USERNAME="admin"
PASSWORD=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --user)     USERNAME="${2:?--user 需要参数}"; shift 2 ;;
    --password) PASSWORD="${2:?--password 需要参数}"; shift 2 ;;
    -h|--help)  sed -n '2,18p' "$0"; exit 0 ;;
    # D235：兼容"把注释一起复制进来"的情况（真机实测：用户从聊天/文档复制
    # `bash deploy/reset-admin-password.sh  # 会打印新密码`，zsh 不把 # 当注释 →
    # 脚本收到 `#` 和后面整句 → 报"未知参数"直接退出，用户以为脚本坏了）。
    \#*) echo "  提示：参数里的「# …」看起来是注释，已忽略（zsh 不把 # 当注释）"; shift ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

# .env 位置容错：仓库根优先，其次 backend/.env（用户可能把模板复制到了那里）。
# 找不到也能工作——容器已在运行，compose 的 exec 不依赖 .env 里的变量。
ENV_ARGS=()
if [[ -f .env ]]; then
  ENV_ARGS=(--env-file .env)
elif [[ -f backend/.env ]]; then
  ENV_ARGS=(--env-file backend/.env)
else
  echo "  （未找到 .env，直接用现有容器执行；如需 --env-file 请cd 到仓库根）"
fi

# 未指定则生成随机口令（去掉易混淆字符）
if [[ -z "$PASSWORD" ]]; then
  PASSWORD="$(python3 - <<'PY'
import secrets, string
alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
print("".join(secrets.choice(alphabet) for _ in range(14)))
PY
)"
fi

echo "==> 重置账号「${USERNAME}」的密码…"

# 在平台容器内执行（复用平台的 hash_password，保证与登录校验一致）
docker compose "${ENV_ARGS[@]}" -f deploy/docker-compose.yaml exec -T \
  -e RESET_USER="$USERNAME" -e RESET_PASSWORD="$PASSWORD" platform \
  python - <<'PY'
import asyncio, os, sys

async def main() -> int:
    from sqlalchemy import select
    from app.core.security import hash_password
    from app.db.session import SessionLocal
    from app.models.shell import User

    username = os.environ["RESET_USER"]
    password = os.environ["RESET_PASSWORD"]
    async with SessionLocal() as db:
        u = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
        if u is None:
            print(f"✗ 账号 {username} 不存在（已有账号见下）", file=sys.stderr)
            rows = (await db.execute(select(User.username))).scalars().all()
            print("  已有账号：" + ("、".join(rows) or "（无）"), file=sys.stderr)
            return 1
        u.password_hash = hash_password(password)
        await db.commit()
        print(f"✓ 已重置 {username} 的密码")
    return 0

sys.exit(asyncio.run(main()))
PY

echo
echo "─────────────────────────────────────────────"
echo "  账号：${USERNAME}"
echo "  密码：${PASSWORD}"
echo "─────────────────────────────────────────────"
echo "  立即用上面的账号密码登录；登录后建议改一次口令。"
