#!/usr/bin/env bash
# Orsat-Decoder — désinstallation
APPDIR="${ORSAT_DATA:-$HOME/Orsat-Decoder}"
echo "Suppression d'Orsat-Decoder : $APPDIR"
echo "Le dossier $APPDIR est supprimé en entier, configuration (serveurs, canaux) comprise."
read -r -p "Continuer ? [o/N] " ans
case "$ans" in [oOyY]*) ;; *) echo "Annulé."; exit 0;; esac
rm -f "$HOME/.local/bin/orsat-decoder" "$HOME/.local/share/applications/orsat-decoder.desktop" \
      "$HOME/.local/share/icons/hicolor/scalable/apps/orsat-decoder.svg"
rm -rf "$APPDIR"
update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
echo "Orsat-Decoder est désinstallé."
