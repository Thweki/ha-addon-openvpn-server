port ${port}
proto ${protocol}
dev ${device}0
user root
group root
topology subnet
server ${vpn_subnet} ${vpn_netmask}
ca ${ca_cert}
cert ${server_cert}
key ${server_key}
crl-verify ${crl}
tls-version-min ${tls_version_min}
remote-cert-tls client
persist-key
persist-tun
keepalive ${keepalive}
data-ciphers ${data_ciphers}
data-ciphers-fallback ${cipher}
auth ${auth}
dh none
${tls_crypt_line}
${duplicate_cn}
${client_to_client}
${inactive_timeout}
${redirect_gateway}
${push_dns}
${push_routes}
${client_config_dir_line}
status ${status_path} ${status_interval}
verb ${verb}
${explicit_exit_notify}
