#!/usr/bin/env bash
# =====================================================================================
#  Orsat-Decoder (Beta 1.0.0) — installation sous Linux
#  F1NSK et Claude AI — licence GPL-3 (fichier LICENSE)
#
#  Usage :  ./install.sh
#  Relancer install.sh met à jour Orsat-Decoder en gardant la configuration et les canaux.
# =====================================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPDIR="${ORSAT_DATA:-$HOME/Orsat-Decoder}"   # tout est là : logiciel, venv, config, journal
OLDDIR="$HOME/.local/share/orsat-decoder"     # emplacement des versions précédentes
BINDIR="$HOME/.local/bin"
DESKDIR="$HOME/.local/share/applications"
ICONDIR="$HOME/.local/share/icons/hicolor/scalable/apps"

gold=$'\e[33m'; red=$'\e[31m'; dim=$'\e[2m'; bold=$'\e[1m'; off=$'\e[0m'
step() { echo; echo "${gold}${bold}▸ $*${off}"; }
info() { echo "  $*"; }
fail() { echo; echo "${red}${bold}Échec :${off} $*"; exit 1; }

[ "$(id -u)" = "0" ] && fail "lancez install.sh avec votre compte habituel, pas en root (sudo sera demandé au besoin)."
echo "${bold}Orsat-Decoder${off} — installation"

