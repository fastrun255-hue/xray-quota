#!/usr/bin/env python3

import copy
import hashlib
import json
import os
import signal
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml


CONFIG_CANDIDATES = [
    os.environ.get("CUSTOM_CONFIG", "/app/config/custom-config.yaml"),
    os.environ.get("YAML_CONFIG", "/app/config/config.yaml"),
    os.environ.get("COMBINED_CONFIG", "/app/config/config.json"),
    os.environ.get("SINGBOX_CONFIG", "/app/config/sing-box.json"),
    "/etc/sing-box/config.json",
]
SINGBOX_GENERATED_CONFIG = os.environ.get("SINGBOX_GENERATED_CONFIG", "/run/sing-box/config.json")
SINGBOX_LAST_GOOD_CONFIG = os.environ.get("SINGBOX_LAST_GOOD_CONFIG", "/run/sing-box/config.last-good.json")
STATE_FILE = os.environ.get("STATE_FILE", "/data/usage-state.json")
SINGBOX_API_SERVER = os.environ.get("SINGBOX_API_SERVER", "127.0.0.1:10085")
SINGBOX_API_MAX_FAILURES = int(os.environ.get("SINGBOX_API_MAX_FAILURES", "5"))
SINGBOX_STARTUP_GRACE_SECONDS = float(os.environ.get("SINGBOX_STARTUP_GRACE_SECONDS", "1"))
DEFAULT_CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "30"))
DEFAULT_RESET_INTERVAL_HOURS = int(os.environ.get("RESET_INTERVAL_HOURS", "24"))

QUOTA_FIELDS = {
    "quota_bytes",
    "traffic_limit_bytes",
    "daily_limit_bytes",
    "limit_bytes",
    "reset_interval_hours",
    "reset_key",
    "renewal_key",
    "enabled",
    "disabled",
    "client",
    "email",
    "level",
    "quota",
}


class ConfigError(Exception):
    pass


class SingBoxConfigError(Exception):
    pass


class SingBoxProcessError(Exception):
    pass


class StatsError(Exception):
    pass


