#!/bin/sh
# Keeps calienfriito-utec.duckdns.org pointing at this VM. With an empty ip= DuckDNS takes the
# caller's address, which on GCP is the VM's external IP, so a new IP is picked up on its own.
# The token lives only on the VM, in /etc/duckdns/token (root, 600): never in git. It goes to curl
# through stdin, not as an argument, so it does not show up in the process list.
DOMAIN=calienfriito-utec
TOKEN=$(cat /etc/duckdns/token) || exit 1
R=$(printf 'url = "https://www.duckdns.org/update?domains=%s&token=%s&ip="\n' "$DOMAIN" "$TOKEN" | curl -fsS --max-time 15 -K -)
logger -t duckdns "update: $R"
[ "$R" = "OK" ]
