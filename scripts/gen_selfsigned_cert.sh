#!/usr/bin/env bash
# Gera certificado autoassinado em deploy/nginx/certs/ (fullchain.pem, privkey.pem).
# Uso: scripts/gen_selfsigned_cert.sh [IP_PUBLICO] [DNS]   ex.: ./gen_selfsigned_cert.sh 3.12.34.56 api.exemplo.com
set -euo pipefail
IP="${1:-}"; DNS="${2:-}"
OUT="$(cd "$(dirname "$0")/.." && pwd)/deploy/nginx/certs"
mkdir -p "$OUT"
SAN="DNS:localhost,IP:127.0.0.1"
[ -n "$IP" ]  && SAN="$SAN,IP:$IP"
[ -n "$DNS" ] && SAN="$SAN,DNS:$DNS"
CN="${DNS:-localhost}"
umask 077
openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 365 \
  -keyout "$OUT/privkey.pem" -out "$OUT/fullchain.pem" \
  -subj "/CN=$CN/O=HospedaSync" -addext "subjectAltName=$SAN"
chmod 644 "$OUT/fullchain.pem"; chmod 600 "$OUT/privkey.pem"
echo "Certificado gerado em $OUT (SAN: $SAN)"
