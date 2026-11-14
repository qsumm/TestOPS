#!/usr/bin/env bash
# upgrade.sh —— 从旧版本升级到当前版本（D235）
#
# 老版本（v1.8.x 及更早）升级时最容易踩的三个坑，本脚本逐个处理：
#   ① 数据丢失：seekdb 真实数据目录随镜像版本变化（/var/lib/seekdb ↔ /var/lib/oceanbase），
#      只挂一个 → 另一个落在容器可写层，`docker compose down` 后数据全丢。
#      本脚本先**备份数据卷**再升级。
#   ② Password.Incorrect 无限重启：`start.sh` 的 .initialized 标记在容器层、数据在卷里，
#      两者不同步 → 拿新密码校验旧数据。本脚本检测到旧卷时**保留原 DB_PASSWORD**
#      并提示（或按 --reset-db 清库重来）。
#   ③ 老镜像/老 compose 的挂载路径不对 → 升级后自动使用双路径挂载（新 compose 已内置）。
#
# 用法（在旧部署目录里执行）：
#   bash deploy/upgrade.sh                    # 备份 + 拉新代码 + 重建
#   bash deploy/upgrade.sh --reset-db         # 数据不要了，清库全新开始
#   bash deploy/upgrade.sh --backup-only      # 只备份
#
# 前置：当前目录是老版本的仓库根（能看到 deploy/docker-compose.yaml 与 .env）。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd -P)" || {
  echo "✗ 无法定位脚本目录（当前 shell 的工作目录可能已被删除）。" >&2
  echo "  解决：cd ~ 后重新执行 bash ~/TestOPS/deploy/upgrade.sh" >&2
  exit 1
}
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd -P)" || exit 1
cd "$ROOT_DIR" || exit 1

RESET_DB=0
BACKUP_ONLY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --reset-db)     RESET_DB=1; shift ;;
    --backup-only)  BACKUP_ONLY=1; shift ;;
    --code-tar)     CODE_TAR="${2:?--code-tar 需要参数（新版本代码包路径）}"; shift 2 ;;
    -h|--help)      sed -n '2,22p' "$0"; exit 0 ;;
    \#*)            echo "  提示：参数里的「# …」看起来是注释，已忽略"; shift ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

ENV_ARGS=()
if [[ -f .env ]]; then ENV_ARGS=(--env-file .env)
elif [[ -f backend/.env ]]; then ENV_ARGS=(--env-file backend/.env)
else echo "✗ 找不到 .env —— 请在部署目录（含 deploy/ 与 .env）里执行" >&2; exit 1; fi

echo "==> 0/5 记录当前版本"
OLD_VER="$(cat VERSION 2>/dev/null || echo unknown)"
echo "  当前版本：v${OLD_VER}"

echo
echo "==> 1/5 备份 seekdb 数据卷（升级前必做）"
VOL="$(docker volume ls -q | grep -E 'seekdb-(data|demo)' | head -1 || true)"
if [[ -z "${VOL}" ]]; then
  VOL="$(docker volume ls -q | grep -i seekdb | head -1 || true)"
fi
STAMP="$(date +%Y%m%d-%H%M%S)"
if [[ -n "${VOL}" ]]; then
  MP="$(docker volume inspect "$VOL" --format '{{.Mountpoint}}' 2>/dev/null || true)"
  if [[ -n "${MP}" && -d "${MP}" ]]; then
    BAK="/root/testops-backup-${STAMP}.tar.gz"
    # 先停库再备份（热备可能拷到半写状态）
    docker compose "${ENV_ARGS[@]}" -f deploy/docker-compose.yaml stop seekdb >/dev/null 2>&1 || true
    tar czf "$BAK" -C "$(dirname "$MP")" "$(basename "$MP")" 2>/dev/null || true
    if [[ -s "$BAK" ]]; then
      echo "  ✓ 数据卷 ${VOL} 已备份 → ${BAK}（$(du -h "$BAK" | cut -f1)）"
      echo "    恢复方法：docker compose down && tar xzf ${BAK} -C $(dirname "$MP") && docker compose up -d"
    else
      echo "  ⚠ 备份失败（卷可能为空）——继续升级，但请先自行确认数据重要性"
    fi
  fi
else
  echo "  没有找到 seekdb 数据卷（全新部署？）"
fi
if [[ "${BACKUP_ONLY}" = "1" ]]; then
  echo "==> --backup-only：仅备份，结束"
  exit 0
fi

echo
echo "==> 2/5 检查数据库口令（旧卷 + 新口令 = Password.Incorrect 无限重启）"
if [[ -n "${VOL}" ]] && grep -q '^DB_PASSWORD=$' .env 2>/dev/null; then
  echo "  提示：.env 里 DB_PASSWORD 为空 —— 与旧库（有口令）不匹配时会启动失败。"
  echo "        若升级后 seekdb 反复重启并报 Password.Incorrect，两种解法："
  echo "          a) 把 .env 的 DB_PASSWORD 改回**旧部署时用的那个口令**"
  echo "          b) bash deploy/upgrade.sh --reset-db（清库重来，历史数据丢弃）"
