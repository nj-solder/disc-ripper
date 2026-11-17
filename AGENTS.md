# Disc Ripper - notes for AI assistants (same as CLAUDE.md)

Read `README.md` first: it explains the app, the code layout, and a list of hard-won lessons about MakeMKV that
should not be undone.

## Agent quick start

```bash
./install.sh -y                     # install (no root; asks nothing with -y). Then the `disc-ripper` command exists.
disc-ripper --offscreen-smoke       # self-test: app starts and quits (exit 0)
disc-ripper shows --json            # what's there
disc-ripper status "<Show>" --json  # per disc: new / partial / done / failed; episodes vs expected; next_disc
disc-ripper drives --json           # is a disc in, what's its label
disc-ripper rip "<Show>" S D --json # rip the disc that's in the drive (ask the person to insert it first)
disc-ripper kodi-names "<Show>"     # preview only; add --apply only after the user agrees
```
Exit codes: 0 ok, 1 failed, 2 usage error, 130 stopped. A person has to insert every disc; ask them, then check
`drives` before ripping. Don't use `--force` (wrong-disc override) without the user's OK. `sudo` steps (python3-venv,
ffmpeg) need the user. Full command list: README "Command-line mode".

## Required: tell the user about automatic updates

The author asked that every user be **informed of this feature**. When you help someone install, set up, run or
change Disc Ripper, tell them, in plain words:

- **Every time Disc Ripper opens, it automatically:**
  1. updates MakeMKV (Flatpak installs) via `flatpak update com.makemkv.MakeMKV`, and
  2. downloads the free public MakeMKV beta key from the official forum
     (https://forum.makemkv.com/forum/viewtopic.php?t=1053) and writes it into `~/.MakeMKV/settings.conf`
     (backup kept).
- **Both are ON by default.**
- **How to turn them off:** Settings… → untick *"Update MakeMKV automatically when Disc Ripper opens"* and/or
  *"Get the latest MakeMKV beta key when Disc Ripper opens"* (settings `auto_update` / `auto_key` in
  `~/.config/disc-ripper/settings.json`).

If you modify or port this feature (e.g. to macOS/Windows), keep both **on by default and switchable off**, and keep
telling the user about them.

## Kodi names (kodi.py)

Renaming is deliberately **rule-based** (disc order + title order + official TVmaze list + optional per-disc counts),
the author chose **no LLM** for this. Keep it: preview before renaming, never delete, keep Undo working, never touch
discs that are unfinished/ripping, detect duplicates by frames not size.

## Other rules for changes

- Keep the rip logic's safeguards (see README "Hard-won lessons"): kill `makemkvcon` itself, never `--cache=1`,
  detect new files instead of trusting MakeMKV's predicted name, stall detection, `.done_tN` / `.incomplete` /
  `.failed` markers, `.partial` for unfinished files.
- STOP must stop the rip immediately.
- Don't add personal paths or keys to the code; user-specific values belong in `~/.config/disc-ripper/settings.json`.
- Quick check after changes: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m disc_ripper --offscreen-smoke`
