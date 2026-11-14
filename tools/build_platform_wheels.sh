#!/usr/bin/env bash
# build_platform_wheels.sh —— 生成平台依赖的离线 wheel 包（D235 开源部署加速）
#
# 背景：开源用户从源码 `docker compose build` 时，pip 要从镜像源下载 ~35MB 依赖；
# 慢源（实测 91 kB/s）下光这一步就十几分钟，用户反馈"构建卡住"。
# 本脚本在**网络好的机器**上把 wheel 下好放进 deploy/wheels/，构建时 pip 走
# --no-index --find-links，全程不联网 → 构建只剩拷贝层，秒级。
#
# 用法：
#   bash tools/build_platform_wheels.sh
#   WITH_AI_DEPS=1 bash tools/build_platform_wheels.sh     # 连带「孤立森林」依赖
#   MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple bash tools/build_platform_wheels.sh
#
# 关键实现：**不手工指定 --platform/--python-version**——很多包（greenlet 3.5.6 是
# manylinux_2_24、cryptography 是 manylinux_2_34…）的平台标签比 manylinux2014 新，
# 限定窄了会导致"Could not find a version"（D235 实测踩到）。正确姿势是**在目标
# 平台的 Python 里下**：linux x86_64 直接用容器（python:3.11-slim），其余平台同理。
set -euo pipefail
cd "$(dirname "$0")/.."

DEST="deploy/wheels"
MIRROR="${MIRROR:-https://mirrors.aliyun.com/pypi/simple}"
PY_VER="${PY_VER:-3.11}"
mkdir -p "$DEST"

# 在 linux x86_64 容器里跑 pip download —— 平台标签天然匹配（容器即目标平台）
docker_pip() { # $@ = pip download 参数
  local img="docker.m.daocloud.io/library/python:${PY_VER}-slim"
  if ! command -v docker >/dev/null 2>&1; then
    echo "[!] 没有 docker，改用本机 python3（平台标签需与目标一致，否则会缺包）" >&2
    return 1
  fi
  docker run --rm -v "$(pwd)/$DEST":/w -v "$(pwd)/backend":/src:ro \
    --entrypoint python3 "$img" -m pip download -q -i "$MIRROR" \
    --only-binary=:all: -d /w "$@"
}

echo "==> 下载平台依赖 wheel（镜像源：$MIRROR，目标平台：linux x86_64 / cp${PY_VER//./}）"
docker_pip -r /src/requirements.txt || {
  echo "[!] 二进制 wheel 下载失败，回退源码包模式（pip 本地编译，构建更慢）" >&2
  docker run --rm -v "$(pwd)/$DEST":/w -v "$(pwd)/backend":/src:ro \
    --entrypoint python3 "docker.m.daocloud.io/library/python:${PY_VER}-slim" \
    -m pip download -q -i "$MIRROR" -d /w -r /src/requirements.txt
}

echo "==> 可选增强（scikit-learn → 孤立森林算法；不装则自动降级 3σ）"
if [ "${WITH_AI_DEPS:-0}" = "1" ]; then
  docker_pip -r /src/requirements-ai.txt || echo "  （可选依赖下载失败，不影响主流程）"
fi

echo "==> 产物：$(ls -1 "$DEST"/*.whl 2>/dev/null | wc -l) 个 wheel，$(du -sh "$DEST" | cut -f1)"
ls -lh "$DEST"/*.whl 2>/dev/null | head -12
cat <<'EOF'

下一步：
  1) 把整个仓库（含 deploy/wheels/）拷到目标机；
  2) 目标机构建（全程不联网）：
       docker compose -f deploy/docker-compose.yaml build platform
     Dockerfile 检测到 deploy/wheels/*.whl → pip 走 --no-index --find-links。
EOF
