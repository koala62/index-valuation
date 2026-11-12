#!/usr/bin/env sh
# Uppcoder 启动脚本：默认会执行 uvicorn main:app，本项目入口在 backend/app.py
set -eu
cd "$(dirname "$0")/backend"
exec uvicorn app:app --host 0.0.0.0 --port "${PORT:-8000}"
