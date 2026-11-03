#!/usr/bin/env bash
# Publica el dashboard en https://<subdominio>.<tu-dominio> con un túnel de Cloudflare.
# No abre puertos en el router: cloudflared sale hacia Cloudflare y el tráfico entra por ahí.
#
# Uso (como tu usuario, NO con sudo; el script pide sudo solo al final):
#   bash deploy/configurar_cloudflare.sh davis.midominio.com
set -euo pipefail
HOST="${1:?Uso: bash deploy/configurar_cloudflare.sh subdominio.tudominio.com}"
TUNEL="davis-dashboard"
CF=~/.cloudflared

command -v cloudflared >/dev/null || { echo "Falta cloudflared"; exit 1; }

if [[ ! -f $CF/cert.pem ]]; then
  echo "1) Autoriza esta Pi en tu cuenta de Cloudflare (se abre/imprime una URL; elige el dominio):"
  cloudflared tunnel login
fi

if ! cloudflared tunnel list 2>/dev/null | grep -qw "$TUNEL"; then
  echo "2) Creando túnel $TUNEL"
  cloudflared tunnel create "$TUNEL"
fi
UUID=$(cloudflared tunnel list -o json | python3 -c "import json,sys;print([t['id'] for t in json.load(sys.stdin) if t['name']=='$TUNEL'][0])")

echo "3) DNS: $HOST → túnel"
cloudflared tunnel route dns --overwrite-dns "$TUNEL" "$HOST"

echo "4) Configuración"
sudo install -d -m 755 /etc/cloudflared
sudo install -m 600 "$CF/$UUID.json" "/etc/cloudflared/$UUID.json"
sudo tee /etc/cloudflared/config.yml >/dev/null <<YML
tunnel: $UUID
credentials-file: /etc/cloudflared/$UUID.json
ingress:
  - hostname: $HOST
    service: http://127.0.0.1:8090
    originRequest:
      connectTimeout: 10s
  - service: http_status:404
YML

echo "5) Servicio cloudflared"
pkill -u "$(id -un)" -f "config-davis.yml run" 2>/dev/null || true   # túnel temporal lanzado a mano
if ! systemctl list-unit-files | grep -q '^cloudflared.service'; then
  sudo cloudflared --config /etc/cloudflared/config.yml service install
else
  sudo systemctl restart cloudflared
fi
sleep 4
systemctl --no-pager status cloudflared | head -6
echo
echo "Listo: https://$HOST   (puede tardar 1-2 min en propagarse)"
