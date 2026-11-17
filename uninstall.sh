#!/usr/bin/env bash
# Disc Ripper uninstaller. Removes the app, its menu entry, desktop icon and command.
# Never touches your rips. Asks before removing your settings. MakeMKV is left installed.
set -uo pipefail
YES=0; [ "${1:-}" = "-y" ] && YES=1     # -y: yes to removing the app (settings are still kept)

DEST="${XDG_DATA_HOME:-$HOME/.local/share}/disc-ripper"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESK="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
{ [ -z "$DESK" ] || [ "$DESK" = "$HOME" ]; } && DESK="$HOME/Desktop"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/disc-ripper"

echo "This removes Disc Ripper from:"
echo "  $DEST"
echo "  $APPS/disc-ripper.desktop, $DESK/Disc Ripper.desktop, ~/.local/bin/disc-ripper"
echo "Your ripped videos are NOT touched. MakeMKV stays installed."
if [ $YES = 0 ]; then
  read -r -p "Continue? [y/N] " a </dev/tty || a=""
  [[ ${a:-N} =~ ^[Yy] ]] || { echo "Cancelled."; exit 0; }
fi

rm -rf -- "$DEST"
rm -f -- "$APPS/disc-ripper.desktop" "$DESK/Disc Ripper.desktop" "$HOME/.local/bin/disc-ripper"
echo "✓ Disc Ripper removed."

if [ -d "$CONF" ] && [ $YES = 0 ]; then
  read -r -p "Also remove your Disc Ripper settings ($CONF)? [y/N] " a </dev/tty || a=""
  [[ ${a:-N} =~ ^[Yy] ]] && rm -rf -- "$CONF" && echo "✓ settings removed"
fi