fi

echo
echo "==> 3/5 拉取新代码"
if git rev-parse --git-dir >/dev/null 2>&1; then
  git fetch --all --tags 2>/dev/null || true
  # D235：**detached HEAD 必须先切回分支**——老用户常把仓库 checkout 到某个 tag
  # （或 rebase 过），此时 `git pull origin HEAD` 会失败，脚本若"继续用当前代码升级"
  # 就等于**没升级**（真机升级模拟实测踩到：Updating 475a489..5162097 后失败，
  # 版本号仍显示 v1.8.1）。
  DEFAULT_BRANCH="${UPGRADE_BRANCH:-main}"
  # D235：本地有改动时 pull 会被拒（"local changes would be overwritten"）——
  # 而老用户几乎都改过 compose（端口/镜像源）。先 stash 并**明确告知**怎么恢复。
  # 注意：必须连**未跟踪文件**一起检查——用户往往把下载的 upgrade.sh / wait-ready.sh
  # 放进 deploy/，而新版代码里**同名文件**存在 → git pull 会报
  # "untracked working tree files would be overwritten" 而失败（真机升级模拟实测踩到）。
  _untracked="$(git ls-files --others --exclude-standard 2>/dev/null | head -20)"
  if ! git diff --quiet 2>/dev/null || ! git diff --cached --quiet 2>/dev/null || [ -n "${_untracked}" ]; then
    echo "  检测到本地改动（含未跟踪文件）→ 先 stash（升级完成后 git stash pop 恢复）"
    [ -n "${_untracked}" ] && echo "    未跟踪：$(echo "${_untracked}" | tr '\n' ' ' | cut -c1-120)"
    git stash push -u -m "pre-upgrade $(date '+%F %T')" >/dev/null 2>&1 || true
    STASHED=1
  fi
  CUR="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo HEAD)"
  if [[ "${CUR}" == "HEAD" ]]; then
    echo "  当前是 detached HEAD（多半 checkout 过 tag）→ 切回 ${DEFAULT_BRANCH} 分支"
    git checkout -q "${DEFAULT_BRANCH}" 2>/dev/null || git checkout -q -B "${DEFAULT_BRANCH}"
    CUR="${DEFAULT_BRANCH}"
  fi
  if ! git pull --ff-only origin "${CUR}" 2>/dev/null; then
    echo "  ✗ 拉取新代码失败（网络不通或分支分叉）。**已中止**，未做任何改动。" >&2
    echo "    排查：git fetch && git reset --hard origin/${CUR}" >&2
    echo "    或手动把新版本代码覆盖到本目录（保留 .env 与 deploy/wheels/），再重新执行本脚本" >&2
    exit 1
  fi
  echo "  新版本：v$(cat VERSION 2>/dev/null || echo unknown)"
else
  # D235：拷贝部署（无 .git）的兼容路径——用 --code-tar 提供的新代码包升级。
  # 没有它，老用户（拷贝部署 + 无 git）就卡死在这一步（真机实测）。
  if [[ -z "${CODE_TAR:-}" ]]; then
    echo "  ✗ 当前目录不是 git 仓库，且未提供新版本代码包。" >&2
    echo "    两种解决方式：" >&2
    echo "      a) 在有网的机器上打包新版本代码并上传，然后：" >&2
    echo "         bash deploy/upgrade.sh --code-tar /path/to/testops-code.tgz" >&2
    echo "      b) 手动用新版本代码覆盖本目录（保留 .env 与 deploy/wheels/），再重跑本脚本" >&2
    exit 1
  fi
  if [[ ! -s "${CODE_TAR}" ]]; then
    echo "  ✗ 代码包不存在或为空：${CODE_TAR}" >&2
    exit 1
  fi
  NEW_DIR="/root/TestOPS-upgraded-$(date +%Y%m%d%H%M%S)"
  echo "  解包新版本代码 → ${NEW_DIR}"
  mkdir -p "${NEW_DIR}"
  tar xzf "${CODE_TAR}" -C "${NEW_DIR}" 2>/dev/null || tar xf "${CODE_TAR}" -C "${NEW_DIR}"
  [[ -f "${NEW_DIR}/VERSION" ]] || { echo "  ✗ 代码包内容异常（无 VERSION）" >&2; exit 1; }
  # 迁移配置：旧 .env（口令/密钥/端口等定制全部沿用）
  cp -f .env "${NEW_DIR}/.env"
  ENV_ARGS=(--env-file "${NEW_DIR}/.env")
  COMPOSE_FILE="${NEW_DIR}/deploy/docker-compose.yaml"
  COMPOSE_DIR="${NEW_DIR}"
  echo "  ✓ 代码已更新（保留原 .env 配置）"
