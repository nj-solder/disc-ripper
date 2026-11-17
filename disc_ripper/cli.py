"""Disc Ripper command-line mode - same engine as the window, no window needed (handy for scripts and AI agents).

    disc-ripper shows                         list shows and movies
    disc-ripper status "The Blacklist"        discs + episodes, done / missing
    disc-ripper drives                        disc drives and what's in them
    disc-ripper rip "The Blacklist" 6 1       rip Season 6 Disc 1 (a person inserts the disc)
    disc-ripper movie "John Wick" --year 2014 rip the movie in the drive
    disc-ripper kodi-names "The 100"          preview Kodi names (add --apply to rename, --undo to restore)
    disc-ripper new-show "Lost" --discs "7 7 7 5 5 5" --lookup
    disc-ripper eject | key | update-makemkv

Add --json to any command for machine-readable output. Exit codes: 0 ok, 1 failed, 2 usage error, 130 stopped (Ctrl+C).
Run with no command to open the window.
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time

from . import core, kodi

COMMANDS = {"shows", "status", "drives", "rip", "movie", "kodi-names", "new-show", "eject", "key", "update-makemkv"}

OK, FAILED, USAGE = 0, 1, 2


def out(args, data, text: str) -> None:
    print(json.dumps(data, indent=2, default=str) if args.json else text)


def die(args, msg: str, code: int = FAILED) -> int:
    if args.json:
        print(json.dumps({"ok": False, "error": msg}))
    else:
        print(f"Error: {msg}", file=sys.stderr)
    return code


def settings_ok(args, s: dict) -> str | None:
    if not core.is_mounted(s["drive_mnt"]):
        return f"the hard drive is not mounted at {s['drive_mnt']} (Settings → 'Require this drive mounted')"
    return None


def get_show(args, s: dict, name: str):
    sh = core.load_show(s["rips_root"], name)
    if not sh:
        names = core.list_shows(s["rips_root"])
        return None, f"no show called '{name}' in {s['rips_root']}" + (f" (shows: {', '.join(names)})" if names else "")
    return sh, None


def pick_drive(args, s: dict) -> tuple[str | None, str | None]:
    drives = core.list_drives(s["nicknames"])
    if args.drive:
        if not any(d.dev == args.drive for d in drives):
            return None, f"{args.drive} is not a disc drive (drives: {', '.join(d.dev for d in drives) or 'none'})"
        return args.drive, None
    if not drives:
        return None, "no disc drive found"
    if len(drives) > 1 and not s.get("last_drive"):
        return None, f"more than one drive - pick one with --drive ({', '.join(d.dev for d in drives)})"
    last = s.get("last_drive")
    return (last if any(d.dev == last for d in drives) else drives[0].dev), None


# ---------------------------------------------------------------- read-only commands
def cmd_shows(args, s):
    shows = core.list_shows(s["rips_root"])
    movies = core.list_movies(s["rips_root"])
    data = {"rips_root": s["rips_root"], "shows": shows, "movies": [{"name": n, "state": st.state} for n, st in movies]}
    lines = [f"Rips folder: {s['rips_root']}", "", "Shows:"] + [f"  {n}" for n in shows or ["(none)"]]
    lines += ["", "Movies:"] + [f"  {n}  [{st.state}]" for n, st in movies] if movies else ["", "Movies: (none)"]
    out(args, data, "\n".join(lines))
    return OK


def cmd_status(args, s):
    sh, err = get_show(args, s, args.show)
    if err:
        return die(args, err)
    counts = core.count_episodes(sh)
    seasons = []
    for si, n in enumerate(sh.discs, 1):
        discs = []
        for d in range(1, n + 1):
            st = core.disc_state(sh.disc_dir(si, d))
            discs.append({"disc": d, "state": st.state, "saved_titles": st.saved, "files": st.mkv, "reason": st.reason})
        seasons.append({"season": si, "episodes": counts.get(si, 0), "expected": sh.expected(si), "discs": discs})
    nxt = next(({"season": x["season"], "disc": dd["disc"]} for x in seasons for dd in x["discs"]
                if dd["state"] != "done"), None)
    data = {"show": sh.name, "seasons": seasons, "next_disc": nxt,
            "episodes": sum(counts.values()), "expected": sum(sh.episodes) if sh.episodes else None}
    sym = {"done": "✓", "partial": "~", "failed": "✗", "new": "·"}
    lines = [f"{sh.name}    ({'·' } not started  ~ partly done  ✓ done  ✗ failed)"]
    for x in seasons:
        eps = f"{x['episodes']}/{x['expected']}" if x["expected"] else f"{x['episodes']}"
        lines.append(f"  Season {x['season']:>2}  " + " ".join(f"D{dd['disc']}{sym[dd['state']]}" for dd in x["discs"])
                     + f"   {eps} episodes")
    lines.append(f"  Total: {data['episodes']}" + (f"/{data['expected']}" if data["expected"] else "") + " episodes")
    if nxt:
        lines.append(f"  Next disc: Season {nxt['season']} Disc {nxt['disc']}")
    out(args, data, "\n".join(lines))
    return OK


def cmd_drives(args, s):
    rows = []
    for d in core.list_drives(s["nicknames"]):
        media, label = core.drive_media(d.dev)
        ls, ld = core.parse_label(label)
        rows.append({"dev": d.dev, "name": d.nickname or f"{d.vendor} {d.model}".strip(), "disc": media, "label": label, "season": ls, "disc_no": ld,
                     "busy": bool(core.mkv_pids(d.dev))})
    text = "\n".join(f"{r['dev']}  {r['name']}  -  " + (f"disc: {r['label'] or '(no label)'}" if r["disc"] else "empty")
                     + ("  [MakeMKV busy]" if r["busy"] else "") for r in rows) or "No disc drives found."
    out(args, rows, text)
    return OK


# ---------------------------------------------------------------- ripping
def _run_job(args, job) -> int:
    """Run a RipJob in this thread, printing progress. Ctrl+C stops MakeMKV properly."""
    tty = sys.stdout.isatty() and not args.json
    events = []
    last_print = [0.0]

    def log(msg, level):
        events.append({"time": time.strftime("%H:%M:%S"), "level": level, "msg": msg})
        if not args.json:
            print(("\r\033[K" if tty else "") + f"{time.strftime('%H:%M:%S')} {msg}", flush=True)

    def status(st):
        if args.json:
            return
        line = f"{st['what']} - {st['phase']} {st['pct']}%"
        if st["bytes"]:
            line += f" - {st['bytes'] / 1048576:,.0f} MB {st['rate'] / 1048576:.1f} MB/s"
        if st["idle"] >= 60:
            line += f" - no progress {st['idle'] // 60}:{st['idle'] % 60:02d} (gives up at {st['stall'] // 60} min)"
        if tty:
            print("\r\033[K" + line, end="", flush=True)
        elif time.time() - last_print[0] >= 30:
            print(f"{time.strftime('%H:%M:%S')} {line}", flush=True)
            last_print[0] = time.time()

    job.log.connect(log)
    job.status.connect(status)
    job.title_state.connect(lambda idx, st: events.append({"title": idx, "state": st}))
    stopping = [False]

    def on_int(*_):
        if not stopping[0]:
            stopping[0] = True
            print("\nStopping MakeMKV…", file=sys.stderr)
            job.stop()
    old = signal.signal(signal.SIGINT, on_int)
    try:
        ok, msg = job._run()
    finally:
        signal.signal(signal.SIGINT, old)
    if tty:
        print("\r\033[K", end="")
    hint = core.needs_update_hint(msg) if not ok else ""
    result = {"ok": ok, "message": msg, "hint": hint, "saved": {str(k): v.name for k, v in job.saved.items()},
              "events": events}
    stopped = msg == "Stopped"
    if args.json:
        print(json.dumps(dict(result, stopped=stopped), indent=2, default=str))
    elif stopped:
        print("■ Stopped. The disc is marked unfinished - run the same command again to continue.")
    else:
        print(("✓ " if ok else "✗ FAILED: ") + msg + (f"\n  {hint}" if hint else ""))
    return OK if ok else (130 if stopped else FAILED)


def _ready(args, s) -> tuple[str | None, str | None]:
    err = settings_ok(args, s)
    if err:
        return None, err
    if not core.makemkvcon_cmd():
        return None, "MakeMKV is not installed"
    dev, err = pick_drive(args, s)
    if err:
        return None, err
    if not core.drive_media(dev)[0]:
        return None, f"no disc in {dev} - insert it and try again"
    if core.mkv_pids(dev):
        return None, f"MakeMKV is already using {dev}"
    return dev, None


def cmd_rip(args, s):
    from .worker import RipJob, choose_episodes
    sh, err = get_show(args, s, args.show)
    if err:
        return die(args, err)
    if not (1 <= args.season <= len(sh.discs)) or not (1 <= args.disc <= sh.discs[args.season - 1]):
        return die(args, f"{sh.name} has no Season {args.season} Disc {args.disc}", USAGE)
    dev, err = _ready(args, s)
    if err:
        return die(args, err)
    label = core.drive_media(dev)[1]
    ls, ld = core.parse_label(label)
    if ((ls and ls != args.season) or (ld and ld != args.disc)) and not args.force:
        return die(args, f"the disc in {dev} is labelled {label} (Season {ls} Disc {ld}), not Season {args.season} "
                         f"Disc {args.disc}. Insert the right disc, or add --force to rip it anyway.")
    if core.disc_state(sh.disc_dir(args.season, args.disc)).state == "done" and not args.json:
        print(f"Season {args.season} Disc {args.disc} is already done - checking for anything missing.")
    job = RipJob(dev, sh.disc_dir(args.season, args.disc), f"S{args.season:02d} D{args.disc}", s,
                 sh.min_min * 60, choose_episodes(sh.min_min, sh.max_min))
    rc = _run_job(args, job)
    if rc == OK and s.get("auto_eject") and not args.no_eject:
        core.subprocess.run(["eject", dev], capture_output=True)
    return rc


def cmd_movie(args, s):
    from .worker import RipJob, choose_movie, movie_finisher
    year = args.year or ""
    if year and not (year.isdigit() and len(year) == 4):
        return die(args, "--year must be 4 digits, e.g. 2014", USAGE)
    dev, err = _ready(args, s)
    if err:
        return die(args, err)
    name = core.movie_folder_name(args.title, year)
    folder = core.movies_root(s["rips_root"]) / name
    if core.disc_state(folder).state == "done" and not args.force:
        return die(args, f"'{name}' is already ripped - add --force to rip it again")
    extras = args.extras is not None
    minlen = min(args.extras * 60, 600) if extras else 600
    job = RipJob(dev, folder, name, s, minlen, choose_movie(extras, args.extras or 5), movie_finisher(name))
    rc = _run_job(args, job)
    if rc == OK and s.get("auto_eject") and not args.no_eject:
        core.subprocess.run(["eject", dev], capture_output=True)
    return rc


# ---------------------------------------------------------------- Kodi names, shows, maintenance
def cmd_kodi(args, s):
    sh, err = get_show(args, s, args.show)
    if err:
        return die(args, err)
    if args.undo:
        n, errs = kodi.undo(sh)
        out(args, {"ok": not errs, "restored": n, "problems": errs},
            f"Restored {n} file(s)." + "".join(f"\n  ! {e}" for e in errs))
        return OK if not errs else FAILED
    try:
        info = kodi.lookup(sh, refresh=args.refresh)
    except RuntimeError as e:
        return die(args, str(e))
    acts, notes = kodi.plan(sh, info, titles=not args.no_titles)
    plan = [{"season": a.season, "kind": a.kind, "from": str(a.src), "to": str(a.dst), "note": a.note} for a in acts]
    if not args.apply:
        lines = [f"{info['name']} ({info['year']}) - {len(acts)} change(s) planned. Nothing renamed yet "
                 "(add --apply to rename, --undo to restore)."]
        lines += [f"  Season {k}: {v}" for k, v in notes.items()]
        lines += [""] + [f"  {a.src.parent.name}/{a.src.name}  ->  "
                         + (a.dst.name if a.kind == 'episode' else f"{kodi.NOT_EPISODES}/  ({a.note})") for a in acts]
        out(args, {"show": info["name"], "applied": False, "seasons": notes, "changes": plan}, "\n".join(lines))
        return OK
    n, errs = kodi.apply(sh, acts)
    out(args, {"show": info["name"], "applied": True, "renamed": n, "problems": errs, "seasons": notes},
        f"Renamed {n} file(s)." + "".join(f"\n  ! {e}" for e in errs))
    return OK if not errs else FAILED


def cmd_new_show(args, s):
    err = settings_ok(args, s)
    if err:
        return die(args, err)
    if core.load_show(s["rips_root"], args.name) and not args.force:
        return die(args, f"'{args.name}' already exists - add --force to overwrite its settings")
    try:
        discs = [int(x) for x in args.discs.replace(",", " ").split()]
        eps = [int(x) for x in args.episodes.replace(",", " ").split()] if args.episodes else []
    except ValueError:
        return die(args, "--discs/--episodes must be numbers, e.g. --discs \"5 5 5 4\"", USAGE)
    sh = core.Show(args.name, discs, args.min, args.max, eps, args.ep_len, root=s["rips_root"])
    if args.lookup:
        try:
            info = kodi.lookup(sh, refresh=True)
            sh.episodes = [len(info["episodes"][k]) for k in sorted(info["episodes"])]
            sh.tvmaze_id = info["id"]
        except RuntimeError as e:
            return die(args, str(e))
    if sh.episodes and len(sh.episodes) != len(sh.discs):
        return die(args, f"{len(sh.discs)} seasons of discs but {len(sh.episodes)} seasons of episodes - check --discs")
    sh.save()
    out(args, {"ok": True, "show": sh.name, "discs": sh.discs, "episodes": sh.episodes},
        f"Created '{sh.name}': discs {' '.join(map(str, sh.discs))}" +
        (f", episodes {' '.join(map(str, sh.episodes))}" if sh.episodes else ""))
    return OK


def cmd_eject(args, s):
    dev, err = pick_drive(args, s)
    if err:
        return die(args, err)
    r = core.subprocess.run(["eject", dev], capture_output=True, text=True)
    out(args, {"ok": r.returncode == 0, "dev": dev}, f"Ejected {dev}" if r.returncode == 0 else f"Eject failed: {r.stderr.strip()}")
    return OK if r.returncode == 0 else FAILED


def cmd_key(args, s):
    have = core.current_mkv_key()
    try:
        key, valid = core.fetch_beta_key()
    except RuntimeError as e:
        return die(args, str(e))
    current = key == have
    if args.install and not current:
        core.install_mkv_key(key)
    s["key_valid"] = valid
    core.save_settings(s)
    out(args, {"key_set": bool(have), "latest_installed": current or args.install, "valid_until": valid},
        ("Your MakeMKV key is the latest" if current else
         "Installed the latest MakeMKV key" if args.install else
         "A newer MakeMKV key is available - run: disc-ripper key --install") + (f" (valid until {valid})." if valid else "."))
    return OK


def cmd_update(args, s):
    if not core.mkv_is_flatpak():
        return die(args, "MakeMKV isn't the Flatpak - update it by hand")
    if core.subprocess.run(["pgrep", "-x", "makemkvcon"], capture_output=True).returncode == 0:
        return die(args, "MakeMKV is running - try again when it's finished")
    if not core.mkv_update_available():
        out(args, {"ok": True, "updated": False, "version": core.mkv_version()}, f"MakeMKV is up to date ({core.mkv_version()}).")
        return OK
    ok, msg = core.update_mkv()
    out(args, {"ok": ok, "updated": ok, "version": core.mkv_version(), "message": msg},
        f"MakeMKV updated to {core.mkv_version()}." if ok else f"Update failed: {msg}")
    return OK if ok else FAILED


# ---------------------------------------------------------------- entry
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="disc-ripper", description="Disc Ripper command-line mode. Run with no command "
                                "to open the window.", formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="Exit codes: 0 ok, 1 failed, 2 usage error, 130 stopped (Ctrl+C).  --json works on every command.")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    def add(name, help_, **kw):
        sp = sub.add_parser(name, help=help_, **kw)
        sp.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable output")
        return sp

    add("shows", "list shows and movies")
    sp = add("status", "a show's discs and episodes: done, missing, next disc"); sp.add_argument("show")
    add("drives", "disc drives and the disc in each")
    sp = add("rip", "rip one TV disc (insert it first)")
    sp.add_argument("show"); sp.add_argument("season", type=int); sp.add_argument("disc", type=int)
    sp.add_argument("--drive", help="e.g. /dev/sr0 (default: the only / last-used drive)")
    sp.add_argument("--force", action="store_true", help="rip even if the disc label says it's a different disc")
    sp.add_argument("--no-eject", action="store_true", help="don't eject when finished")
    sp = add("movie", "rip the movie in the drive (longest title)")
    sp.add_argument("title"); sp.add_argument("--year", default="")
    sp.add_argument("--extras", type=int, metavar="MIN", help="also rip extras longer than MIN minutes")
    sp.add_argument("--drive"); sp.add_argument("--force", action="store_true", help="rip again even if already done")
    sp.add_argument("--no-eject", action="store_true")
    sp = add("kodi-names", "preview / apply / undo Kodi episode names")
    sp.add_argument("show"); sp.add_argument("--apply", action="store_true", help="actually rename")
    sp.add_argument("--undo", action="store_true", help="restore every original name")
    sp.add_argument("--no-titles", action="store_true", help="names without episode titles")
    sp.add_argument("--refresh", action="store_true", help="re-download the episode list")
    sp = add("new-show", "create a show")
    sp.add_argument("name"); sp.add_argument("--discs", required=True, help='discs per season, e.g. "5 5 5 4"')
    sp.add_argument("--episodes", help='episodes per season, e.g. "22 22 23 22"')
    sp.add_argument("--lookup", action="store_true", help="fill episodes per season from TVmaze")
    sp.add_argument("--ep-len", type=int, default=0, help="typical episode length in minutes")
    sp.add_argument("--min", type=int, default=10, help="shortest title to rip (min)")
    sp.add_argument("--max", type=int, default=60, help="longest title to rip (min)")
    sp.add_argument("--force", action="store_true")
    sp = add("eject", "eject the disc"); sp.add_argument("--drive")
    sp = add("key", "check the MakeMKV beta key"); sp.add_argument("--install", action="store_true", help="install the latest key")
    add("update-makemkv", "update the MakeMKV Flatpak now")
    return p


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    args.json = getattr(args, "json", False)
    for a in ("drive",):
        if not hasattr(args, a):
            setattr(args, a, None)
    from PySide6.QtCore import QCoreApplication     # RipJob uses Qt signals; no window is created
    _app = QCoreApplication.instance() or QCoreApplication(["disc-ripper"])
    s = core.load_settings()
    handlers = {"shows": cmd_shows, "status": cmd_status, "drives": cmd_drives, "rip": cmd_rip, "movie": cmd_movie,
                "kodi-names": cmd_kodi, "new-show": cmd_new_show, "eject": cmd_eject, "key": cmd_key,
                "update-makemkv": cmd_update}
    try:
        return handlers[args.cmd](args, s)
    except KeyboardInterrupt:
        return die(args, "interrupted")
    except BrokenPipeError:
        return OK
