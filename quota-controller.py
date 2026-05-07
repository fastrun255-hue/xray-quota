#!/usr/bin/env python3

import copy
import base64
import hashlib
import html
import json
import os
import signal
import shutil
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import yaml


SINGBOX_CONFIG = os.environ.get("SINGBOX_CONFIG", "/app/config/sing-box.json")
SINGBOX_GENERATED_CONFIG = os.environ.get("SINGBOX_GENERATED_CONFIG", "/etc/sing-box/config.json")
SINGBOX_LAST_GOOD_CONFIG = os.environ.get("SINGBOX_LAST_GOOD_CONFIG", "/etc/sing-box/config.last-good.json")
XRAY_TEMPLATE = os.environ.get("XRAY_TEMPLATE", "/app/config/xray-template.json")
QUOTA_CONFIG = os.environ.get("QUOTA_CONFIG", "/app/config/quota.json")
COMBINED_CONFIG = os.environ.get("COMBINED_CONFIG", "/app/config/config.json")
CUSTOM_CONFIG = os.environ.get("CUSTOM_CONFIG", "/app/config/custom-config.yaml")
YAML_CONFIG = os.environ.get("YAML_CONFIG", "/app/config/config.yaml")
CONFIG_MODE = os.environ.get("CONFIG_MODE", "auto")
RUNTIME_CONFIG_DIR = os.environ.get("RUNTIME_CONFIG_DIR", "/run/xray-quota")
XRAY_GENERATED_CONFIG = os.environ.get("XRAY_GENERATED_CONFIG", "/etc/xray/config.json")
XRAY_LAST_GOOD_CONFIG = os.environ.get("XRAY_LAST_GOOD_CONFIG", "/etc/xray/config.last-good.json")
STATE_FILE = os.environ.get("STATE_FILE", "/data/usage-state.json")
XRAY_API_SERVER = os.environ.get("XRAY_API_SERVER", "127.0.0.1:10085")
XRAY_INBOUND_TAG = os.environ.get("XRAY_INBOUND_TAG", "vless-in")
XRAY_API_MAX_FAILURES = int(os.environ.get("XRAY_API_MAX_FAILURES", "5"))
XRAY_STARTUP_GRACE_SECONDS = float(os.environ.get("XRAY_STARTUP_GRACE_SECONDS", "1"))
SINGBOX_STARTUP_GRACE_SECONDS = float(os.environ.get("SINGBOX_STARTUP_GRACE_SECONDS", "1"))
ENABLE_HTTP_FRONTEND = os.environ.get("ENABLE_HTTP_FRONTEND", "true").lower() in ["1", "true", "yes", "on"]
PUBLIC_HTTP_PORT = int(os.environ.get("PUBLIC_HTTP_PORT", "8080"))
XRAY_PROXY_HOST = os.environ.get("XRAY_PROXY_HOST", "127.0.0.1")
XRAY_PROXY_PORT = int(os.environ.get("XRAY_PROXY_PORT", "10000"))
XRAY_WS_PATH = os.environ.get("XRAY_WS_PATH", "").strip()
NGINX_GENERATED_CONFIG = os.environ.get("NGINX_GENERATED_CONFIG", "/etc/nginx/http.d/xray-quota.conf")
QUOTA_UI_HOST = os.environ.get("QUOTA_UI_HOST", "0.0.0.0")
QUOTA_UI_PORT = int(os.environ.get("QUOTA_UI_PORT", "9090"))
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
QUOTA_USER_RESERVED_FIELDS = {"uuid", "daily_limit_bytes", "reset_interval_hours", "level", "client"}


class XrayConfigError(Exception):
    pass


class XrayStatsError(Exception):
    pass


class XrayProcessError(Exception):
    pass


class SingBoxConfigError(Exception):
    pass


class SingBoxProcessError(Exception):
    pass