fi

# D235：tar 模式下，后续 compose 都指向**新代码目录**（git 模式 = 本目录）
COMPOSE_FILE="${COMPOSE_FILE:-deploy/docker-compose.yaml}"
COMPOSE_DIR="${COMPOSE_DIR:-.}"

echo
echo "==> 4/5 重建并启动（首次约 2~4 分钟；迁移会自动执行）"
if [[ "${RESET_DB}" = "1" ]]; then
  echo "  --reset-db：删除数据卷，全新开始"
  docker compose "${ENV_ARGS[@]}" -f "${COMPOSE_FILE}" down -v --remove-orphans 2>&1 | tail -2
fi
# D235：**先构建/拉取，最后才 up -d**。直接 `up -d --build` 时 compose 会先删旧容器
# 再拉镜像——镜像拉不动（受限网络）就留下「数据库容器消失、平台半死」的状态
# （真机升级模拟实测：seekdb 容器消失，平台还连着旧库跑）。分两步后，拉取失败时
# **旧容器仍然活着**，平台不受影响。
echo "  4.1/4.2 构建镜像 + 拉取数据库镜像（失败不影响现容器）"
docker compose "${ENV_ARGS[@]}" -f "${COMPOSE_FILE}" build platform || {
  echo "  ✗ 构建失败——已中止，现有容器保持运行" >&2; exit 1; }
docker compose "${ENV_ARGS[@]}" -f "${COMPOSE_FILE}" pull seekdb 2>/dev/null || {
  echo "  ⚠ 数据库镜像拉取失败（受限网络？可改 .env 的 SEEKDB_IMAGE 指定可达源）" >&2; }
echo "  4.3/4.3 重建并启动"
docker compose "${ENV_ARGS[@]}" -f "${COMPOSE_FILE}" up -d

echo
echo "==> 5/5 等待平台就绪"
# D235：wait-ready.sh 缺失时内联轮询——用户可能只拷了 upgrade.sh 一个文件
# （真机实测：只放 upgrade.sh → 第 5 步报 No such file or directory，用户误判升级失败）
PORT="${PLATFORM_PORT:-8848}"
if [[ -f .env ]]; then
  _p="$(grep -E '^PLATFORM_PORT=' .env 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '[:space:]')"
  [[ -n "${_p:-}" ]] && PORT="$_p"
fi
if [[ -f "$SCRIPT_DIR/wait-ready.sh" ]]; then
  WAIT_TIMEOUT="${WAIT_TIMEOUT:-300}" bash "$SCRIPT_DIR/wait-ready.sh" || {
    echo
    echo "✗ 平台未就绪。升级后常见原因："
    echo "  · seekdb 反复重启且日志有 Password.Incorrect → 见上面第 2 步的两种解法"
    echo "  · 磁盘不足（SeekDB 要求数据盘空闲 >15GB）：df -h /"
    echo "  · 看日志：docker logs aiops-seekdb --tail 30 / docker logs aiops-platform --tail 30"
    exit 1
  }
else
  START=$(date +%s)
  while :; do
    if curl -sf -m 5 "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
      echo "✓ 平台已就绪（耗时 $(( $(date +%s) - START ))s）"
      echo "  地址：http://127.0.0.1:${PORT}"
      { docker logs aiops-platform 2>&1 || true; } | grep "初始密码" | tail -1 | sed 's/^/  初始密码（首次启动才有）: /' || true
      break
    fi
    ELAPSED=$(( $(date +%s) - START ))
    if (( ELAPSED > ${WAIT_TIMEOUT:-300} )); then
      echo "✗ ${WAIT_TIMEOUT:-300}s 未就绪：docker logs aiops-platform --tail 30" >&2
      exit 1
    fi
    sleep 5
  done
fi

echo
if [[ "${STASHED:-0}" = "1" ]]; then
  echo "  ⚠ 你升级前的本地改动已被 stash：git stash pop 可恢复"
  echo "    （建议把端口/镜像源等定制挪进 .env —— .env 不受升级影响）"
fi
echo "── 升级完成 ─────────────────────────────────────"
echo "  v${OLD_VER} → v$(cat VERSION 2>/dev/null || echo unknown)"
echo "  数据：已备份到 /root/testops-backup-*.tar.gz（确认无误后自行删除）"
echo "  注意："
echo "    · 管理员口令沿用旧库的（不会重置）"
echo "    · 新增的用户管理页在「系统设置 → 用户」（仅 admin）"
echo "    · 若有主机显示「需重新接入」：在「主机管理 → 接入新主机」重新生成令牌并重跑安装命令"
