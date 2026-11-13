#!/bin/sh
# Certbot deploy hook: copies the certificate for Mosquitto (it runs as uid 1883 and cannot read /etc/letsencrypt) and restarts it.
# Certificate name (certbot --cert-name): valid for calienfriito-utec.duckdns.org and the old sslip.io name.
D=calienfriito
mkdir -p /opt/ccs-mqtt-certs
cp -L /etc/letsencrypt/live/$D/fullchain.pem /etc/letsencrypt/live/$D/privkey.pem /opt/ccs-mqtt-certs/
chown -R 1883:1883 /opt/ccs-mqtt-certs && chmod 600 /opt/ccs-mqtt-certs/privkey.pem
docker restart ccs_mosquitto >/dev/null 2>&1 || true
docker restart ccs_frontend >/dev/null 2>&1 || true
