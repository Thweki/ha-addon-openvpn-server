client
dev tun
proto ${protocol}
remote ${remote_host} ${remote_port}
resolv-retry infinite
nobind
persist-key
persist-tun
remote-cert-tls server
cipher ${cipher}
auth ${auth}
verb 3

<ca>
${ca}
</ca>

<cert>
-----BEGIN CERTIFICATE-----
${cert}
-----END CERTIFICATE-----
</cert>

<key>
${key}
</key>

${tls_crypt_block}