def log(message: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    print(f"[{now}] {message}", flush=True)


def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_json_or_yaml(path: str) -> Dict[str, Any]:
    suffix = Path(path).suffix.lower()
    with open(path, "r", encoding="utf-8") as f:
        if suffix in [".yaml", ".yml"]:
            data = yaml.safe_load(f)
        else:
            data = json.load(f)

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON/YAML object")

    return data


def save_json_atomic(path: str, data: Dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    tmp.replace(target)


def bytes_to_mb(value: int) -> float:
    return round(int(value) / (1024 * 1024), 2)


def seconds_to_hr_min(value: int) -> str:
    seconds = max(0, int(value))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"{hours:02d}:{minutes:02d}"


def file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def combined_file_hash(paths: List[str]) -> str:
    h = hashlib.sha256()
    for path in paths:
        h.update(path.encode("utf-8"))
        h.update(b"\0")
        h.update(file_hash(path).encode("ascii"))
        h.update(b"\0")
    return h.hexdigest()


def required_separate_config_files_exist() -> bool:
    return all(os.path.exists(path) for path in [SINGBOX_CONFIG, XRAY_TEMPLATE, QUOTA_CONFIG])


def find_combined_config() -> Optional[str]:
    for path in [COMBINED_CONFIG, CUSTOM_CONFIG, YAML_CONFIG]:
        if os.path.exists(path):
            return path

    return None


def get_config_section(bundle: Dict[str, Any], *names: str) -> Dict[str, Any]:
    for name in names:
        value = bundle.get(name)
        if value is not None:
            if not isinstance(value, dict):
                raise ValueError(f"combined config section `{name}` must be an object")
            return value

    raise ValueError(f"combined config is missing section `{names[0]}`")


def extract_combined_config() -> None:
    bundle = load_json_or_yaml(COMBINED_CONFIG)
    if not isinstance(bundle, dict):
        raise ValueError("combined config must be a JSON/YAML object")

    runtime_dir = Path(RUNTIME_CONFIG_DIR)
    runtime_dir.mkdir(parents=True, exist_ok=True)

    save_json_atomic(str(runtime_dir / "sing-box.json"), get_config_section(bundle, "sing-box", "sing_box"))
    save_json_atomic(
        str(runtime_dir / "xray-template.json"),
        get_config_section(bundle, "xray-template", "xray_template", "xray")
    )
    save_json_atomic(str(runtime_dir / "quota.json"), get_config_section(bundle, "quota"))


def configure_runtime_config_files() -> str:
    global SINGBOX_CONFIG, XRAY_TEMPLATE, QUOTA_CONFIG, COMBINED_CONFIG

    mode = CONFIG_MODE.lower()
    if mode not in ["auto", "separate", "combined"]:
        raise ValueError("CONFIG_MODE must be `auto`, `separate`, or `combined`")

    if mode in ["auto", "separate"] and required_separate_config_files_exist():
        log("[config] using separate config files")
        return "separate"

    if mode == "separate":
        raise FileNotFoundError("CONFIG_MODE=separate but one or more separate config files are missing")

    combined_config = find_combined_config()
    if combined_config is None:
        raise FileNotFoundError(
            "missing runtime config: provide separate sing-box/xray-template/quota files, config.json, or custom-config.yaml"
        )

    COMBINED_CONFIG = combined_config
    runtime_dir = Path(RUNTIME_CONFIG_DIR)
    SINGBOX_CONFIG = str(runtime_dir / "sing-box.json")
    XRAY_TEMPLATE = str(runtime_dir / "xray-template.json")
    QUOTA_CONFIG = str(runtime_dir / "quota.json")
    extract_combined_config()
    log("[config] using combined config file")
    return "combined"


def current_config_hash(config_mode: str) -> str:
    if config_mode == "combined":
        return file_hash(COMBINED_CONFIG)

    return combined_file_hash([SINGBOX_CONFIG, XRAY_TEMPLATE, QUOTA_CONFIG])


def now_ts() -> int:
    return int(time.time())


def default_state() -> Dict[str, Any]:
    ts = now_ts()
    return {
        "reset_started_at": ts,
        "users": {}
    }


def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        state = default_state()
        save_json_atomic(STATE_FILE, state)
        return state

    try:
        return load_json(STATE_FILE)
    except Exception as e:
        log(f"[warn] failed to read state file, creating a new state: {e}")
        state = default_state()
        save_json_atomic(STATE_FILE, state)
        return state


def normalize_quota(quota: Dict[str, Any]) -> Dict[str, Any]:
    quota.setdefault("reset_interval_hours", 24)
    quota.setdefault("check_interval_seconds", 60)
    quota.setdefault("users", {})
    quota["reset_interval_hours"] = int(quota["reset_interval_hours"])
    if quota["reset_interval_hours"] < 0:
        raise ValueError("quota.json field `reset_interval_hours` cannot be negative")

    if not isinstance(quota["users"], dict):
        raise ValueError("quota.json field `users` must be an object")

    for username, user in quota["users"].items():
        if not isinstance(user, dict):
            raise ValueError(f"quota user `{username}` must be an object")
        if ">>>" in username:
            raise ValueError(f"quota user `{username}` cannot contain `>>>`")
        if "uuid" not in user:
            raise ValueError(f"quota user `{username}` is missing uuid")
        if "daily_limit_bytes" not in user:
            raise ValueError(f"quota user `{username}` is missing daily_limit_bytes")
        if "client" in user and not isinstance(user["client"], dict):
            raise ValueError(f"quota user `{username}` field `client` must be an object")
        user.setdefault("level", 0)
        user["daily_limit_bytes"] = int(user["daily_limit_bytes"])
        if user["daily_limit_bytes"] <= 0:
            raise ValueError(f"quota user `{username}` daily_limit_bytes must be greater than zero")
        user["level"] = int(user.get("level", 0))
        if "reset_interval_hours" in user and user["reset_interval_hours"] is not None:
            user["reset_interval_hours"] = int(user["reset_interval_hours"])
            if user["reset_interval_hours"] < 0:
                raise ValueError(f"quota user `{username}` reset_interval_hours cannot be negative")

    return quota


def new_user_state(reset_started_at: Optional[int] = None) -> Dict[str, Any]:
    return {
        "used_bytes": 0,
        "last_xray_total_bytes": 0,
        "disabled": False,
        "disabled_at": None,
        "reset_started_at": int(reset_started_at or now_ts())
    }


def ensure_state_users(state: Dict[str, Any], quota: Dict[str, Any]) -> bool:
    changed = False
    state.setdefault("users", {})
    inherited_reset_started_at = int(state.get("reset_started_at", now_ts()))

    for username in quota["users"].keys():
        if username not in state["users"]:
            state["users"][username] = new_user_state(inherited_reset_started_at)
            changed = True
        elif "reset_started_at" not in state["users"][username]:
            state["users"][username]["reset_started_at"] = inherited_reset_started_at
            changed = True

    return changed


def user_reset_interval_hours(user: Dict[str, Any], quota: Dict[str, Any]) -> int:
    value = user.get("reset_interval_hours", quota.get("reset_interval_hours", 24))
    if value is None:
        return 0
    return int(value)


def should_reset_user(user_state: Dict[str, Any], user: Dict[str, Any], quota: Dict[str, Any]) -> bool:
    interval_hours = user_reset_interval_hours(user, quota)
    if interval_hours <= 0:
        return False

    reset_started_at = int(user_state.get("reset_started_at", state_reset_started_at_fallback()))
    return now_ts() - reset_started_at >= interval_hours * 3600


def state_reset_started_at_fallback() -> int:
    return now_ts()


def reset_remaining_seconds_for_user(user_state: Dict[str, Any], user: Dict[str, Any], quota: Dict[str, Any]) -> Optional[int]:
    interval_hours = user_reset_interval_hours(user, quota)
    if interval_hours <= 0:
        return None

    reset_started_at = int(user_state.get("reset_started_at", now_ts()))
    next_reset_at = reset_started_at + interval_hours * 3600
    return max(0, next_reset_at - now_ts())


def reset_user_state(state: Dict[str, Any], username: str) -> bool:
    previous_disabled = bool(state.get("users", {}).get(username, {}).get("disabled", False))
    state["users"][username] = new_user_state()
    return previous_disabled


def reset_due_users(state: Dict[str, Any], quota: Dict[str, Any]) -> Tuple[bool, bool]:
    state_changed = False
    disabled_changed = False

    for username, user in quota["users"].items():
        user_state = state["users"].setdefault(username, new_user_state())
        if should_reset_user(user_state, user, quota):
            interval_hours = user_reset_interval_hours(user, quota)
            log(f"[quota] reset started for {username}: interval_hours={interval_hours}")
            was_disabled = reset_user_state(state, username)
            state_changed = True
            disabled_changed = disabled_changed or was_disabled
            log(f"[quota] reset completed for {username}")

    return state_changed, disabled_changed


def build_clients(quota: Dict[str, Any], state: Dict[str, Any]) -> List[Dict[str, Any]]:
    clients = []

    for username, user in quota["users"].items():
        user_state = state.get("users", {}).get(username, {})
        if user_state.get("disabled", False):
            continue

        client = copy.deepcopy(user.get("client", {}))
        for key, value in user.items():
            if key not in QUOTA_USER_RESERVED_FIELDS:
                client[key] = value

        client.update({
            "id": user["uuid"],
            "email": username,
            "level": int(user.get("level", 0))
        })
        clients.append(client)

    return clients


def inject_clients_into_xray_template(template: Dict[str, Any], clients: List[Dict[str, Any]]) -> Dict[str, Any]:
    config = copy.deepcopy(template)
    inbounds = config.get("inbounds", [])

    found = False
    for inbound in inbounds:
        if inbound.get("tag") == XRAY_INBOUND_TAG:
            inbound.setdefault("settings", {})
            inbound["settings"]["clients"] = clients
            found = True
            break

    if not found:
        raise ValueError(f"xray-template.json has no inbound with tag `{XRAY_INBOUND_TAG}`")

    return config


def find_xray_inbound(config: Dict[str, Any]) -> Dict[str, Any]:
    for inbound in config.get("inbounds", []):
        if inbound.get("tag") == XRAY_INBOUND_TAG:
            return inbound

    raise ValueError(f"xray-template.json has no inbound with tag `{XRAY_INBOUND_TAG}`")


def detect_xray_ws_path(inbound: Dict[str, Any]) -> str:
    if XRAY_WS_PATH:
        path = XRAY_WS_PATH
    else:
        stream_settings = inbound.get("streamSettings", {})
        ws_settings = stream_settings.get("wsSettings", {})
        path = str(ws_settings.get("path", "/myvpn"))

    if not path.startswith("/"):
        path = f"/{path}"

    return path


def enable_local_xray_proxy(config: Dict[str, Any]) -> str:
    inbound = find_xray_inbound(config)
    ws_path = detect_xray_ws_path(inbound)
    inbound["listen"] = XRAY_PROXY_HOST
    inbound["port"] = XRAY_PROXY_PORT
    return ws_path


def ensure_xray_user_stats_enabled(config: Dict[str, Any], quota: Dict[str, Any]) -> None:
    config.setdefault("stats", {})
    policy = config.setdefault("policy", {})
    levels = policy.setdefault("levels", {})

    if not isinstance(levels, dict):
        raise ValueError("xray-template.json field `policy.levels` must be an object")

    for user in quota["users"].values():
        level = str(int(user.get("level", 0)))
        level_policy = levels.setdefault(level, {})

        if not isinstance(level_policy, dict):
            raise ValueError(f"xray-template.json field `policy.levels.{level}` must be an object")

        level_policy["statsUserUplink"] = True
        level_policy["statsUserDownlink"] = True


def write_xray_config_file(path: str, quota: Dict[str, Any], state: Dict[str, Any]) -> int:
    template = load_json(XRAY_TEMPLATE)
    clients = build_clients(quota, state)
    config = inject_clients_into_xray_template(template, clients)
    if ENABLE_HTTP_FRONTEND:
        enable_local_xray_proxy(config)
    ensure_xray_user_stats_enabled(config, quota)
    save_json_atomic(path, config)
    return len(clients)


def validate_xray_config(path: str) -> None:
    cmd = ["xray", "run", "-test", "-c", path]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except Exception as e:
        raise XrayConfigError(f"failed running xray config test: {e}") from e

    if result.returncode != 0:
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        raise XrayConfigError(stderr or stdout or f"xray config test failed with code {result.returncode}")


def write_validated_xray_config(quota: Dict[str, Any], state: Dict[str, Any]) -> None:
    candidate = f"{XRAY_GENERATED_CONFIG}.candidate.json"
    active_clients = write_xray_config_file(candidate, quota, state)
    validate_xray_config(candidate)
    Path(candidate).replace(XRAY_GENERATED_CONFIG)
    log(f"[xray] generated validated config with {active_clients} active client(s)")


def validate_singbox_config(path: str) -> None:
    cmd = ["sing-box", "check", "-c", path]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except Exception as e:
        raise SingBoxConfigError(f"failed running sing-box config check: {e}") from e

    if result.returncode != 0:
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        raise SingBoxConfigError(stderr or stdout or f"sing-box config check failed with code {result.returncode}")


def write_validated_singbox_config() -> None:
    candidate = f"{SINGBOX_GENERATED_CONFIG}.candidate.json"
    target = Path(candidate)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SINGBOX_CONFIG, candidate)
    validate_singbox_config(candidate)
    Path(candidate).replace(SINGBOX_GENERATED_CONFIG)
    log("[sing-box] generated validated config")


def nginx_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def write_nginx_config() -> None:
    template = load_json(XRAY_TEMPLATE)
    config = inject_clients_into_xray_template(template, [])
    ws_path = detect_xray_ws_path(find_xray_inbound(config))
    target = Path(NGINX_GENERATED_CONFIG)
    target.parent.mkdir(parents=True, exist_ok=True)
    body = f"""
map $http_upgrade $connection_upgrade {{
    default upgrade;
    '' close;
}}

server {{
    listen 0.0.0.0:{PUBLIC_HTTP_PORT};
    server_name _;

    location = /healthz {{
        access_log off;
        return 200 'ok\\n';
    }}

    location {nginx_escape(ws_path)} {{
        proxy_pass http://{XRAY_PROXY_HOST}:{XRAY_PROXY_PORT};
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }}

    location / {{
        proxy_pass http://127.0.0.1:{QUOTA_UI_PORT};
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }}
}}
""".lstrip()
    target.write_text(body, encoding="utf-8")
    log(f"[nginx] generated config on public port {PUBLIC_HTTP_PORT}; VPN path {ws_path}; quota UI on /")


def validate_nginx_config() -> None:
    result = subprocess.run(["nginx", "-t"], capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip() or "nginx config test failed")


def mark_current_xray_config_good() -> None:
    shutil.copyfile(XRAY_GENERATED_CONFIG, XRAY_LAST_GOOD_CONFIG)


def mark_current_singbox_config_good() -> None:
    shutil.copyfile(SINGBOX_GENERATED_CONFIG, SINGBOX_LAST_GOOD_CONFIG)


def start_xray_process(config_path: str = XRAY_GENERATED_CONFIG) -> subprocess.Popen:
    proc = start_process("xray", ["xray", "run", "-c", config_path])

    if XRAY_STARTUP_GRACE_SECONDS > 0:
        time.sleep(XRAY_STARTUP_GRACE_SECONDS)

    if proc.poll() is not None:
        raise XrayProcessError(f"xray exited during startup with code {proc.returncode}")

    return proc


def start_singbox_process(config_path: str = SINGBOX_GENERATED_CONFIG) -> subprocess.Popen:
    proc = start_process("sing-box", ["sing-box", "run", "-c", config_path])

    if SINGBOX_STARTUP_GRACE_SECONDS > 0:
        time.sleep(SINGBOX_STARTUP_GRACE_SECONDS)

    if proc.poll() is not None:
        raise SingBoxProcessError(f"sing-box exited during startup with code {proc.returncode}")

    return proc


def start_nginx_process() -> subprocess.Popen:
    proc = start_process("nginx", ["nginx", "-g", "daemon off;"])
    time.sleep(0.5)

    if proc.poll() is not None:
        raise RuntimeError(f"nginx exited during startup with code {proc.returncode}")

    return proc


def reload_nginx() -> None:
    result = subprocess.run(["nginx", "-s", "reload"], capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip() or "nginx reload failed")


def start_process(name: str, args: List[str]) -> subprocess.Popen:
    log(f"[process] starting {name}: {' '.join(args)}")
    return subprocess.Popen(args)


def stop_process(name: str, proc: Optional[subprocess.Popen], timeout: int = 10) -> None:
    if proc is None:
        return

    if proc.poll() is not None:
        return

    log(f"[process] stopping {name}")
    proc.terminate()

    try:
        proc.wait(timeout=timeout)
        log(f"[process] stopped {name}")
    except subprocess.TimeoutExpired:
        log(f"[process] killing {name}")
        proc.kill()
        proc.wait(timeout=timeout)


def restart_xray_from_generated_config(current_proc: Optional[subprocess.Popen]) -> subprocess.Popen:
    has_last_good = os.path.exists(XRAY_LAST_GOOD_CONFIG)
    stop_process("xray", current_proc)

    try:
        proc = start_xray_process(XRAY_GENERATED_CONFIG)
        mark_current_xray_config_good()
        return proc
    except XrayProcessError:
        if not has_last_good:
            raise

        log("[xray] new config failed at startup, rolling back to last-known-good config")
        shutil.copyfile(XRAY_LAST_GOOD_CONFIG, XRAY_GENERATED_CONFIG)
        proc = start_xray_process(XRAY_GENERATED_CONFIG)
        log("[xray] rollback to last-known-good config completed")
        return proc


def restart_xray(current_proc: Optional[subprocess.Popen], quota: Dict[str, Any], state: Dict[str, Any]) -> subprocess.Popen:
    write_validated_xray_config(quota, state)
    return restart_xray_from_generated_config(current_proc)


def restart_singbox_from_generated_config(current_proc: Optional[subprocess.Popen]) -> subprocess.Popen:
    has_last_good = os.path.exists(SINGBOX_LAST_GOOD_CONFIG)
    stop_process("sing-box", current_proc)

    try:
        proc = start_singbox_process(SINGBOX_GENERATED_CONFIG)
        mark_current_singbox_config_good()
        return proc
    except SingBoxProcessError:
        if not has_last_good:
            raise

        log("[sing-box] new config failed at startup, rolling back to last-known-good config")
        shutil.copyfile(SINGBOX_LAST_GOOD_CONFIG, SINGBOX_GENERATED_CONFIG)
        proc = start_singbox_process(SINGBOX_GENERATED_CONFIG)
        log("[sing-box] rollback to last-known-good config completed")
        return proc


def restart_singbox(current_proc: Optional[subprocess.Popen]) -> subprocess.Popen:
    write_validated_singbox_config()
    return restart_singbox_from_generated_config(current_proc)


def parse_xray_stats_output(text: str) -> Dict[str, int]:
    if not text.strip():
        return {}

    try:
        data = json.loads(text)
    except Exception as e:
        raise XrayStatsError(f"failed to parse xray stats response as JSON: {e}") from e

    stats = data.get("stat", [])
    if isinstance(stats, dict):
        stats = [stats]
    if not isinstance(stats, list):
        raise XrayStatsError("xray stats response field `stat` is not a list")

    parsed = {}
    for item in stats:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str):
            continue
        try:
            parsed[name] = int(item.get("value", 0))
        except Exception:
            parsed[name] = 0

    return parsed


