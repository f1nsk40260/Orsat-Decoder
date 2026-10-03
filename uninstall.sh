#!/usr/bin/env bash
# Orsat-Decoder — désinstallation
APPDIR="${ORSAT_DATA:-$HOME/.local/share/orsat-decoder}"
echo "Suppression d'Orsat-Decoder : $APPDIR"
echo "La configuration (~/.config/orsat-decoder : serveurs, canaux) est conservée, sauf avec --all."
read -r -p "Continuer ? [o/N] " ans
case "$ans" in [oOyY]*) ;; *) echo "Annulé."; exit 0;; esac
rm -f "$HOME/.local/bin/orsat-decoder" "$HOME/.local/share/applications/orsat-decoder.desktop" \
      "$HOME/.local/share/icons/hicolor/scalable/apps/orsat-decoder.svg"
rm -rf "$APPDIR"
[ "${1:-}" = "--all" ] && rm -rf "$HOME/.config/orsat-decoder"
update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
echo "Orsat-Decoder est désinstallé."