# -------------------------------------------------------------------------------------
step "1/6  Paquets système (Python, compilateur C)"
need=()
command -v python3 >/dev/null || need+=(python3)
python3 -c "import venv, ensurepip" 2>/dev/null || need+=(python3-venv)
command -v cc >/dev/null || command -v gcc >/dev/null || need+=(gcc)
command -v make >/dev/null || need+=(make)
if [ ${#need[@]} -gt 0 ]; then
  info "À installer : ${need[*]}"
  if command -v apt-get >/dev/null; then
    sudo apt-get update -q && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3 python3-venv python3-pip build-essential
  elif command -v dnf >/dev/null; then sudo dnf install -y python3 python3-pip gcc make
  elif command -v pacman >/dev/null; then sudo pacman -S --needed --noconfirm python python-pip base-devel
  elif command -v zypper >/dev/null; then sudo zypper install -y python3 python3-pip gcc make
  else fail "installez Python 3 (avec venv), gcc et make, puis relancez."; fi
else
  info "Tout est présent ($(python3 --version))."
fi
python3 - <<'PY' || fail "Python 3.9 ou plus récent est nécessaire."
import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)
PY

# -------------------------------------------------------------------------------------
step "2/6  Copie du logiciel dans $APPDIR"
mkdir -p "$APPDIR"
if [ "$(cd "$APPDIR" && pwd -P)" != "$(cd "$HERE" && pwd -P)" ]; then
  for d in orsatdec web native tests; do
    rm -rf "$APPDIR/$d.new"
    cp -a "$HERE/$d" "$APPDIR/$d.new"
    rm -rf "$APPDIR/$d"
    mv "$APPDIR/$d.new" "$APPDIR/$d"
  done
  for f in install.sh get.sh uninstall.sh README.md LICENSE NOTICE .gitignore; do
    [ -f "$HERE/$f" ] && cp "$HERE/$f" "$APPDIR/$f"
  done
fi
find "$APPDIR" -name __pycache__ -prune -exec rm -rf {} +
# reprise des versions précédentes (~/.local/share/orsat-decoder et ~/.config/orsat-decoder)
if [ ! -f "$APPDIR/config.json" ] && [ -f "$HOME/.config/orsat-decoder/config.json" ]; then
  cp "$HOME/.config/orsat-decoder/config.json" "$APPDIR/config.json" && info "Configuration reprise de ~/.config/orsat-decoder"
fi
[ -f "$APPDIR/config.json" ] && rm -rf "$HOME/.config/orsat-decoder"
if [ -d "$OLDDIR" ] && [ "$(cd "$OLDDIR" && pwd -P)" != "$(cd "$APPDIR" && pwd -P)" ]; then
  [ -d "$OLDDIR/browser-profile" ] && [ ! -d "$APPDIR/browser-profile" ] && mv "$OLDDIR/browser-profile" "$APPDIR/"
  rm -rf "$OLDDIR" && info "Ancienne installation (~/.local/share/orsat-decoder) supprimée"
fi
info "Installé dans $APPDIR"

# -------------------------------------------------------------------------------------
step "3/6  Décodeurs natifs"
"$APPDIR/native/build.sh" >"$APPDIR/build.log" 2>&1 || { tail -20 "$APPDIR/build.log"; fail "compilation des décodeurs natifs."; }
info "$(tail -1 "$APPDIR/build.log")"

# -------------------------------------------------------------------------------------
step "4/6  Environnement Python (numpy, scipy, aiohttp…)"
if [ ! -x "$APPDIR/venv/bin/python" ]; then
  python3 -m venv "$APPDIR/venv" || fail "création de l'environnement Python (paquet python3-venv manquant ?)."
fi
"$APPDIR/venv/bin/pip" install -q --upgrade pip >/dev/null 2>&1 || true
"$APPDIR/venv/bin/pip" install -q numpy scipy aiohttp cbor2 zstandard pyflac || fail "installation des modules Python (connexion Internet ?)."
info "Modules installés."
command -v parec >/dev/null || command -v pw-record >/dev/null || \
  info "${dim}Note : ni parec ni pw-record trouvés ; la source « Entrée audio » demande pulseaudio-utils ou pipewire-bin.${off}"

# -------------------------------------------------------------------------------------
step "5/6  Bibliothèque de signaux (images et sons de référence, hors ligne)"
# Installée une seule fois, puis seulement quand sa version change ; Orsat-Decoder la lit sans Internet.
REFDIR="$APPDIR/references"
REFURL="https://github.com/f1nsk40260/Orsat-Decoder/releases/download/bibliotheque"
have="$(cat "$REFDIR/VERSION" 2>/dev/null || true)"
pack=""; dl=""
if [ -n "${ORSAT_REFPACK:-}" ] && [ -f "$ORSAT_REFPACK" ]; then pack="$ORSAT_REFPACK"
elif [ -f "$HERE/orsat-references.tar" ]; then pack="$HERE/orsat-references.tar"
else
  # pack déposé dans le dossier de téléchargements : installé s'il est d'une autre version
  for f in "$HOME/Téléchargements/orsat-references.tar" "$HOME/Downloads/orsat-references.tar"; do
    if [ -f "$f" ]; then
      v="$(tar xOf "$f" VERSION 2>/dev/null | tr -cd '0-9.')"
      if [ -n "$v" ] && [ "$v" != "$have" ]; then pack="$f"; fi
      break
    fi
  done
fi
if [ -z "$pack" ]; then
  want="$(curl -fsSL "$REFURL/references.version" 2>/dev/null || wget -qO- "$REFURL/references.version" 2>/dev/null || true)"
  want="$(echo "$want" | head -1 | tr -cd '0-9.')"
  if [ -z "$want" ]; then
    if [ -n "$have" ]; then info "Version $have en place."; else info "${dim}Pas encore disponible au téléchargement : le panneau « Comparer » fonctionnera sans référence.${off}"; fi
  elif [ "$want" = "$have" ]; then
    info "Version $have déjà installée."
  else
    info "Téléchargement de la version $want (environ 85 Mo, une seule fois)…"
    pack="$(mktemp --suffix=.tar)"; dl="$pack"
    if ! { curl -fL --progress-bar "$REFURL/orsat-references.tar" -o "$pack" 2>/dev/null || wget -q --show-progress -O "$pack" "$REFURL/orsat-references.tar"; }; then
      rm -f "$pack"; pack=""; dl=""; info "${dim}Téléchargement impossible ; nouvel essai à la prochaine mise à jour.${off}"
    fi
  fi
fi
if [ -n "$pack" ]; then
  rm -rf "$REFDIR.new" && mkdir -p "$REFDIR.new"
  if tar xf "$pack" -C "$REFDIR.new" && [ -f "$REFDIR.new/index.json" ]; then
    rm -rf "$REFDIR" && mv "$REFDIR.new" "$REFDIR"
    info "Bibliothèque installée : version $(cat "$REFDIR/VERSION"), $(find "$REFDIR" -name signal.json | wc -l) signaux."
  else
    rm -rf "$REFDIR.new"; info "${dim}Pack de références illisible : ignoré.${off}"
  fi
  if [ -n "$dl" ]; then rm -f "$dl"; fi   # seulement la copie téléchargée ici, jamais un pack fourni
fi

# -------------------------------------------------------------------------------------
step "6/6  Lanceur, menu et autotest"
mkdir -p "$BINDIR" "$DESKDIR" "$ICONDIR"
cat > "$BINDIR/orsat-decoder" <<EOF
#!/usr/bin/env bash
# Orsat-Decoder — lanceur
export ORSAT_DATA="$APPDIR"
# --identify FICHIER : chemin rendu absolu avant de changer de dossier
if [ "\${1:-}" = "--identify" ] && [ -n "\${2:-}" ]; then
  set -- "\$1" "\$(realpath -- "\$2")" "\${@:3}"
fi
cd "$APPDIR"
case "\${1:-}" in
  --check) exec "$APPDIR/venv/bin/python" tests/selftest.py ;;
  --log)   exec \${PAGER:-less} "$APPDIR/orsat-decoder.log" ;;
