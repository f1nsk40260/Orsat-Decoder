#!/usr/bin/env bash
# Orsat-Decoder — installation et mise à jour en une commande, sans git ni mot de passe :
#
#   curl -fsSL https://raw.githubusercontent.com/f1nsk40260/Orsat-Decoder/main/get.sh | bash
#
# Télécharge la dernière version, l'installe (ou la met à jour) et efface le téléchargement.
set -euo pipefail
URL="https://github.com/f1nsk40260/Orsat-Decoder/archive/refs/heads/main.tar.gz"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "Téléchargement d'Orsat-Decoder…"
if command -v curl >/dev/null; then curl -fsSL "$URL" -o "$TMP/o.tgz"
else wget -qO "$TMP/o.tgz" "$URL"; fi
tar xzf "$TMP/o.tgz" -C "$TMP"
# stdin = le clavier, pour que sudo puisse demander le mot de passe de la session Linux
bash "$TMP"/Orsat-Decoder-main/install.sh </dev/tty
