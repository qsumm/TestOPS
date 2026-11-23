#!/usr/bin/env bash
# build_agent_wheels.sh —— 生成 Agent 依赖 wheel 清单（D235 内网兜底）
#
# 用途：目标机无公网（连 pypi.org 都 Errno 101）时，install.sh 的终极兜底是
#   `pip install --no-index --find-links http://<平台>/wheels/ psutil httpx`
# 本脚本把 psutil/httpx 及其全部依赖的 wheel 拉到 agent/wheels/，随镜像分发。
#
# 在**平台机**上执行（需要有公网或国内镜像出网，仅装机时跑一次）：
#   bash tools/build_agent_wheels.sh
#
# 说明：
# · psutil 是 C 扩展，wheel 必须按 解释器版本 × ABI × 平台 分列——覆盖 install.sh
#   会挑的 python3.9~3.13 × x86_64 manylinux（aarch64 场景在此清单追加 --platform manylinux2014_aarch64）。
# · httpx 及依赖是纯 py wheel（py3-none-any），一份即可。
# · 清单走 git 忽略（大体积二进制不进仓库）；部署后跑一次本脚本即可。
set -euo pipefail
cd "$(dirname "$0")/.."

DEST="agent/wheels"
mkdir -p "$DEST"

# pip download 的宿主解释器无关紧要：目标版本全用 --python-version/--platform/--abi 指定
MIRROR="${MIRROR:-https://mirrors.aliyun.com/pypi/simple/}"
DL() { # $1=输出目录参数附加串，$2=包名
  python3 -m pip download -i "$MIRROR" --only-binary=:all: -d "$DEST" "$@" >/dev/null 2>&1 \
    || python3 -m pip download --only-binary=:all: -d "$DEST" "$@" >/dev/null 2>&1 \
    || { echo "  [失败] $*"; return 1; }
  echo "  [ok] $*"
}

echo "==> psutil（C 扩展，按解释器 × ABI 分列）"
for pyver_abi in 39 cp39 310 cp310 311 cp311 312 cp312 313 cp313; do
  pv="${pyver_abi%% *}"; abi="${pyver_abi##* }"
  DL --python-version "$pv" --platform manylinux2014_x86_64 --platform manylinux_2_17_x86_64 \
     --implementation cp --abi "$abi" --no-deps "psutil==7.2.2" || true
done

echo "==> httpx 及纯 py 依赖（universal wheel，一份覆盖全部解释器）"
DL --python-version 39 "httpx==0.28.1" \
  || DL --python-version 39 --no-deps "httpx==0.28.1" \
  || DL --python-version 39 --no-deps "httpx==0.28.1" "anyio" "httpcore" "h11" "sniffio" "idna" "certifi" "exceptiongroup"

echo "==> 产物"
ls -lh "$DEST" | tail -n +2
echo "完成。重启平台后 ${0%/tools/build_agent_wheels.sh} 的 /wheels/ 即提供清单页。"
