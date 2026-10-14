client
dev tun
proto ${protocol}
remote ${remote_host} ${remote_port}
resolv-retry infinite
nobind
persist-key
persist-tun
remote-cert-tls server
data-ciphers ${data_ciphers}
tls-version-min ${tls_version_min}
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