esac
exec "$APPDIR/venv/bin/python" -m orsatdec "\$@"
EOF
chmod 755 "$BINDIR/orsat-decoder"
install -m 755 "$HERE/uninstall.sh" "$APPDIR/uninstall.sh"
cp "$HERE/web/icon.svg" "$ICONDIR/orsat-decoder.svg"
cat > "$DESKDIR/orsat-decoder.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Orsat-Decoder
GenericName=Décodeur de modes numériques
Comment=Décodeur multimode pour PhantomSDR / Orsat-SDR
Exec=$BINDIR/orsat-decoder
Icon=orsat-decoder
Terminal=false
Categories=HamRadio;Network;AudioVideo;
Keywords=radio;ham;sdr;psk;rtty;cw;navtex;ft8;decoder;phantomsdr;
StartupWMClass=orsat-decoder
EOF
update-desktop-database "$DESKDIR" >/dev/null 2>&1 || true
gtk-update-icon-cache -q "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true

if "$BINDIR/orsat-decoder" --check; then
  echo
  echo "${gold}${bold}Installation terminée.${off}"
  echo "  Lancez ${bold}Orsat-Decoder${off} depuis le menu des applications, ou tapez :  ${bold}orsat-decoder${off}"
  echo "  La source (PhantomSDR / Orsat-SDR, TCI, entrée audio) se choisit en haut à gauche ; elles se règlent dans les réglages."
  echo "  Désinstallation :  $APPDIR/uninstall.sh"
  case ":$PATH:" in *":$BINDIR:"*) ;; *) echo "  ${dim}Note : $BINDIR n'est pas dans votre PATH ; utilisez le menu ou ouvrez un nouveau terminal.${off}";; esac
  if ! command -v chromium >/dev/null && ! command -v chromium-browser >/dev/null && ! command -v google-chrome >/dev/null && ! command -v brave-browser >/dev/null; then
    echo "  ${dim}Astuce : avec Chromium installé, l'interface s'ouvre dans sa propre fenêtre et Orsat-Decoder s'arrête à sa fermeture.${off}"
  fi
else
  fail "l'autotest des décodeurs a échoué (détails ci-dessus)."
fi
