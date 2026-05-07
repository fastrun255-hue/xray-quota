#!/usr/bin/env bash
set -e

mkdir -p /app/config /etc/xray /etc/sing-box /data

echo "[start] checking required config files"

SINGBOX_CONFIG_PATH="${SINGBOX_CONFIG:-/app/config/sing-box.json}"
XRAY_TEMPLATE_PATH="${XRAY_TEMPLATE:-/app/config/xray-template.json}"
QUOTA_CONFIG_PATH="${QUOTA_CONFIG:-/app/config/quota.json}"
COMBINED_CONFIG_PATH="${COMBINED_CONFIG:-/app/config/config.json}"

if [ -f "$SINGBOX_CONFIG_PATH" ] && [ -f "$XRAY_TEMPLATE_PATH" ] && [ -f "$QUOTA_CONFIG_PATH" ]; then
  echo "[start] using separate config files"
elif [ -f "$COMBINED_CONFIG_PATH" ]; then
  echo "[start] using combined config file: $COMBINED_CONFIG_PATH"
else
  echo "[error] missing config"
  echo "[error] provide either:"
  echo "[error]   $SINGBOX_CONFIG_PATH"
  echo "[error]   $XRAY_TEMPLATE_PATH"
  echo "[error]   $QUOTA_CONFIG_PATH"
  echo "[error] or combined config:"
  echo "[error]   $COMBINED_CONFIG_PATH"

  exit 1
fi

echo "[start] starting quota controller"
exec python3 /app/quota-controller.py