def log(message: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    print(f"[{now}] {message}", flush=True)


def now_ts() -> int:
    return int(time.time())


def load_json_or_yaml(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        if Path(path).suffix.lower() in [".yaml", ".yml"]:
            data = yaml.safe_load(f)
        else:
            data = json.load(f)

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain an object")
    return data


def parse_embedded_config(value: Any) -> Optional[Dict[str, Any]]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    try:
        data = json.loads(text)
    except Exception:
        data = yaml.safe_load(text)

    if not isinstance(data, dict):
        raise ConfigError("embedded config.json must contain an object")
    return data


def find_embedded_config_json(value: Any) -> Optional[Dict[str, Any]]:
    if isinstance(value, dict):
        if "config.json" in value:
            embedded = parse_embedded_config(value["config.json"])
            if embedded is not None:
                return embedded
        for child in value.values():
            found = find_embedded_config_json(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_embedded_config_json(child)
            if found is not None:
                return found
    return None


def extract_runtime_config(bundle: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    quota = {}
    if isinstance(bundle.get("quota"), dict):
        quota = copy.deepcopy(bundle["quota"])

    for key in ["sing-box", "sing_box", "singbox"]:
        if isinstance(bundle.get(key), dict):
            return copy.deepcopy(bundle[key]), quota

    embedded = find_embedded_config_json(bundle.get("configmap"))
    if embedded is None:
        embedded = find_embedded_config_json(bundle)
    if embedded is not None and embedded is not bundle:
        raw = copy.deepcopy(embedded)
        embedded_quota = raw.pop("quota", None)
        if isinstance(embedded_quota, dict) and not quota:
            quota = embedded_quota
        return raw, quota

    if isinstance(bundle.get("inbounds"), list) and isinstance(bundle.get("outbounds"), list):
        raw_config = copy.deepcopy(bundle)
        embedded_quota = raw_config.pop("quota", None)
        if isinstance(embedded_quota, dict) and not quota:
            quota = embedded_quota
        return raw_config, quota

    raise ConfigError("custom config must contain `sing-box`, raw sing-box `inbounds`/`outbounds`, or embedded config.json")


def find_config_file() -> str:
    seen = set()
    for path in CONFIG_CANDIDATES:
        if not path or path in seen:
            continue
        seen.add(path)
        if os.path.exists(path):
            return path
    raise FileNotFoundError("no runtime config found; mount custom-config.yaml or /etc/sing-box/config.json")


def file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def save_json_atomic(path: str, data: Dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    tmp.replace(target)


def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {"users": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("state root is not an object")
        data.setdefault("users", {})
        return data
    except Exception as e:
        log(f"[warn] could not read state file, starting fresh: {e}")
        return {"users": {}}


def first_present(data: Dict[str, Any], keys: List[str]) -> Any:
    for key in keys:
        if key in data:
            return data[key]
    return None


def int_or_none(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    return int(value)


def normalize_quota_defaults(quota: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "check_interval_seconds": int(quota.get("check_interval_seconds", DEFAULT_CHECK_INTERVAL_SECONDS)),
        "reset_interval_hours": int(
            quota.get("reset_interval_hours", quota.get("default_reset_interval_hours", DEFAULT_RESET_INTERVAL_HOURS))
        ),
        "default_limit_bytes": int_or_none(
            first_present(quota, ["default_limit_bytes", "default_quota_bytes", "default_daily_limit_bytes"])
        ),
        "auto_optimize": bool(quota.get("auto_optimize", True)),
    }


def find_quota_user(quota: Dict[str, Any], name: str, uuid: str) -> Dict[str, Any]:
    users = quota.get("users", {})
    if not isinstance(users, dict):
        return {}
    for key in [name, uuid]:
        value = users.get(key)
        if isinstance(value, dict):
            return copy.deepcopy(value)
    return {}


def sanitize_singbox_user(user: Dict[str, Any], name: str) -> Dict[str, Any]:
    clean = {k: copy.deepcopy(v) for k, v in user.items() if k not in QUOTA_FIELDS}
    clean["name"] = name
    return clean


def user_limit_bytes(merged: Dict[str, Any], defaults: Dict[str, Any]) -> Optional[int]:
    value = first_present(merged, ["quota_bytes", "traffic_limit_bytes", "daily_limit_bytes", "limit_bytes"])
    if value is None:
        value = defaults.get("default_limit_bytes")
    return int_or_none(value)


def user_reset_key(merged: Dict[str, Any]) -> str:
    value = first_present(merged, ["reset_key", "renewal_key"])
    return "" if value is None else str(value)


def build_users_from_quota_map(quota: Dict[str, Any]) -> List[Dict[str, Any]]:
    users = quota.get("users", {})
    if not isinstance(users, dict):
        return []

    result = []
    for username, item in users.items():
        if not isinstance(item, dict):
            continue
        user = copy.deepcopy(item.get("client", {}))
        user.update(item)
        user.pop("client", None)
        user.setdefault("name", username)
        if "uuid" not in user:
            raise ConfigError(f"quota user `{username}` is missing uuid")
        result.append(user)
    return result


def new_user_state(reset_key: str, uuid: str) -> Dict[str, Any]:
    return {
        "used_bytes": 0,
        "last_total_bytes": 0,
        "disabled": False,
        "disabled_at": None,
        "reset_started_at": now_ts(),
        "reset_key": reset_key,
        "uuid": uuid,
    }


def reset_due(user_state: Dict[str, Any], reset_interval_hours: int) -> bool:
    if reset_interval_hours <= 0:
        return False
    reset_started_at = int(user_state.get("reset_started_at", now_ts()))
    return now_ts() - reset_started_at >= reset_interval_hours * 3600


def prepare_runtime_config(source: Dict[str, Any], quota: Dict[str, Any], state: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any], bool]:
    defaults = normalize_quota_defaults(quota)
    config = copy.deepcopy(source)
    config.setdefault("log", {})
    if defaults["auto_optimize"]:
        config["log"].setdefault("level", "warn")
        config["log"].setdefault("output", "stdout")

    quota_users: Dict[str, Dict[str, Any]] = {}
    active_stat_users: List[str] = []
    state_changed = False
    vless_found = False

    for inbound in config.get("inbounds", []):
        if not isinstance(inbound, dict) or inbound.get("type") != "vless":
            continue

        vless_found = True
        if defaults["auto_optimize"]:
            inbound.setdefault("tcp_fast_open", True)

        original_users = inbound.get("users")
        if not original_users and isinstance(quota.get("users"), dict):
            original_users = build_users_from_quota_map(quota)

        if not isinstance(original_users, list):
            raise ConfigError(f"vless inbound `{inbound.get('tag', '')}` users must be a list")

        active_users = []
        for raw_user in original_users:
            if not isinstance(raw_user, dict):
                continue
            uuid = str(raw_user.get("uuid", "")).strip()
            if not uuid:
                raise ConfigError("each VLESS user must have a uuid")

            configured_name = str(raw_user.get("name") or raw_user.get("email") or uuid).strip()
            quota_user = find_quota_user(quota, configured_name, uuid)
            merged = copy.deepcopy(raw_user)
            merged.update(quota_user)
            name = str(merged.get("name") or configured_name or uuid).strip()
            if not name:
                name = uuid

            limit = user_limit_bytes(merged, defaults)
            reset_interval = int(merged.get("reset_interval_hours", defaults["reset_interval_hours"]))
            reset_key = user_reset_key(merged)
            configured_enabled = bool(merged.get("enabled", True)) and not bool(merged.get("disabled", False))

            quota_users[name] = {
                "name": name,
                "uuid": uuid,
                "limit_bytes": limit,
                "reset_interval_hours": reset_interval,
                "reset_key": reset_key,
                "enabled": configured_enabled,
            }

            user_state = state["users"].setdefault(name, new_user_state(reset_key, uuid))
            if user_state.get("reset_key", "") != reset_key or user_state.get("uuid", uuid) != uuid:
                state["users"][name] = new_user_state(reset_key, uuid)
                user_state = state["users"][name]
                state_changed = True
                log(f"[quota] reset state for {name}: config reset_key or uuid changed")

            if reset_due(user_state, reset_interval):
                state["users"][name] = new_user_state(reset_key, uuid)
                user_state = state["users"][name]
                state_changed = True
                log(f"[quota] periodic reset completed for {name}")

            if not configured_enabled:
                user_state["disabled"] = True
                user_state["disabled_at"] = user_state.get("disabled_at") or now_ts()
                state_changed = True

            if not user_state.get("disabled", False):
                active_users.append(sanitize_singbox_user(merged, name))
                active_stat_users.append(name)

        inbound["users"] = active_users

    if not vless_found:
        raise ConfigError("sing-box config must include at least one VLESS inbound")

    experimental = config.setdefault("experimental", {})
    if not isinstance(experimental, dict):
        raise ConfigError("sing-box field `experimental` must be an object")
    experimental["v2ray_api"] = {
        "listen": SINGBOX_API_SERVER,
        "stats": {
            "enabled": True,
            "users": sorted(set(active_stat_users)),
        },
    }

    return config, {"defaults": defaults, "users": quota_users}, state_changed


def validate_singbox_config(path: str) -> None:
    result = subprocess.run(["sing-box", "check", "-c", path], capture_output=True, text=True, timeout=20)
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        raise SingBoxConfigError(message or f"sing-box config check failed with code {result.returncode}")


def write_validated_config(config: Dict[str, Any]) -> None:
    candidate = f"{SINGBOX_GENERATED_CONFIG}.candidate.json"
    save_json_atomic(candidate, config)
    validate_singbox_config(candidate)
    Path(candidate).replace(SINGBOX_GENERATED_CONFIG)
    log("[sing-box] generated validated config")


def start_singbox_process(config_path: str) -> subprocess.Popen:
    proc = subprocess.Popen(["sing-box", "run", "-c", config_path])
    time.sleep(SINGBOX_STARTUP_GRACE_SECONDS)
    if proc.poll() is not None:
        raise SingBoxProcessError(f"sing-box exited during startup with code {proc.returncode}")
    log(f"[sing-box] started pid={proc.pid}")
    return proc


def stop_process(name: str, proc: Optional[subprocess.Popen]) -> None:
    if proc is None or proc.poll() is not None:
        return
    log(f"[shutdown] stopping {name}")
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=8)


def restart_singbox(current_proc: Optional[subprocess.Popen], config: Dict[str, Any]) -> subprocess.Popen:
    has_last_good = os.path.exists(SINGBOX_LAST_GOOD_CONFIG)
    write_validated_config(config)
    stop_process("sing-box", current_proc)
    try:
        proc = start_singbox_process(SINGBOX_GENERATED_CONFIG)
        shutil.copyfile(SINGBOX_GENERATED_CONFIG, SINGBOX_LAST_GOOD_CONFIG)
        return proc
    except SingBoxProcessError:
        if not has_last_good:
            raise
        log("[sing-box] startup failed; rolling back to last-known-good config")
        shutil.copyfile(SINGBOX_LAST_GOOD_CONFIG, SINGBOX_GENERATED_CONFIG)
        return start_singbox_process(SINGBOX_GENERATED_CONFIG)


def parse_stats_output(text: str) -> Dict[str, int]:
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except Exception as e:
        raise StatsError(f"failed to parse stats response as JSON: {e}") from e

    stats = data.get("stat", [])
    if isinstance(stats, dict):
        stats = [stats]
    if not isinstance(stats, list):
        raise StatsError("stats response field `stat` is not a list")

    parsed = {}
    for item in stats:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str):
            parsed[name] = int(item.get("value", 0))
    return parsed


def query_user_stats() -> Dict[str, int]:
    cmd = [
        "xray",
        "api",
        "statsquery",
        "--server",
        SINGBOX_API_SERVER,
        "-pattern",
        "user>>>",
        "-reset=false",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        raise StatsError(message or f"stats API failed with code {result.returncode}")
    return parse_stats_output(result.stdout)


def user_total_bytes(username: str, stats: Dict[str, int]) -> int:
    return (
        stats.get(f"user>>>{username}>>>traffic>>>uplink", 0)
        + stats.get(f"user>>>{username}>>>traffic>>>downlink", 0)
    )


def update_usage(state: Dict[str, Any], quota: Dict[str, Any], stats: Dict[str, int]) -> bool:
    changed = False
    for username, user in quota["users"].items():
        if user.get("limit_bytes") is None:
            continue
        user_state = state["users"].setdefault(username, new_user_state(user.get("reset_key", ""), user.get("uuid", "")))
        if user_state.get("disabled", False):
            continue

        current_total = user_total_bytes(username, stats)
        last_total = int(user_state.get("last_total_bytes", 0))
        delta = current_total - last_total if current_total >= last_total else current_total
        if delta <= 0:
            continue

        user_state["used_bytes"] = int(user_state.get("used_bytes", 0)) + delta
        user_state["last_total_bytes"] = current_total
        changed = True
        log(f"[usage] {username}: +{delta} bytes, total={user_state['used_bytes']}")
    return changed


def enforce_quota(state: Dict[str, Any], quota: Dict[str, Any]) -> bool:
    changed = False
    for username, user in quota["users"].items():
        limit = user.get("limit_bytes")
        if limit is None:
            continue
        user_state = state["users"].setdefault(username, new_user_state(user.get("reset_key", ""), user.get("uuid", "")))
        if user_state.get("disabled", False):
            continue
        used = int(user_state.get("used_bytes", 0))
        if used >= int(limit):
            user_state["disabled"] = True
            user_state["disabled_at"] = now_ts()
            changed = True
            log(f"[quota] disabled {username}: used={used}, limit={limit}")
    return changed


def read_prepared_config(state: Dict[str, Any]) -> Tuple[str, Dict[str, Any], Dict[str, Any], bool]:
    config_path = find_config_file()
    source_config, quota_config = extract_runtime_config(load_json_or_yaml(config_path))
    runtime_config, quota, state_changed = prepare_runtime_config(source_config, quota_config, state)
    return config_path, runtime_config, quota, state_changed


def graceful_shutdown(signum, frame) -> None:
    raise KeyboardInterrupt()


def main() -> int:
    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT, graceful_shutdown)

    Path("/etc/sing-box").mkdir(parents=True, exist_ok=True)
    Path("/run/sing-box").mkdir(parents=True, exist_ok=True)
    Path("/data").mkdir(parents=True, exist_ok=True)

    state = load_state()
    config_path, runtime_config, quota, state_changed = read_prepared_config(state)
    if state_changed:
        save_json_atomic(STATE_FILE, state)
    write_validated_config(runtime_config)
    shutil.copyfile(SINGBOX_GENERATED_CONFIG, SINGBOX_LAST_GOOD_CONFIG)
    config_hash = file_hash(config_path)

    singbox_proc: Optional[subprocess.Popen] = None
    stats_failure_count = 0

    try:
        singbox_proc = start_singbox_process(SINGBOX_GENERATED_CONFIG)
        while True:
            if singbox_proc.poll() is not None:
                log(f"[warn] sing-box exited with code {singbox_proc.returncode}, restarting")
                singbox_proc = restart_singbox(singbox_proc, runtime_config)
                stats_failure_count = 0

            new_hash = file_hash(config_path)
            if new_hash != config_hash:
                log("[config] runtime config changed, reloading")
                config_path, runtime_config, quota, state_changed = read_prepared_config(state)
                singbox_proc = restart_singbox(singbox_proc, runtime_config)
                config_hash = file_hash(config_path)
                stats_failure_count = 0
                if state_changed:
                    save_json_atomic(STATE_FILE, state)

            try:
                stats = query_user_stats()
                stats_failure_count = 0
            except StatsError as e:
                stats_failure_count += 1
                log(f"[stats] API failed ({stats_failure_count}/{SINGBOX_API_MAX_FAILURES}): {e}")
                if stats_failure_count >= SINGBOX_API_MAX_FAILURES:
                    log("[fatal] stats API failure limit reached; exiting for PaaS restart")
                    return 1
                time.sleep(max(5, quota["defaults"]["check_interval_seconds"]))
                continue

            usage_changed = update_usage(state, quota, stats)
            disabled_changed = enforce_quota(state, quota)
            if usage_changed or disabled_changed:
                save_json_atomic(STATE_FILE, state)

            if disabled_changed:
                _, runtime_config, quota, state_changed = read_prepared_config(state)
                singbox_proc = restart_singbox(singbox_proc, runtime_config)
                if state_changed:
                    save_json_atomic(STATE_FILE, state)

            time.sleep(max(5, quota["defaults"]["check_interval_seconds"]))

    except KeyboardInterrupt:
        log("[shutdown] received shutdown signal")
    finally:
        stop_process("sing-box", singbox_proc)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"[fatal] startup failed: {e}")
        sys.exit(1)
