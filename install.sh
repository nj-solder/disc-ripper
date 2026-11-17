#!/usr/bin/env bash
# Disc Ripper installer for Linux.
#   Run from the unzipped disc-ripper folder:   ./install.sh        (asks before anything optional)
#                                               ./install.sh -y     (yes to everything optional)
# Installs into ~/.local/share/disc-ripper (no root needed), adds a menu entry, a desktop icon and the
# `disc-ripper` command. Run it again to update. Uninstall with ./uninstall.sh.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/disc-ripper"
BIN="$HOME/.local/bin"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
YES=0; [ "${1:-}" = "-y" ] && YES=1

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
fail() { printf '\n\033[31m✗ %s\033[0m\n' "$*"; exit 1; }
ask()  { # ask "question" default(Y/N) -> 0 = yes
  local def=$2 a
  [ $YES = 1 ] && return 0
  read -r -p "  $1 [$( [ "$def" = Y ] && echo Y/n || echo y/N )] " a </dev/tty || a=""
  a=${a:-$def}; [[ $a =~ ^[Yy] ]]
}

[ -f "$SRC/disc_ripper/app.py" ] || fail "Run this from the unzipped disc-ripper folder (the one with disc_ripper/ inside)."
[ "$(id -u)" = 0 ] && fail "Don't run this with sudo - it installs for your own user."

bold "Disc Ripper installer"
echo "  from: $SRC"
echo "  to:   $DEST"
echo

# ---------------------------------------------------------------- 1. Python
bold "1. Python"
command -v python3 >/dev/null || fail "python3 is not installed. Install it with your package manager (e.g. sudo apt install python3)."
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || fail "Python 3.10 or newer is needed (you have $(python3 --version))."
python3 -c 'import venv, ensurepip' 2>/dev/null \
  || fail "Python's venv module is missing. Install it (Debian/Ubuntu/Kali: sudo apt install python3-venv) and run ./install.sh again."
ok "$(python3 --version)"

# ---------------------------------------------------------------- 2. copy the app + its own Python environment
bold "2. Installing the app"
mkdir -p "$DEST"
rm -rf "$DEST/disc_ripper"
cp -r "$SRC/disc_ripper" "$DEST/"
for f in README.md CLAUDE.md AGENTS.md requirements.txt uninstall.sh install.sh; do
  [ -f "$SRC/$f" ] && cp "$SRC/$f" "$DEST/"
done
find "$DEST/disc_ripper" -name '__pycache__' -prune -exec rm -rf {} +
ok "app files copied"
if [ ! -x "$DEST/.venv/bin/python" ]; then
  python3 -m venv "$DEST/.venv" || fail "Couldn't create the Python environment."
fi
echo "  installing PySide6 (the window toolkit) - this downloads ~150 MB the first time…"
"$DEST/.venv/bin/pip" install -q --upgrade pip >/dev/null 2>&1 || true
"$DEST/.venv/bin/pip" install -q -r "$DEST/requirements.txt" || fail "Installing PySide6 failed (no internet?)."
"$DEST/.venv/bin/python" -c 'import PySide6.QtWidgets' 2>/dev/null || fail "PySide6 installed but doesn't load."
ok "Python environment ready"

# ---------------------------------------------------------------- 3. MakeMKV + ffmpeg (needed to rip / count episodes)
bold "3. MakeMKV (does the actual disc reading)"
MKV=""
if command -v flatpak >/dev/null && flatpak info com.makemkv.MakeMKV >/dev/null 2>&1; then
  MKV=flatpak; ok "MakeMKV Flatpak is installed"
elif command -v makemkvcon >/dev/null; then
  MKV=native; ok "MakeMKV (makemkvcon) is installed"
else
  warn "MakeMKV is not installed - Disc Ripper can't rip without it."
  if command -v flatpak >/dev/null; then
    if ask "Install the MakeMKV Flatpak from Flathub now?" Y; then
      flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
      flatpak install --user -y flathub com.makemkv.MakeMKV && MKV=flatpak && ok "MakeMKV installed"
    fi
  else
    warn "Install it later: https://www.makemkv.com/forum/viewtopic.php?f=3&t=224 (or install flatpak, then:"
    warn "  flatpak install flathub com.makemkv.MakeMKV )"
  fi
