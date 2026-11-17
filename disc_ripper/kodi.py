"""Kodi naming: give ripped TV episodes Kodi-friendly names after the fact, reversibly.

    Season 01/disc1/C1_t01.mkv  ->  Season 01/disc1/The 100 S01E01 - Pilot.mkv

Kodi finds episodes by the SxxEyy in the file name, even inside the disc1/disc2 sub-folders, so the folders stay as
they are (and Disc Ripper keeps working). Extras and duplicate titles go to disc*/_not-episodes/ with a .nomedia file
so Kodi ignores them. Nothing is deleted. Every rename is recorded in <show>/.disc-ripper-kodi.json for Undo.

Episode names/counts come from TVmaze (free, no key): https://www.tvmaze.com/api

How numbering works (deterministic - no guessing):
  episodes are numbered in disc order, then title-index order on each disc.
  Each disc's first episode number = sum of the episodes on the discs before it. Those per-disc counts come from
  show.disc_eps if set, otherwise from the discs themselves - but only when every disc in the season is done and the
  total matches the official count. So a damaged/short disc never shifts the numbering of later discs.
"""
from __future__ import annotations

import json
import re
import subprocess
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import core

TVMAZE = "https://api.tvmaze.com"
MAP_FILE = ".disc-ripper-kodi.json"
EP_CACHE = ".disc-ripper-episodes.json"
NOT_EPISODES = "_not-episodes"
_IDX_RE = re.compile(r"_t(\d+)\.mkv$")
_SXE_RE = re.compile(r"\bS\d{2}E\d{2}\b")


# ---------------------------------------------------------------- episode list (TVmaze)
def lookup(show: core.Show, refresh: bool = False) -> dict:
    """{'id','name','year','episodes': {season: {number: title}}} - cached in the show folder."""
    cache = show.folder / EP_CACHE
    if not refresh:
        try:
            d = json.loads(cache.read_text())
            d["episodes"] = {int(s): {int(n): t for n, t in e.items()} for s, e in d["episodes"].items()}
            return d
        except (OSError, ValueError, KeyError):
            pass
    q = urllib.parse.urlencode({"q": re.sub(r"\s*\(\d{4}\)$", "", show.name)})
    url = f"{TVMAZE}/shows/{show.tvmaze_id}?embed=episodes" if show.tvmaze_id else \
        f"{TVMAZE}/singlesearch/shows?{q}&embed=episodes"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "disc-ripper"}), timeout=20) as r:
            data = json.load(r)
    except OSError as e:
        raise RuntimeError(f"Couldn't reach TVmaze ({e}). Check the internet connection.") from e
    eps: dict[int, dict[int, str]] = {}
    for e in data.get("_embedded", {}).get("episodes", []):
        if e.get("season") and e.get("number"):
            eps.setdefault(int(e["season"]), {})[int(e["number"])] = e.get("name") or ""
    if not eps:
        raise RuntimeError(f"TVmaze has no episode list for “{show.name}”.")
    d = {"id": data["id"], "name": data["name"], "year": (data.get("premiered") or "")[:4], "episodes": eps}
    try:
        cache.write_text(json.dumps(d, indent=1))
    except OSError:
        pass
    return d


# ---------------------------------------------------------------- duplicate check (by picture, not size)
_SIGS: dict[str, bytes | None] = {}


def _frame_sig(f: Path, secs: float) -> bytes | None:
    """Three tiny grey frames from fixed points in the video. Same title ripped twice -> same pictures."""
    key = str(f)
    if key not in _SIGS:
        out = b""
        for t in (secs * 0.2, secs * 0.5, secs * 0.8):
            try:
                r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.1f}", "-i", str(f), "-frames:v", "1",
                                    "-vf", "scale=32:18,format=gray", "-f", "rawvideo", "-"],
                                   capture_output=True, timeout=60)
                out += r.stdout if len(r.stdout) == 32 * 18 else b""
            except (OSError, subprocess.SubprocessError):
                pass
        _SIGS[key] = out if len(out) == 3 * 32 * 18 else None
    return _SIGS[key]


def same_video(a: Path, a_secs: float, b: Path, b_secs: float) -> bool:
    """True only if the two files show the same pictures (a real duplicate), not just the same length."""
    sa, sb = _frame_sig(a, min(a_secs, b_secs)), _frame_sig(b, min(a_secs, b_secs))
    if not sa or not sb:
        return False                       # can't tell -> treat as different (never hide a real episode)
    diff = sum(abs(x - y) for x, y in zip(sa, sb)) / len(sa)
    return diff < 4.0


# ---------------------------------------------------------------- planning
@dataclass
class Action:
    src: Path
    dst: Path
    kind: str            # episode | extra | duplicate
    season: int
    episode: int = 0
    note: str = ""


def _safe(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|]+', " ", name)).strip().rstrip(".")


def _load_map(show: core.Show) -> list[dict]:
    try:
        return json.loads((show.folder / MAP_FILE).read_text())
    except (OSError, ValueError):
        return []


