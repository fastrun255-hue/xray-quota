#!/bin/sh
set -eu

mkdir -p /app/config /etc/sing-box /run/sing-box /data

if ulimit -n 1048576 2>/dev/null; then
  echo "[start] file descriptor limit set to $(ulimit -n)"
else
  echo "[warn] could not raise file descriptor limit"
fi

echo "[start] starting sing-box quota controller"
exec python3 /app/quota-controller.py
