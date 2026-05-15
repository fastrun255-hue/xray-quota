# xray-quota

Fast sing-box image for a paid VLESS WebSocket to Hysteria2 VPN.

Runtime traffic path:

```text
client -> sing-box VLESS WS inbound -> sing-box Hysteria2 outbound
```

The quota controller is outside the traffic path. It reads the same PaaS config, enables sing-box user stats, stores usage in `/data/usage-state.json`, and removes a user from the generated sing-box config after that user reaches the paid traffic limit.

## PaaS Config

The image auto-detects these files:

```text
/app/config/custom-config.yaml
/app/config/config.yaml
/app/config/config.json
/app/config/sing-box.json
/etc/sing-box/config.json
```

For your PaaS setup, keep everything in `custom-config.yaml`.

## Recommended Shape

You can put quota fields directly on each VLESS user. The controller removes those fields before writing the generated sing-box config.

```yaml
sing-box:
  log:
    level: warn
    output: stdout
  inbounds:
    - type: vless
      tag: vless-in
      listen: 0.0.0.0
      listen_port: 8081
      users:
        - name: user1
          uuid: 47869f00-d04b-4ca8-ab92-bc29c72012ea
          quota_bytes: 107374182400
          reset_interval_hours: 720
          reset_key: "user1-2026-05"
      transport:
        type: ws
        path: /myvpn
  outbounds:
    - type: hysteria2
      tag: foreign-server-out
      server: YOUR-HYSTERIA2-SERVER-IP
      server_port: 443
      password: "6969"
      up_mbps: 1000
      down_mbps: 1000
      tls:
        enabled: true
        server_name: bing.com
        insecure: true
    - type: direct
      tag: direct-out
  route:
    rules:
      - inbound: vless-in
        outbound: foreign-server-out
    final: direct-out

quota:
  check_interval_seconds: 30
  reset_interval_hours: 720
  auto_optimize: true
```

Supported per-user quota fields:

```text
quota_bytes
traffic_limit_bytes
daily_limit_bytes
limit_bytes
reset_interval_hours
reset_key
renewal_key
enabled
disabled
```

`name` is important. sing-box stats are counted by user name. If a user has no `name`, the controller uses the UUID as the name.

## Renewing Users

Because the PaaS `/data` volume is not manually accessible, renew a user from config:

```yaml
reset_key: "user1-2026-06"
```

Changing `reset_key` resets that user's stored usage and enables the user again. Changing the UUID also resets that user's stored usage.

Use `reset_interval_hours: 0` for one-time traffic packages that should never automatically reset.

## ConfigMap Wrapper

The controller can also read a PaaS configmap wrapper containing `config.json`. This shape is accepted:

```yaml
configmap:
  configmapItems:
    singbox-config:
      path: /etc/sing-box/config.json
      subPath: config.json
      value:
        config.json: |-
          {
            "log": { "level": "warn", "output": "stdout" },
            "inbounds": [],
            "outbounds": [],
            "route": {},
            "quota": {}
          }
```

## Speed Notes

The image builds sing-box from source with the V2Ray API enabled so per-user stats work. The V2Ray API is used only for accounting; it is not in the VPN traffic path.

For Hysteria2, set `up_mbps` and `down_mbps` to the real bandwidth of the Hysteria2 server. Too-low values can cap speed. Too-high values can make congestion behavior worse on weak links.

The controller sets these performance defaults when `quota.auto_optimize` is not false:

```text
log.level = warn
log.output = stdout
vless inbound tcp_fast_open = true
```

## Ports

The image exposes `8081`. Your PaaS public service should route to the same port used by the VLESS inbound `listen_port`.

## Security

Do not commit production UUIDs, server IPs, Hysteria2 passwords, domains, or real paid-user quota data unless this private repository is where you intentionally manage that config.