def plan(show: core.Show, info: dict, titles: bool = True) -> tuple[list[Action], dict[int, str]]:
    """Work out the renames. Returns (actions, note per season). Doesn't touch any file."""
    current_to_orig = {m["now"]: m["orig"] for m in _load_map(show)}
    cache_f = show.folder / ".disc-ripper-lengths.json"
    try:
        lens = json.loads(cache_f.read_text())
    except (OSError, ValueError):
        lens = {}
    show_title = _safe(info["name"])
    actions: list[Action] = []
    notes: dict[int, str] = {}
    for s, ndiscs in enumerate(show.discs, 1):
        official = info["episodes"].get(s, {})
        if not official:
            notes[s] = "not in the episode list"
            continue
        per_disc: list[list[tuple[int, Path]]] = []      # episode files per disc: (title index, path)
        extras: list[tuple[Path, str]] = []
        all_done = True
        unfinished: set[int] = set()
        for d in range(1, ndiscs + 1):
            ddir = show.disc_dir(s, d)
            if core.disc_state(ddir).state != "done":
                all_done = False
                unfinished.add(d)
            eps: list[tuple[int, Path, float, int]] = []
            for f in sorted(ddir.glob("*.mkv")):
                orig = current_to_orig.get(str(f), str(f))
                m = _IDX_RE.search(orig)
                idx = int(m.group(1)) if m else 999
                secs = core.file_secs(f, lens) or 0
                if not show.is_episode(secs):
                    extras.append((f, "extra (not episode length)"))
                    continue
                size = f.stat().st_size
                dup = next((p for i, p, sc, sz in eps
                            if abs(sc - secs) < 2.0 and abs(sz - size) <= size * 0.02 and same_video(p, sc, f, secs)), None)
                if dup:
                    extras.append((f, f"duplicate of {Path(current_to_orig.get(str(dup), str(dup))).name}"))
                    continue
                eps.append((idx, f, secs, size))
            per_disc.append([(i, p) for i, p, _s, _z in sorted(eps)])
        found = sum(len(x) for x in per_disc)
        layout = show.disc_eps.get(str(s)) if show.disc_eps else None
        if found == 0:
            notes[s] = "nothing ripped yet"
            continue
        if layout and (len(layout) != ndiscs or sum(layout) != len(official)):
            notes[s] = f"disc layout {layout} doesn't add up to {len(official)} episodes - fix it in the show settings"
            continue
        if not layout:
            if found == 0:
                notes[s] = "nothing ripped yet"
                continue
            if not all_done or found != len(official):
                notes[s] = (f"{found}/{len(official)} episodes - can't number safely until the season is complete, "
                            "or set the episodes per disc (Edit show…)")
                continue
            layout = [len(x) for x in per_disc]
        start, missing, bad = 1, [], ""
        for d, (files, n) in enumerate(zip(per_disc, layout), 1):
            if len(files) > n:
                bad = f"disc {d} has {len(files)} episode files but should have {n}"
                break
            for k, (_idx, f) in enumerate(files):
                e = start + k
                if d in unfinished:            # never rename on a disc that's still being ripped / unfinished
                    continue
                title = official.get(e, "")
                name = f"{show_title} S{s:02d}E{e:02d}" + (f" - {_safe(title)}" if titles and title else "")
                actions.append(Action(f, f.with_name(name + ".mkv"), "episode", s, e))
            missing += [f"E{e:02d}" for e in range(start + len(files), start + n)]
            start += n
        if bad:
            actions = [a for a in actions if a.season != s]
            notes[s] = bad
            continue
        for f, why in extras:
            if any(f.parent == show.disc_dir(s, d) for d in unfinished):
                continue
            actions.append(Action(f, f.parent / NOT_EPISODES / f.name, "duplicate" if why.startswith("dup") else "extra",
                                  s, note=why))
        later = f" - disc {', '.join(map(str, sorted(unfinished)))} unfinished, named later" if unfinished else ""
        notes[s] = "ready" + (f" - missing {', '.join(missing)}" if missing else "") + later
    return [a for a in actions if a.src != a.dst], notes


# ---------------------------------------------------------------- apply / undo
def apply(show: core.Show, actions: list[Action]) -> tuple[int, list[str]]:
    mapping = _load_map(show)
    done, errors = 0, []
    for a in actions:
        if a.dst.exists():
            errors.append(f"{a.dst.name} already exists - skipped {a.src.name}")
            continue
        try:
            a.dst.parent.mkdir(exist_ok=True)
            if a.dst.parent.name == NOT_EPISODES:
                (a.dst.parent / ".nomedia").touch()     # Kodi skips folders containing .nomedia
            a.src.rename(a.dst)
        except OSError as e:
            errors.append(f"{a.src.name}: {e}")
            continue
        prev = next((m for m in mapping if m["now"] == str(a.src)), None)
        if prev:
            prev["now"] = str(a.dst)
        else:
            mapping.append({"orig": str(a.src), "now": str(a.dst)})
        done += 1
    (show.folder / MAP_FILE).write_text(json.dumps(mapping, indent=1))
    return done, errors


def undo(show: core.Show) -> tuple[int, list[str]]:
    """Put every renamed file back to its original name."""
    mapping = _load_map(show)
    done, errors, keep = 0, [], []
    for m in reversed(mapping):
        now, orig = Path(m["now"]), Path(m["orig"])
        if not now.exists():
            errors.append(f"{now.name} is gone - can't restore {orig.name}")
            keep.append(m)
            continue
        if orig.exists():
            errors.append(f"{orig.name} already exists - left {now.name} as is")
            keep.append(m)
            continue
        now.rename(orig)
        done += 1
        try:
            left = list(now.parent.iterdir())
            if now.parent.name == NOT_EPISODES and all(p.name == ".nomedia" for p in left):
                for p in left:
                    p.unlink()
                now.parent.rmdir()
        except OSError:
            pass
    f = show.folder / MAP_FILE
    if keep:
        f.write_text(json.dumps(list(reversed(keep)), indent=1))
    else:
        f.unlink(missing_ok=True)
    return done, errors


def has_renames(show: core.Show) -> bool:
    return bool(_load_map(show))
