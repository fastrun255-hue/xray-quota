# xray-quota

Docker image for running Xray-core, sing-box, and a Python quota controller.

The image contains no secrets, server addresses, UUIDs, passwords, or user quota data. All real runtime configuration must be provided by the PaaS.

## Recommended PaaS Setup

If your PaaS only lets you mount or edit one config file, mount it here:

```text
/app/config/config.json
```

This image also auto-detects these paths:

```text
/app/config/custom-config.yaml
/app/config/config.yaml
```

Use this top-level shape for JSON or YAML:

```yaml
sing-box: {}
xray-template: {}
quota:
  reset_interval_hours: 24
  check_interval_seconds: 60
  users:
    user1:
      uuid: 00000000-0000-4000-8000-000000000001
      daily_limit_bytes: 2147483648
      flow: xtls-rprx-vision
```

Equivalent JSON is also accepted:

```json
{
  "sing-box": {},
  "xray-template": {},
  "quota": {
    "reset_interval_hours": 24,
    "check_interval_seconds": 60,
    "users": {
      "user1": {
        "uuid": "00000000-0000-4000-8000-000000000001",
        "daily_limit_bytes": 2147483648,
        "flow": "xtls-rprx-vision"
      }
    }
  }
}
```

The `sing-box` object is your full sing-box config. The `xray-template` object is your full Xray config without real clients in the quota-managed inbound. The `quota` object contains users and their daily byte limits.

Quota user fields other than `uuid`, `daily_limit_bytes`, `level`, and `client` are passed into the generated Xray client object. This is useful for VLESS fields such as `flow`.

## Separate File Mode

You can also mount three separate files:

```text
/app/config/sing-box.json
/app/config/xray-template.json
/app/config/quota.json
```

`CONFIG_MODE=auto` uses separate files if all three exist, otherwise it uses `/app/config/config.json`.

## Persistent State

Mount a persistent disk to:

```text
/data
```

Usage state is stored at:

```text
/data/usage-state.json
```

Without a persistent `/data` volume, users' daily usage can reset on redeploy.

## Quota Sizing

`daily_limit_bytes` is the daily cap for one user. Examples:

```text
1 GiB  = 1073741824
2 GiB  = 2147483648
5 GiB  = 5368709120
10 GiB = 10737418240
```

If someone pays for 90 GiB/month, a simple daily quota is:

```text
90 * 1073741824 / 30 = 3221225472 bytes/day
```

## Environment Variables

Optional environment variables:

```text
CONFIG_MODE=auto
COMBINED_CONFIG=/app/config/config.json
CUSTOM_CONFIG=/app/config/custom-config.yaml
YAML_CONFIG=/app/config/config.yaml
RUNTIME_CONFIG_DIR=/run/xray-quota
SINGBOX_CONFIG=/app/config/sing-box.json
SINGBOX_GENERATED_CONFIG=/etc/sing-box/config.json
SINGBOX_LAST_GOOD_CONFIG=/etc/sing-box/config.last-good.json
SINGBOX_STARTUP_GRACE_SECONDS=1
ENABLE_HTTP_FRONTEND=true
PUBLIC_HTTP_PORT=8080
XRAY_PROXY_HOST=127.0.0.1
XRAY_PROXY_PORT=10000
XRAY_WS_PATH=
NGINX_GENERATED_CONFIG=/etc/nginx/http.d/xray-quota.conf
QUOTA_UI_HOST=0.0.0.0
QUOTA_UI_PORT=9090
XRAY_TEMPLATE=/app/config/xray-template.json
QUOTA_CONFIG=/app/config/quota.json
XRAY_GENERATED_CONFIG=/etc/xray/config.json
XRAY_LAST_GOOD_CONFIG=/etc/xray/config.last-good.json
STATE_FILE=/data/usage-state.json
XRAY_API_SERVER=127.0.0.1:10085
XRAY_INBOUND_TAG=vless-in
XRAY_API_MAX_FAILURES=5
XRAY_STARTUP_GRACE_SECONDS=1
XRAY_LOCATION_ASSET=/usr/local/share/xray
```

## Xray Template Requirements

The Xray template must include:

* An inbound with tag matching `XRAY_INBOUND_TAG`, default: `vless-in`
* Xray API with `StatsService` enabled on `127.0.0.1:10085`
* An API inbound routed to the `api` outbound
* An outbound pointing to local sing-box SOCKS, usually `127.0.0.1:10808`

The quota controller injects active users into:

```json
inbounds[].settings.clients
```

for the inbound tagged `vless-in`.

The controller also enables Xray user uplink/downlink stats for every quota user level in the generated config.

Generic Xray API pieces needed in your template:

```json
{
  "api": {
    "tag": "api",
    "services": ["StatsService"]
  },
  "inbounds": [
    {
      "tag": "api",
      "listen": "127.0.0.1",
      "port": 10085,
      "protocol": "dokodemo-door",
      "settings": {
        "address": "127.0.0.1"
      }
    }
  ],
  "outbounds": [
    {
      "tag": "api",
      "protocol": "freedom"
    }
  ],
  "routing": {
    "rules": [
      {
        "type": "field",
        "inboundTag": ["api"],
        "outboundTag": "api"
      }
    ]
  }
}
```

## Runtime Behavior

1. The controller loads either `/app/config/config.json` or the three separate config files.
2. It validates sing-box with `sing-box check -c`.
3. It generates and validates Xray config with `xray run -test`.
4. It starts sing-box and Xray.
5. It queries Xray user stats through `xray api statsquery -pattern "user>>>"`.
6. If a user exceeds their daily quota, the controller removes that user from generated Xray config and restarts Xray.
7. After `reset_interval_hours`, default 24, all users are enabled again and counters reset.
8. Runtime config changes are reloaded automatically.
9. Repeated stats API failures make the controller exit non-zero so the PaaS can restart the container.

## Quota UI

By default, the container starts an HTTP frontend on public port `8080`:

* `http://HOST/` shows the quota page.
* The Xray WebSocket path, for example `/myvpn`, is proxied to Xray internally.
* The quota UI still runs internally on port `9090`.

This lets PaaS providers expose one HTTP service port while keeping the VPN and quota page on the same domain.

If `ENABLE_HTTP_FRONTEND=false`, the quota page is exposed directly on:

```text
http://HOST:9090/
```

Users enter their username and see:

* Daily limit in MB
* Today traffic usage in MB
* Remaining today usage in MB
* Time till reset as `hr:min`

There is also a JSON endpoint:

```text
/api/quota?user=user01
```

Expose container port `9090` in the PaaS only if users should access this page directly instead of through the default HTTP frontend.

## Security

Never commit:

* Real UUIDs
* Server IPs
* Hysteria2 passwords
* Domains
* User quota data
* Production config files