def query_xray_user_stats() -> Dict[str, int]:
    cmd = [
        "xray",
        "api",
        "statsquery",
        "--server",
        XRAY_API_SERVER,
        "-pattern",
        "user>>>",
        "-reset=false"
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except Exception as e:
        raise XrayStatsError(f"failed running xray stats API: {e}") from e

    if result.returncode != 0:
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        raise XrayStatsError(stderr or stdout or f"xray stats API failed with code {result.returncode}")

    return parse_xray_stats_output(result.stdout)


def query_user_total_bytes(username: str, stats: Dict[str, int]) -> int:
    uplink = stats.get(f"user>>>{username}>>>traffic>>>uplink", 0)
    downlink = stats.get(f"user>>>{username}>>>traffic>>>downlink", 0)
    return uplink + downlink


def update_usage_from_xray(state: Dict[str, Any], quota: Dict[str, Any], stats: Dict[str, int]) -> bool:
    changed = False

    for username in quota["users"].keys():
        user_state = state["users"].setdefault(username, {
            "used_bytes": 0,
            "last_xray_total_bytes": 0,
            "disabled": False,
            "disabled_at": None,
            "reset_started_at": now_ts()
        })

        if user_state.get("disabled", False):
            continue

        current_total = query_user_total_bytes(username, stats)
        last_total = int(user_state.get("last_xray_total_bytes", 0))

        if current_total >= last_total:
            delta = current_total - last_total
        else:
            delta = current_total

        if delta > 0:
            user_state["used_bytes"] = int(user_state.get("used_bytes", 0)) + delta
            user_state["last_xray_total_bytes"] = current_total
            changed = True
            log(f"[usage] {username}: +{delta} bytes, total={user_state['used_bytes']}")

    return changed


def enforce_quota(state: Dict[str, Any], quota: Dict[str, Any]) -> bool:
    disabled_changed = False

    for username, user in quota["users"].items():
        limit = int(user["daily_limit_bytes"])
        user_state = state["users"].setdefault(username, {
            "used_bytes": 0,
            "last_xray_total_bytes": 0,
            "disabled": False,
            "disabled_at": None,
            "reset_started_at": now_ts()
        })

        used = int(user_state.get("used_bytes", 0))

        if used >= limit and not user_state.get("disabled", False):
            user_state["disabled"] = True
            user_state["disabled_at"] = now_ts()
            disabled_changed = True
            log(f"[quota] disabled {username}: used={used}, limit={limit}")

    return disabled_changed


def quota_status_record(username: str, user: Dict[str, Any], user_state: Dict[str, Any], quota: Dict[str, Any]) -> Dict[str, Any]:
    daily_limit = int(user["daily_limit_bytes"])
    used = int(user_state.get("used_bytes", 0))
    remaining = max(0, daily_limit - used)
    reset_remaining = reset_remaining_seconds_for_user(user_state, user, quota)

    return {
        "found": True,
        "user": username,
        "daily_limit_mb": bytes_to_mb(daily_limit),
        "today_usage_mb": bytes_to_mb(used),
        "remaining_today_mb": bytes_to_mb(remaining),
        "reset_interval_hours": user_reset_interval_hours(user, quota),
        "time_till_reset": seconds_to_hr_min(reset_remaining) if reset_remaining is not None else "never",
        "disabled": bool(user_state.get("disabled", False)),
        "disabled_at": user_state.get("disabled_at")
    }


def quota_status_for_user(username: str) -> Dict[str, Any]:
    quota = normalize_quota(load_json(QUOTA_CONFIG))
    state = load_state()
    users = quota.get("users", {})

    if username not in users:
        return {
            "found": False,
            "user": username,
            "error": "User not found"
        }

    return quota_status_record(username, users[username], state.get("users", {}).get(username, {}), quota)


def quota_status_for_all_users() -> Dict[str, Any]:
    quota = normalize_quota(load_json(QUOTA_CONFIG))
    state = load_state()
    users = []

    for username, user in sorted(quota.get("users", {}).items()):
        users.append(quota_status_record(username, user, state.get("users", {}).get(username, {}), quota))

    return {
        "users": users,
        "user_count": len(users)
    }


def admin_ui_html() -> str:
    data = quota_status_for_all_users()
    rows = []

    for item in data["users"]:
        state_class = "disabled" if item.get("disabled") else "active"
        state_label = "Disabled" if item.get("disabled") else "Active"
        rows.append(f"""
          <tr>
            <td>{html.escape(str(item["user"]))}</td>
            <td>{item["daily_limit_mb"]}</td>
            <td>{item["today_usage_mb"]}</td>
            <td>{item["remaining_today_mb"]}</td>
            <td>{item["reset_interval_hours"]}</td>
            <td>{item["time_till_reset"]}</td>
            <td><span class="badge {state_class}">{state_label}</span></td>
          </tr>
        """)

    table_rows = "\n".join(rows) if rows else '<tr><td colspan="7">No users configured.</td></tr>'

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>VPN Quota Admin</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #101114;
      --panel: #181b22;
      --text: #f4f7fb;
      --muted: #a8b0bf;
      --line: #32384a;
      --green: #3ddc97;
      --red: #ff6b6b;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: Arial, Helvetica, sans-serif;
      background: var(--bg);
      color: var(--text);
      padding: 24px;
    }}
    main {{
      width: min(1180px, 100%);
      margin: 0 auto;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 24px;
    }}
    .head {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      margin-bottom: 18px;
    }}
    h1 {{ margin: 0; font-size: 28px; }}
    .meta {{ color: var(--muted); }}
    .table-wrap {{ overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; }}
    table {{ width: 100%; border-collapse: collapse; min-width: 860px; }}
    th, td {{ padding: 12px 14px; text-align: left; border-bottom: 1px solid var(--line); white-space: nowrap; }}
    th {{ color: var(--muted); font-size: 13px; background: #11141c; }}
    tr:last-child td {{ border-bottom: 0; }}
    .badge {{
      border-radius: 999px;
      padding: 6px 10px;
      font-size: 13px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .badge.active {{ background: rgba(61, 220, 151, .15); color: var(--green); }}
    .badge.disabled {{ background: rgba(255, 107, 107, .15); color: var(--red); }}
    a {{ color: #8dbdff; text-decoration: none; }}
  </style>
</head>
<body>
  <main>
    <div class="head">
      <h1>VPN Quota Admin</h1>
      <div class="meta">{data["user_count"]} user(s) &middot; <a href="/">User lookup</a></div>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>User</th>
            <th>Limit MB</th>
            <th>Used MB</th>
            <th>Remaining MB</th>
            <th>Reset Hours</th>
            <th>Time Till Reset</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {table_rows}
        </tbody>
      </table>
    </div>
  </main>
</body>
</html>
"""


def quota_ui_html(username: str = "", status: Optional[Dict[str, Any]] = None) -> str:
    safe_username = html.escape(username)
    result = ""

    if status:
        if not status.get("found"):
            result = f"""
            <section class="result error">
              <h2>User not found</h2>
              <p>No quota entry exists for <strong>{safe_username}</strong>.</p>
            </section>
            """
        else:
            state_class = "disabled" if status.get("disabled") else "active"
            state_label = "Disabled until reset" if status.get("disabled") else "Active"
            result = f"""
            <section class="result">
              <div class="result-head">
                <h2>{html.escape(str(status["user"]))}</h2>
                <span class="badge {state_class}">{state_label}</span>
              </div>
              <dl class="grid">
                <div><dt>Traffic limit</dt><dd>{status["daily_limit_mb"]} MB</dd></div>
                <div><dt>Current usage</dt><dd>{status["today_usage_mb"]} MB</dd></div>
                <div><dt>Remaining usage</dt><dd>{status["remaining_today_mb"]} MB</dd></div>
                <div><dt>Time till reset</dt><dd>{status["time_till_reset"]}</dd></div>
              </dl>
            </section>
            """

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>VPN Quota</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #101114;
      --panel: #181b22;
      --panel-2: #202430;
      --text: #f4f7fb;
      --muted: #a8b0bf;
      --line: #32384a;
      --blue: #5ea1ff;
      --green: #3ddc97;
      --red: #ff6b6b;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: Arial, Helvetica, sans-serif;
      background: var(--bg);
      color: var(--text);
      display: grid;
      place-items: center;
      padding: 24px;
    }}
    main {{
      width: min(760px, 100%);
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 24px;
    }}
    h1 {{ margin: 0 0 20px; font-size: 28px; }}
    form {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 12px;
      margin-bottom: 18px;
    }}
    input, button {{
      height: 44px;
      border-radius: 6px;
      border: 1px solid var(--line);
      font-size: 16px;
    }}
    input {{
      min-width: 0;
      background: #0f1118;
      color: var(--text);
      padding: 0 14px;
    }}
    button {{
      background: var(--blue);
      color: #07111f;
      font-weight: 700;
      padding: 0 18px;
      cursor: pointer;
    }}
    .result {{
      background: var(--panel-2);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
    }}
    .result.error {{ border-color: var(--red); }}
    .result-head {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin-bottom: 16px;
    }}
    h2 {{ margin: 0; font-size: 22px; }}
    .badge {{
      border-radius: 999px;
      padding: 6px 10px;
      font-size: 13px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .badge.active {{ background: rgba(61, 220, 151, .15); color: var(--green); }}
    .badge.disabled {{ background: rgba(255, 107, 107, .15); color: var(--red); }}
    .grid {{
      margin: 0;
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }}
    .grid div {{
      background: #11141c;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 14px;
      min-width: 0;
    }}
    dt {{ color: var(--muted); font-size: 13px; margin-bottom: 8px; }}
    dd {{ margin: 0; font-size: 22px; font-weight: 700; overflow-wrap: anywhere; }}
    @media (max-width: 560px) {{
      main {{ padding: 18px; }}
      form {{ grid-template-columns: 1fr; }}
      .grid {{ grid-template-columns: 1fr; }}
      .result-head {{ align-items: flex-start; flex-direction: column; }}
    }}
  </style>
</head>
<body>
  <main>
    <h1>VPN Quota</h1>
    <form method="get" action="/">
      <input name="user" value="{safe_username}" placeholder="Enter username, e.g. user01" autocomplete="username" autofocus>
      <button type="submit">Check</button>
    </form>
    {result}
  </main>
</body>
</html>
"""


class QuotaUiHandler(BaseHTTPRequestHandler):
    server_version = "xray-quota-ui/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        log(f"[quota-ui] {self.address_string()} {format % args}")

    def send_bytes(self, status_code: int, content_type: str, body: bytes) -> None:
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_admin_auth_required(self) -> None:
        body = b"admin authentication required"
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="xray-quota-admin"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def admin_auth_enabled(self) -> bool:
        return bool(ADMIN_USERNAME or ADMIN_PASSWORD)

    def admin_authenticated(self) -> bool:
        if not self.admin_auth_enabled():
            return True

        if not ADMIN_USERNAME or not ADMIN_PASSWORD:
            log("[quota-ui] admin auth is partially configured; set both ADMIN_USERNAME and ADMIN_PASSWORD")
            return False

        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False

        try:
            decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
        except Exception:
            return False

        username, separator, password = decoded.partition(":")
        if not separator:
            return False

        return secrets.compare_digest(username, ADMIN_USERNAME) and secrets.compare_digest(password, ADMIN_PASSWORD)

    def require_admin_auth(self) -> bool:
        if self.admin_authenticated():
            return True

        self.send_admin_auth_required()
        return False

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        username = params.get("user", [""])[0].strip()

        if parsed.path == "/api/quota":
            if not username:
                body = json.dumps({"found": False, "error": "Missing user"}).encode("utf-8")
                self.send_bytes(400, "application/json; charset=utf-8", body)
                return

            body = json.dumps(quota_status_for_user(username), sort_keys=True).encode("utf-8")
            self.send_bytes(200, "application/json; charset=utf-8", body)
            return

        if parsed.path == "/api/admin":
            if not self.require_admin_auth():
                return

            body = json.dumps(quota_status_for_all_users(), sort_keys=True).encode("utf-8")
            self.send_bytes(200, "application/json; charset=utf-8", body)
            return

        if parsed.path == "/admin":
            if not self.require_admin_auth():
                return

            body = admin_ui_html().encode("utf-8")
            self.send_bytes(200, "text/html; charset=utf-8", body)
            return

        if parsed.path not in ["/", "/quota"]:
            self.send_bytes(404, "text/plain; charset=utf-8", b"not found")
            return

        status = quota_status_for_user(username) if username else None
        body = quota_ui_html(username, status).encode("utf-8")
        self.send_bytes(200, "text/html; charset=utf-8", body)


def start_quota_ui_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((QUOTA_UI_HOST, QUOTA_UI_PORT), QuotaUiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log(f"[quota-ui] listening on {QUOTA_UI_HOST}:{QUOTA_UI_PORT}")
    return server


def graceful_shutdown(signum, frame) -> None:
    raise KeyboardInterrupt()


def main() -> int:
    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT, graceful_shutdown)

    Path("/etc/xray").mkdir(parents=True, exist_ok=True)
    Path("/etc/sing-box").mkdir(parents=True, exist_ok=True)
    Path("/data").mkdir(parents=True, exist_ok=True)
    Path(RUNTIME_CONFIG_DIR).mkdir(parents=True, exist_ok=True)

    config_mode = configure_runtime_config_files()
    quota = normalize_quota(load_json(QUOTA_CONFIG))
    state = load_state()

    if ensure_state_users(state, quota):
        save_json_atomic(STATE_FILE, state)

    reset_changed, _ = reset_due_users(state, quota)
    if reset_changed:
        save_json_atomic(STATE_FILE, state)

    write_validated_singbox_config()
    write_validated_xray_config(quota, state)
    if ENABLE_HTTP_FRONTEND:
        write_nginx_config()
        validate_nginx_config()

    config_hash = current_config_hash(config_mode)
    disabled_snapshot = json.dumps(
        {u: state["users"].get(u, {}).get("disabled", False) for u in quota["users"].keys()},
        sort_keys=True
    )

    singbox_proc = None
    xray_proc = None
    nginx_proc = None
    quota_ui_server = None
    stats_failure_count = 0

    try:
        quota_ui_server = start_quota_ui_server()
        if ENABLE_HTTP_FRONTEND:
            nginx_proc = start_nginx_process()
        singbox_proc = start_singbox_process(SINGBOX_GENERATED_CONFIG)
        mark_current_singbox_config_good()
        xray_proc = start_xray_process(XRAY_GENERATED_CONFIG)
        mark_current_xray_config_good()

        while True:
            if ENABLE_HTTP_FRONTEND and nginx_proc is not None and nginx_proc.poll() is not None:
                log(f"[fatal] nginx exited with code {nginx_proc.returncode}")
                return nginx_proc.returncode or 1

            if singbox_proc.poll() is not None:
                log(f"[warn] sing-box exited with code {singbox_proc.returncode}, restarting")
                try:
                    singbox_proc = restart_singbox_from_generated_config(singbox_proc)
                except (SingBoxConfigError, SingBoxProcessError) as e:
                    log(f"[fatal] cannot restart sing-box: {e}")
                    return 1

            if xray_proc.poll() is not None:
                log(f"[warn] xray exited with code {xray_proc.returncode}, restarting")
                try:
                    xray_proc = restart_xray(xray_proc, quota, state)
                except (XrayConfigError, XrayProcessError) as e:
                    log(f"[fatal] cannot restart xray because generated config is invalid: {e}")
                    return 1

            try:
                new_config_hash = current_config_hash(config_mode)
                if new_config_hash != config_hash:
                    log("[config] runtime config changed, reloading")
                    if config_mode == "combined":
                        extract_combined_config()

                    next_quota = normalize_quota(load_json(QUOTA_CONFIG))
                    next_state = copy.deepcopy(state)
                    state_changed = ensure_state_users(next_state, next_quota)

                    write_validated_singbox_config()
                    write_validated_xray_config(next_quota, next_state)
                    if ENABLE_HTTP_FRONTEND:
                        write_nginx_config()
                        validate_nginx_config()
                        reload_nginx()

                    next_singbox_proc = restart_singbox_from_generated_config(singbox_proc)
                    next_xray_proc = restart_xray_from_generated_config(xray_proc)

                    quota = next_quota
                    state = next_state
                    config_hash = new_config_hash
                    singbox_proc = next_singbox_proc
                    xray_proc = next_xray_proc
                    stats_failure_count = 0
                    if state_changed:
                        save_json_atomic(STATE_FILE, state)
            except Exception as e:
                log(f"[warn] failed to reload runtime config: {e}")

            try:
                stats = query_xray_user_stats()
                stats_failure_count = 0
            except XrayStatsError as e:
                stats_failure_count += 1
                log(f"[stats] xray stats API failed ({stats_failure_count}/{XRAY_API_MAX_FAILURES}): {e}")
                if stats_failure_count >= XRAY_API_MAX_FAILURES:
                    log("[fatal] xray stats API failure limit reached; exiting so the PaaS can restart the container")
                    return 1

                interval = int(quota.get("check_interval_seconds", 60))
                time.sleep(max(5, interval))
                continue

            usage_changed = update_usage_from_xray(state, quota, stats)
            disabled_changed = enforce_quota(state, quota)
            reset_changed, reset_disabled_changed = reset_due_users(state, quota)
            disabled_changed = disabled_changed or reset_disabled_changed

            if usage_changed or disabled_changed or reset_changed:
                save_json_atomic(STATE_FILE, state)

            current_disabled_snapshot = json.dumps(
                {u: state["users"].get(u, {}).get("disabled", False) for u in quota["users"].keys()},
                sort_keys=True
            )

            if reset_changed or disabled_changed or current_disabled_snapshot != disabled_snapshot:
                disabled_snapshot = current_disabled_snapshot
                try:
                    xray_proc = restart_xray(xray_proc, quota, state)
                except (XrayConfigError, XrayProcessError) as e:
                    log(f"[fatal] cannot update quota enforcement because generated config is invalid: {e}")
                    return 1

            interval = int(quota.get("check_interval_seconds", 60))
            time.sleep(max(5, interval))

    except KeyboardInterrupt:
        log("[shutdown] received shutdown signal")
    finally:
        if quota_ui_server is not None:
            quota_ui_server.shutdown()
            quota_ui_server.server_close()
        stop_process("xray", xray_proc)
        stop_process("sing-box", singbox_proc)
        stop_process("nginx", nginx_proc)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"[fatal] startup failed: {e}")
        sys.exit(1)