fi
if [ "$MKV" = flatpak ]; then
  echo "  The MakeMKV Flatpak needs permission to save onto USB/external drives (/media, /run/media)."
  if ask "Allow that now?" Y; then
    flatpak override --user --filesystem=/media --filesystem=/run/media com.makemkv.MakeMKV && ok "permission given"
  fi
fi
if command -v ffprobe >/dev/null && command -v ffmpeg >/dev/null; then
  ok "ffmpeg is installed (used to count episodes and find duplicates)"
else
  warn "ffmpeg is not installed. Ripping works, but episode counts / Kodi names need it."
  warn "  Install it: sudo apt install ffmpeg   (or your distro's package manager)"
fi

# ---------------------------------------------------------------- 4. launchers
bold "4. Menu entry, desktop icon and 'disc-ripper' command"
mkdir -p "$BIN" "$APPS"
cat > "$BIN/disc-ripper" <<EOF
#!/usr/bin/env bash
cd "$DEST" && exec "$DEST/.venv/bin/python" -m disc_ripper "\$@"
EOF
chmod +x "$BIN/disc-ripper"
DESKTOP_ENTRY="[Desktop Entry]
Type=Application
Name=Disc Ripper
Comment=Rip TV box sets and movies with MakeMKV
Exec=$BIN/disc-ripper
Path=$DEST
Icon=media-optical
Terminal=false
Categories=AudioVideo;Video;
StartupWMClass=disc-ripper"
printf '%s\n' "$DESKTOP_ENTRY" > "$APPS/disc-ripper.desktop"
chmod +x "$APPS/disc-ripper.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" >/dev/null 2>&1 || true
ok "app menu entry (look under Multimedia / Video)"
DESK="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
{ [ -z "$DESK" ] || [ "$DESK" = "$HOME" ]; } && DESK="$HOME/Desktop"
if [ -d "$DESK" ] && ask "Put a Disc Ripper icon on the desktop?" Y; then
  printf '%s\n' "$DESKTOP_ENTRY" > "$DESK/Disc Ripper.desktop"
  chmod +x "$DESK/Disc Ripper.desktop"
  if command -v gio >/dev/null; then   # mark it trusted so it opens without a "launcher not trusted" prompt
    gio set "$DESK/Disc Ripper.desktop" metadata::trusted true 2>/dev/null || true
    gio set -t string "$DESK/Disc Ripper.desktop" metadata::xfce-exe-checksum \
      "$(sha256sum "$DESK/Disc Ripper.desktop" | cut -d' ' -f1)" 2>/dev/null || true
  fi
  ok "desktop icon"
fi
case ":$PATH:" in *":$BIN:"*) ok "command: disc-ripper" ;; *) warn "command installed at $BIN/disc-ripper (that folder isn't on your PATH - the menu/desktop icons still work)";; esac

# ---------------------------------------------------------------- 5. quick self-test
bold "5. Self-test"
if QT_QPA_PLATFORM=offscreen timeout 60 "$BIN/disc-ripper" --offscreen-smoke >/dev/null 2>&1; then
  ok "Disc Ripper starts correctly"
else
  warn "Self-test failed - try running: $BIN/disc-ripper   to see the error"
fi

# ---------------------------------------------------------------- done + required notice
echo
bold "Done! Open Disc Ripper from your app menu or the desktop icon."
echo
bold "Please note - automatic MakeMKV updates (on by default):"
cat <<'EOF'
  Every time Disc Ripper opens, it automatically:
    • updates MakeMKV (Flatpak only):  flatpak update com.makemkv.MakeMKV
    • downloads the free public MakeMKV beta key from the official MakeMKV forum
      and writes it into ~/.MakeMKV/settings.conf (a backup of the old file is kept).
  This keeps MakeMKV working (its beta versions and keys expire every month or two).
  To turn either off: Disc Ripper → Settings… → untick
    "Update MakeMKV automatically when Disc Ripper opens" and/or
    "Get the latest MakeMKV beta key when Disc Ripper opens".

  First steps: Settings… → choose where rips are saved (default ~/Videos/Rips).
  Full guide: README.md (also copied to the install folder).
EOF
