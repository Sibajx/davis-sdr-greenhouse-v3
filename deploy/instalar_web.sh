#!/usr/bin/env bash
# Instala el servicio del dashboard. Uso: sudo bash deploy/instalar_web.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Ejecuta con sudo"; exit 1; }
DIR="$(cd "$(dirname "$0")/.." && pwd)"
USUARIO=alternas
pkill -u $USUARIO -f "python3 -m davis_web" 2>/dev/null && sleep 1 || true   # ejecución temporal
install -d -o $USUARIO -g $USUARIO "$DIR/datos/web" "$DIR/datos/web/.matplotlib" "$DIR/datos/logs"
install -m 644 "$DIR/deploy/davis-web.service" /etc/systemd/system/davis-web.service
systemctl daemon-reload
systemctl enable --now davis-web
sleep 2
systemctl --no-pager status davis-web | head -8
curl -s -o /dev/null -w "Prueba local: HTTP %{http_code}\n" http://127.0.0.1:8090/ || true
if ! sudo -u $USUARIO python3 -c "import sys; sys.path.insert(0,'$DIR'); from davis_monitor import config; from davis_web.auth import Autenticador; sys.exit(0 if Autenticador(config.RUTAS.usuarios_db).hay_usuarios() else 1)"; then
  echo; echo ">>> Aún no hay cuentas. Crea tu administrador con:"
  echo "    cd $DIR && python3 -m davis_web admin crear <tu_usuario>"
fi
