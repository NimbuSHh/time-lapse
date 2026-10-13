#!/bin/zsh
set -eu
export PYTHONDONTWRITEBYTECODE=1
cd -- "$(dirname -- "$0")"
if [[ ! -x .venv/bin/python ]]; then
  print '尚未安装运行环境。请按 README.md 的首次安装步骤操作。'
  read '?按回车退出。'
  exit 1
fi
exec .venv/bin/python -m timelapse.server --port 0 --root "$PWD" "$@"
