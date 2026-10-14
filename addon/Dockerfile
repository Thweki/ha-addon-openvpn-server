# Pinned multi-arch base image, built by .github/workflows/builder.yaml on GitHub Actions and,
# as a fallback, locally by the Supervisor on the Home Assistant host.
# Supervisor no longer provides BUILD_FROM by default, so the base has to be named here.
FROM ghcr.io/home-assistant/base:3.22

# Home Assistant OS hosts in mainland China cannot reach dl-cdn.alpinelinux.org (Fastly).
# Point apk at a domestic mirror before installing packages. Delete this line (or change the
# mirror) if you build this add-on from a network where the official CDN is reachable.
RUN sed -i 's|https://dl-cdn.alpinelinux.org|https://mirrors.aliyun.com|g' /etc/apk/repositories

RUN apk add --no-cache \
    bash \
    coreutils \
    easy-rsa \
    iproute2 \
    iptables \
    openssl \
    openvpn \
    python3

COPY rootfs/ /
COPY src /opt/openvpn_server/src
COPY templates /opt/openvpn_server/templates

RUN chmod +x /etc/services.d/openvpn/run /usr/local/bin/openvpn-addon

ENV PYTHONPATH=/opt/openvpn_server/src
