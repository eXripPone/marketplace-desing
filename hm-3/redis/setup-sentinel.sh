#!/bin/sh
set -eu
# Sentinel must be able to rewrite its config after electing a new master.
config=/data/flights.conf
if [ ! -s "$config" ]; then
    cat > "$config" <<EOF
port 26379
dir /data
sentinel announce-ip ${SENTINEL_IP}
sentinel monitor flights 172.29.83.11 6379 2
sentinel down-after-milliseconds flights 3000
sentinel failover-timeout flights 10000
sentinel parallel-syncs flights 1
EOF
fi
exec redis-sentinel "$config"
