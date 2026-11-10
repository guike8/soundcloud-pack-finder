#!/bin/zsh
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install -e '.[telegram]'
fi
if [ "$#" -eq 0 ]; then
  .venv/bin/packfinder doctor
else
  .venv/bin/packfinder "$@"
fi
