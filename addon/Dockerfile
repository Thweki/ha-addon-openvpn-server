ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache \
    bash \
    coreutils \
    easy-rsa \
    iproute2 \
    iptables \
    openvpn \
    python3

COPY rootfs/ /
COPY src /opt/openvpn_server/src
COPY templates /opt/openvpn_server/templates

RUN chmod +x /etc/services.d/openvpn/run /usr/local/bin/openvpn-addon

ENV PYTHONPATH=/opt/openvpn_server/src
