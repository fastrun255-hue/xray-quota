FROM golang:1.24-alpine AS sing-box-builder

ARG SINGBOX_VERSION=1.11.15

RUN apk add --no-cache git build-base

WORKDIR /src
RUN git clone --depth 1 --branch "v${SINGBOX_VERSION}" https://github.com/SagerNet/sing-box.git .
RUN TAGS="with_gvisor,with_dhcp,with_wireguard,with_reality_server,with_clash_api,with_quic,with_utls,with_acme,with_ech,with_v2ray_api" \
    && mkdir -p /out \
    && VERSION="$(CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go run ./cmd/internal/read_tag)" \
    && go build -v -trimpath -tags "$TAGS" \
      -ldflags "-X 'github.com/sagernet/sing-box/constant.Version=${VERSION}' -s -w -buildid=" \
      -o /out/sing-box ./cmd/sing-box

FROM alpine:3.20

RUN apk add --no-cache \
    python3 \
    py3-yaml \
    ca-certificates

ARG XRAY_VERSION=25.1.1

RUN apk add --no-cache --virtual .fetch-deps curl unzip \
    && curl -fsSL -o /tmp/xray.zip "https://github.com/XTLS/Xray-core/releases/download/v${XRAY_VERSION}/Xray-linux-64.zip" \
    && mkdir -p /tmp/xray \
    && unzip /tmp/xray.zip -d /tmp/xray \
    && mv /tmp/xray/xray /usr/local/bin/xray \
    && chmod +x /usr/local/bin/xray \
    && rm -rf /tmp/xray.zip /tmp/xray \
    && apk del .fetch-deps

COPY --from=sing-box-builder /out/sing-box /usr/local/bin/sing-box

WORKDIR /app

COPY start.sh /app/start.sh
COPY quota-controller.py /app/quota-controller.py

RUN chmod +x /app/start.sh \
    && mkdir -p /etc/sing-box /run/sing-box /data /app/config

ENV CUSTOM_CONFIG=/app/config/custom-config.yaml
ENV YAML_CONFIG=/app/config/config.yaml
ENV COMBINED_CONFIG=/app/config/config.json
ENV SINGBOX_CONFIG=/app/config/sing-box.json
ENV SINGBOX_GENERATED_CONFIG=/run/sing-box/config.json
ENV SINGBOX_LAST_GOOD_CONFIG=/run/sing-box/config.last-good.json
ENV STATE_FILE=/data/usage-state.json
ENV SINGBOX_API_SERVER=127.0.0.1:10085
ENV SINGBOX_API_MAX_FAILURES=5
ENV SINGBOX_STARTUP_GRACE_SECONDS=1
ENV CHECK_INTERVAL_SECONDS=30
ENV RESET_INTERVAL_HOURS=24

EXPOSE 8081

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD pidof sing-box >/dev/null || exit 1

CMD ["/app/start.sh"]
