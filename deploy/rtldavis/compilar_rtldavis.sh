#!/usr/bin/env bash
# Compila rtldavis (fork lheijst, v0.15) con el parche de davis-sdr que reporta
# las tramas descartadas por CRC ("CRC ERROR <hex> ok=N"). Sin el parche el
# monitor de CRC del dashboard no tiene datos: rtldavis las tira en silencio.
#
# rtldavis es GPL-3.0 (https://github.com/lheijst/rtldavis); este parche también.
# davis_monitor (MIT) solo lee su salida como programa aparte.
#
# Uso:  bash deploy/rtldavis/compilar_rtldavis.sh [destino]
#       destino por defecto: ~/.local/bin/rtldavis-crc  (no requiere sudo)
# Luego en config.json:  "rtldavis_cmd": "/home/USUARIO/.local/bin/rtldavis-crc"
# y reiniciar el receptor:  davis reiniciar
set -euo pipefail

COMMIT=b95d5d734e46            # lheijst/rtldavis "Version 0.15"
AQUI="$(cd "$(dirname "$0")" && pwd)"
DESTINO="${1:-$HOME/.local/bin/rtldavis-crc}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

command -v go >/dev/null || { echo "Falta Go (sudo apt install golang-go)"; exit 1; }
command -v git >/dev/null || { echo "Falta git"; exit 1; }
pkg-config --exists librtlsdr 2>/dev/null || echo "Aviso: no encuentro librtlsdr-dev (sudo apt install librtlsdr-dev)"

echo "→ Descargando rtldavis ($COMMIT)…"
git clone -q https://github.com/lheijst/rtldavis "$TMP/rtldavis"
cd "$TMP/rtldavis"
git checkout -q "$COMMIT"
rm -rf vendor
echo "→ Aplicando parche de CRC…"
patch -p1 < "$AQUI/rtldavis-crc.patch"
cat > go.mod <<'EOF'
module github.com/lheijst/rtldavis

go 1.21

require github.com/jpoirier/gortlsdr v2.10.0+incompatible
EOF
echo "→ Compilando…"
GOFLAGS=-mod=mod go build -o rtldavis-crc .
mkdir -p "$(dirname "$DESTINO")"
install -m 755 rtldavis-crc "$DESTINO"
echo "✓ Instalado en $DESTINO"
echo "  Ahora pon en config.json:  \"rtldavis_cmd\": \"$DESTINO\"  y ejecuta: davis reiniciar"
