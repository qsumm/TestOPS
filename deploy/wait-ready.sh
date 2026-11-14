#!/usr/bin/env bash
# wait-ready.sh —— 等平台真正就绪，并打印访问信息（D235）
#
# 为什么需要：平台启动顺序是「数据库就绪 → 迁移 → 起 HTTP 服务」。
# 首次启动（尤其复用已有数据卷时，SeekDB 要校验口令并恢复）要 **40~90 秒**，
# 全新数据卷首次初始化可能 **1~3 分钟**。这期间：
#   · curl /api/health → Connection reset by peer（端口映射已建，uvicorn 还没监听）
#   · docker logs ... | grep 管理员 → 没有输出（管理员种子还没执行）
#   · docker ps 里数据库显示 Restarting（SeekDB 首次初始化中）
# 这些都是**正常现象**，不是部署失败——本脚本替你等到真正就绪。
#
# 用法：
#   bash deploy/wait-ready.sh                 # 默认等 5 分钟，端口取 .env 的 PLATFORM_PORT
#   WAIT_TIMEOUT=600 bash deploy/wait-ready.sh   # 放宽到 10 分钟
#   bash deploy/wait-ready.sh --port 8848
#
# 退出码：0 = 已就绪；1 = 超时（并打印排查命令）
set -uo pipefail

# 超时：环境变量 WAIT_TIMEOUT 优先（up.sh 用它传），--timeout 可覆盖
TIMEOUT="${WAIT_TIMEOUT:-300}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --timeout) TIMEOUT="${2:-300}"; shift 2 ;;
    --port)    PORT_OVERRIDE="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

# D235：绝对路径定位仓库根（当前 shell 的 cwd 可能已被删除，见 up.sh 注释）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd -P)" || {
  echo "✗ 无法定位脚本所在目录（当前 shell 的工作目录可能已被删除）。" >&2
  echo "  解决：cd ~ 后重新执行 bash ~/TestOPS/deploy/wait-ready.sh" >&2
  exit 1
}
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd -P)" || exit 1
cd "$ROOT_DIR" || exit 1

# 端口来源与 compose 同一真源：.env 的 PLATFORM_PORT（未设置则 8848）
PORT="${PORT_OVERRIDE:-8848}"
if [[ -z "${PORT_OVERRIDE:-}" && -f .env ]]; then
  _p="$(sed -n 's/^PLATFORM_PORT=//p' .env 2>/dev/null | tail -1 | sed 's/["'"'"']//g' | tr -d '[:space:]')"
  [[ -n "${_p:-}" ]] && PORT="$_p"
fi
URL="http://127.0.0.1:${PORT}"

echo "等待平台就绪（最多 ${TIMEOUT}s，探测 ${URL}/api/health）…"
START=$(date +%s)
LAST=""
while :; do
  if curl -sf -m 5 "${URL}/api/health" >/dev/null 2>&1; then
    COST=$(( $(date +%s) - START ))
    echo "✓ 平台已就绪（耗时 ${COST}s）"
    echo
    echo "── 访问信息 ─────────────────────────────────────"
    echo "  地址：${URL}"
    echo "  管理员账号：admin"
    # D235：只匹配「初始密码」这行——此前用 `grep 管理员` 会误抓其它含"请联系管理员"
    # 的业务告警日志（真机实测：抓到了 chat_suggest 的 LLM 503 警告），用户以为那是密码。
    _pwd_line="$({ docker logs aiops-platform 2>&1 || true; } | grep -E "初始密码 *=" | tail -1)"
    if [[ -n "${_pwd_line}" ]]; then
        echo "  初始密码（首次启动时打印）："
        echo "    ${_pwd_line##*初始密码 = }" | sed 's/（.*$//' | sed 's/^/    /'
    else
        echo "  初始密码：本次启动**没有**打印（管理员已存在 / 日志已轮转）"
        echo "    → 拿回密码：bash deploy/reset-admin-password.sh"
    fi
    echo
    echo "── 容器状态 ─────────────────────────────────────"
    { docker ps --filter "name=aiops-" --format "{{.Names}} | {{.Status}}" || true; } | sed 's/^/  /'
    echo
    echo "  换机器访问：把 127.0.0.1 换成服务器 IP，并放行端口 ${PORT}（云上安全组）。"
    exit 0
  fi
  NOW=$(date +%s)
  ELAPSED=$(( NOW - START ))
  if (( ELAPSED > TIMEOUT )); then
    echo "✗ ${TIMEOUT}s 内未就绪（最后一次探测失败）" >&2
    echo "  排查：docker logs aiops-seekdb --tail 30   # 数据库是否还在首次初始化/反复重启" >&2
    echo "        docker logs aiops-platform --tail 30  # 平台是否卡在迁移或等待数据库" >&2
    echo "        内存不足是数据库起不来的最常见原因：free -m 看 available（需 ≥1.2G）" >&2
    exit 1
  fi
  # 进度提示：每 15 秒一行，避免刷屏
  if (( ELAPSED % 15 == 0 )) && [[ "${LAST}" != "${ELAPSED}" ]]; then
    LAST="${ELAPSED}"
    echo "  …已等待 ${ELAPSED}s（首次启动要等数据库初始化，属正常）"
  fi
  sleep 2
done
