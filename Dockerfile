FROM alpine:3.20

RUN apk add --no-cache \
    bash \
    curl \
    unzip \
    tar \
    python3 \
    py3-yaml \
    nginx \
    ca-certificates \
    jq \
    procps

ARG XRAY_VERSION=25.1.1
ARG SINGBOX_VERSION=1.11.15

RUN curl -L -o /tmp/xray.zip "https://github.com/XTLS/Xray-core/releases/download/v${XRAY_VERSION}/Xray-linux-64.zip" \
    && mkdir -p /tmp/xray \
    && mkdir -p /usr/local/share/xray \
    && unzip /tmp/xray.zip -d /tmp/xray \
    && mv /tmp/xray/xray /usr/local/bin/xray \
    && mv /tmp/xray/geoip.dat /usr/local/share/xray/geoip.dat \
    && mv /tmp/xray/geosite.dat /usr/local/share/xray/geosite.dat \
    && chmod +x /usr/local/bin/xray \
    && rm -rf /tmp/xray.zip /tmp/xray

RUN curl -L -o /tmp/sing-box.tar.gz "https://github.com/SagerNet/sing-box/releases/download/v${SINGBOX_VERSION}/sing-box-${SINGBOX_VERSION}-linux-amd64.tar.gz" \
    && tar -xzf /tmp/sing-box.tar.gz -C /tmp \
    && mv "/tmp/sing-box-${SINGBOX_VERSION}-linux-amd64/sing-box" /usr/local/bin/sing-box \
    && chmod +x /usr/local/bin/sing-box \
    && rm -rf /tmp/sing-box.tar.gz "/tmp/sing-box-${SINGBOX_VERSION}-linux-amd64"

WORKDIR /app

COPY start.sh /app/start.sh
COPY quota-controller.py /app/quota-controller.py

RUN chmod +x /app/start.sh

ENV SINGBOX_CONFIG=/app/config/sing-box.json
ENV SINGBOX_GENERATED_CONFIG=/etc/sing-box/config.json
ENV SINGBOX_LAST_GOOD_CONFIG=/etc/sing-box/config.last-good.json
ENV XRAY_TEMPLATE=/app/config/xray-template.json
ENV QUOTA_CONFIG=/app/config/quota.json
ENV COMBINED_CONFIG=/app/config/config.json
ENV CONFIG_MODE=auto
ENV RUNTIME_CONFIG_DIR=/run/xray-quota
ENV XRAY_GENERATED_CONFIG=/etc/xray/config.json
ENV XRAY_LAST_GOOD_CONFIG=/etc/xray/config.last-good.json
ENV STATE_FILE=/data/usage-state.json
ENV XRAY_API_SERVER=127.0.0.1:10085
ENV XRAY_INBOUND_TAG=vless-in
ENV XRAY_API_MAX_FAILURES=5
ENV XRAY_STARTUP_GRACE_SECONDS=1
ENV SINGBOX_STARTUP_GRACE_SECONDS=1
ENV ENABLE_HTTP_FRONTEND=true
ENV PUBLIC_HTTP_PORT=8080
ENV XRAY_PROXY_HOST=127.0.0.1
ENV XRAY_PROXY_PORT=10000
ENV XRAY_WS_PATH=
ENV NGINX_GENERATED_CONFIG=/etc/nginx/http.d/xray-quota.conf
ENV QUOTA_UI_HOST=0.0.0.0
ENV QUOTA_UI_PORT=9090
ENV XRAY_LOCATION_ASSET=/usr/local/share/xray

EXPOSE 8080 9090

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD pgrep -x sing-box >/dev/null && pgrep -x xray >/dev/null && { [ "$ENABLE_HTTP_FRONTEND" != "true" ] || pgrep -x nginx >/dev/null; } || exit 1

CMD ["/app/start.sh"]
