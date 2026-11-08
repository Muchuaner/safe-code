#!/bin/sh
set -eu
if [ "${RENEWED_LINEAGE:-}" = /etc/letsencrypt/live/safecode.tommukan.dpdns.org ]; then
    /usr/sbin/nginx -t
    systemctl reload nginx
fi
