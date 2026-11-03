#!/usr/bin/env bash
# Instala/actualiza el servicio davis-monitor (v3). Uso: sudo bash deploy/instalar_servicio.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Ejecuta con sudo"; exit 1; }
DIR="$(cd "$(dirname "$0")/.." && pwd)"
USUARIO=alternas
DATOS=$DIR/datos
UNIT=/etc/systemd/system/davis-monitor.service

id "$USUARIO" >/dev/null || { echo "No existe el usuario $USUARIO"; exit 1; }
command -v rtldavis >/dev/null || { echo "rtldavis no está en el PATH"; exit 1; }

echo "1) Deteniendo servicio anterior (si existe)"
systemctl stop davis-monitor 2>/dev/null || true
systemctl reset-failed davis-monitor 2>/dev/null || true
if [[ -f $UNIT ]]; then cp "$UNIT" "$UNIT.bak-$(date +%Y%m%d-%H%M%S)"; fi

echo "2) Deteniendo ejecuciones temporales (lanzadas a mano con nohup) y comprobando el dongle"
pkill -u "$USUARIO" -f "python3 -m davis_monitor --headless" 2>/dev/null && sleep 6 || true
if pgrep -x rtldavis >/dev/null; then
  echo "   AVISO: hay un rtldavis corriendo (pid $(pgrep -x rtldavis | tr '\n' ' ')). Ciérralo (TUI abierta?) y vuelve a ejecutar."
  exit 1
fi

echo "3) Carpeta de datos: $DATOS"
for sub in csv db logs estado exportaciones; do
  install -d -o "$USUARIO" -g "$USUARIO" "$DATOS/$sub"
done
if [[ "$DIR" != "/home/$USUARIO/Escritorio/files/davis_v2" ]]; then
  echo "   AVISO: el proyecto está en $DIR; ajusta WorkingDirectory y ReadWritePaths en la unidad."
fi

echo "4) Regla udev del dongle"
install -m 644 "$DIR/deploy/99-rtlsdr.rules" /etc/udev/rules.d/99-rtlsdr.rules
udevadm control --reload-rules && udevadm trigger

echo "5) Instalando unidad"
install -m 644 "$DIR/deploy/davis-monitor.service" "$UNIT"
systemctl daemon-reload
systemctl enable --now davis-monitor
sleep 5
systemctl --no-pager status davis-monitor | head -15
echo
echo "Logs en vivo:   journalctl -u davis-monitor -f"
echo "Visor:          cd $DIR && python3 -m davis_monitor --visor"
