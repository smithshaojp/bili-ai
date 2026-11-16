#!/bin/bash
# B站视频AI助手 - Mac 启动脚本（双击或在终端运行 bash start.command）
cd "$(dirname "$0")" || exit 1

PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1; then
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then PY="$c"; break; fi
  fi
done
if [ -z "$PY" ]; then
  echo "没有找到 Python 3.9 以上版本。"
  echo "请先安装：终端运行  xcode-select --install   或   brew install python"
  read -r -p "按回车退出"; exit 1
fi

PIP_ARGS=()
if [ -n "$PIP_MIRROR" ]; then PIP_ARGS=(-i "$PIP_MIRROR"); fi

if [ ! -d .venv ]; then
  echo "首次运行：创建虚拟环境（用 $PY）…"
  "$PY" -m venv .venv || { echo "创建虚拟环境失败"; read -r -p "按回车退出"; exit 1; }
fi
# shellcheck disable=SC1091
source .venv/bin/activate

if [ ! -f .venv/.installed ] || [ requirements.txt -nt .venv/.installed ]; then
  echo "安装依赖（只有第一次比较慢）…"
  python -m pip install -q --upgrade pip "${PIP_ARGS[@]}"
  python -m pip install -q -r requirements.txt "${PIP_ARGS[@]}" || {
    echo ""; echo "依赖安装失败。如果是网络问题，可以用国内镜像重试："
    echo "  PIP_MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple bash start.command"
    read -r -p "按回车退出"; exit 1; }
  if [ "$(uname -m)" = "arm64" ]; then
    echo "检测到 Apple 芯片，安装 mlx-whisper（GPU 加速转写）…"
    python -m pip install -q mlx-whisper "${PIP_ARGS[@]}" || echo "mlx-whisper 安装失败，将使用 CPU 转写（不影响使用）"
  fi
  touch .venv/.installed
fi

PORT="${BILIAI_PORT:-8765}"
( sleep 2; open "http://127.0.0.1:$PORT" ) &
python -m app.server
