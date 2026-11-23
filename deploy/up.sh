#!/usr/bin/env bash
# up.sh —— 一键部署：构建 → 启动 → 等就绪 → 打印访问信息（D235）
#
# 为什么有它：README 的三步（clone / cp .env / compose up）之后，
# **必须再等 40~180 秒**平台才真正可用（数据库初始化 + 迁移）。
# 新手最常见的误判就是"以为失败"——curl 报 Connection reset、日志里没有管理员密码。
# 本脚本把"等待"也一并做完，最后直接把地址和初始密码打出来。
#
# 用法：
#   git clone https://github.com/qsumm/TestOPS.git && cd TestOPS
#   cp backend/.env.example .env      # 填 JWT_SECRET / KEY_STORE_SECRET / DB_PASSWORD
#   bash deploy/up.sh
#
# 可选环境变量：
#   WAIT_TIMEOUT=600   # 等就绪超时（默认 300s；慢机器/全新卷可放宽）
#   PLATFORM_PORT=8848 # 对外端口（默认取 .env 的值）
set -euo pipefail

# D235：用**绝对路径**定位仓库根——真机踩过：用户在"已被删除/替换的目录"里执行
# （先 rm -rf 再 clone 同名目录，旧 shell 仍指向已删 inode），此时 `cd 相对路径` 与
# `bash deploy/xxx.sh` 都会因 getcwd 失败报 "cannot access parent directories"。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd -P)" || {
  echo "✗ 无法定位脚本所在目录（当前 shell 的工作目录可能已被删除）。" >&2
  echo "  解决：cd ~ 后重新执行 bash ~/TestOPS/deploy/up.sh" >&2
  exit 1
}
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd -P)" || exit 1
cd "$ROOT_DIR" || exit 1

if [[ ! -f .env ]]; then
  echo "✗ 缺少 .env —— 先执行：cp backend/.env.example .env" >&2
  echo "  并把 JWT_SECRET / KEY_STORE_SECRET / DB_PASSWORD 换成随机值" >&2
  echo "  生成：python3 -c \"import secrets;print(secrets.token_urlsafe(48))\"（跑三次）" >&2
  exit 1
fi

echo "==> 1/2 构建并启动（首次约 2~4 分钟；慢镜像源见《自建部署加速指引》）"
docker compose --env-file .env -f deploy/docker-compose.yaml up -d --build

echo
echo "==> 2/2 等待平台就绪"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-300}" bash "$SCRIPT_DIR/wait-ready.sh"
