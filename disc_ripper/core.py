"""Disc Ripper engine: settings, shows, disc folders, drives and MakeMKV helpers.

No Qt in here, so it can be tested on its own.

Folder layout (same as ~/ripshow.sh, so existing rips carry over):
    <rips_root>/<Show>/Season 01/disc1/*.mkv
    .done_tN     title index N saved
    .incomplete  disc started but not finished
    .failed      disc failed twice (text = reason)          (new in the app)
    <rips_root>/<Show>/.disc-ripper.json   show settings   (new in the app)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "disc-ripper"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
SHOW_FILE = ".disc-ripper.json"

DEFAULT_SETTINGS = {
    "drive_mnt": "",                                     # optional: external drive that must be mounted first
    "rips_root": str(Path.home() / "Videos" / "Rips"),   # shows and Movies/ live here
    "stall_secs": 1200,                                  # no progress this long = stalled
    "auto_eject": True,                                  # eject after a disc finishes
    "nicknames": {},                                     # drive model -> friendly name, e.g. {"BD-MLT UJ272": "GOOBOX"}
    "auto_update": True,                                 # update the MakeMKV Flatpak when the app opens
    "update_checked": "",
    "auto_key": True,                                    # fetch the public MakeMKV beta key when the app opens
    "key_valid": "",                                     # e.g. "end of November 2026" (from the forum post)
    "key_checked": "",                                   # date of the last key check (YYYY-MM-DD)
    "last_show": "",
    "last_drive": "",
}


# ---------------------------------------------------------------- settings
def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    try:
        s.update(json.loads(SETTINGS_FILE.read_text()))
    except (OSError, ValueError):
        pass
    return s


def save_settings(s: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=2))
    tmp.replace(SETTINGS_FILE)


def is_mounted(path: str) -> bool:
    """True if the required drive is mounted. No drive set (empty) = nothing to check."""
    return os.path.ismount(path) if path else True


# ---------------------------------------------------------------- shows
@dataclass
class Show:
    name: str
    discs: list[int]                 # discs per season, S1 first
    min_min: int = 10                # shortest title to rip (minutes)
    max_min: int = 60                # longest title to rip (minutes) - skips "Play All"
    episodes: list[int] = field(default_factory=list)   # expected episodes per season (optional)
    ep_len: int = 0                  # typical episode length in minutes (0 = unknown) - for counting episodes
    disc_eps: dict = field(default_factory=dict)        # {"5": [5, 4, 5, 5, 3]} episodes per disc (optional)
    tvmaze_id: int = 0               # TVmaze show id (episode names/counts), 0 = look up by name
    root: str = field(default="", repr=False)

    def is_episode(self, secs: float) -> bool:
        """Does a ripped file of this length count as an episode (not an extra)?"""
        if self.ep_len:
            return self.ep_len * 60 * 0.8 <= secs <= self.ep_len * 60 * 1.2
        return self.min_min * 60 <= secs <= self.max_min * 60

    @property
    def folder(self) -> Path:
        return Path(self.root) / self.name

    def expected(self, season: int) -> int:
        return self.episodes[season - 1] if 0 < season <= len(self.episodes) else 0

    def disc_dir(self, season: int, disc: int) -> Path:
        return self.folder / f"Season {season:02d}" / f"disc{disc}"

    def save(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        data = {k: v for k, v in asdict(self).items() if k != "root"}
        (self.folder / SHOW_FILE).write_text(json.dumps(data, indent=2))


MOVIES_DIR = "Movies"


def list_shows(root: str) -> list[str]:
    try:
        return sorted(p.name for p in Path(root).iterdir()
                      if p.is_dir() and not p.name.startswith(".") and p.name != MOVIES_DIR)
    except OSError:
        return []


# ---------------------------------------------------------------- movies
def movies_root(root: str) -> Path:
    return Path(root) / MOVIES_DIR


def movie_folder_name(title: str, year: str) -> str:
    """Plex/Jellyfin style: 'John Wick (2014)'. Strips characters that are bad in file names."""
    clean = re.sub(r'[\\/:*?"<>|]+', " ", title).strip()
    clean = re.sub(r"\s+", " ", clean)
    return f"{clean} ({year})" if year else clean


def list_movies(root: str) -> list[tuple[str, DiscState]]:
    base = movies_root(root)
    try:
        dirs = sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        return []
    return [(p.name, disc_state(p)) for p in dirs]


def title_from_label(label: str) -> str:
    """Disc label -> a guess at the movie name: 'JOHN_WICK_WS' -> 'John Wick'."""
    s = re.sub(r"[_\.]+", " ", label or "").strip()
    s = re.sub(r"\b(WS|FS|16X9|4X3|DVD|BLURAY|BD|DISC ?\d+|D\d+|SE|NTSC|PAL)\b", "", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip()
    return s.title() if s.isupper() or s.islower() else s


# ---------------------------------------------------------------- episode counting (ffprobe, cached)
def file_secs(f: Path, cache: dict) -> float | None:
    """Length of a video file in seconds; cached by path+size+mtime so ffprobe runs once per file."""
    try:
        st = f.stat()
    except OSError:
        return None
    key = f"{f}|{st.st_size}|{int(st.st_mtime)}"
    if key in cache:
        return cache[key]
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(f)],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        secs = float(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    cache[key] = secs
    return secs


def count_episodes(show: Show) -> dict[int, int]:
    """Episodes found on disk per season (by file length). Cache kept in the show folder."""
    cache_f = show.folder / ".disc-ripper-lengths.json"
    try:
        cache = json.loads(cache_f.read_text())
    except (OSError, ValueError):
        cache = {}
    counts: dict[int, int] = {}
    for si, n in enumerate(show.discs, 1):
        c = 0
        for d in range(1, n + 1):
            for f in sorted(show.disc_dir(si, d).glob("*.mkv")):
                secs = file_secs(f, cache)
                if secs and show.is_episode(secs):
                    c += 1
        counts[si] = c
    try:
        cache_f.write_text(json.dumps(cache))
    except OSError:
        pass
    return counts


def load_show(root: str, name: str) -> Show | None:
    """Load a show's settings; for folders made by ripshow.sh (no settings file) guess them."""
    folder = Path(root) / name
    try:
        d = json.loads((folder / SHOW_FILE).read_text())
        return Show(name=d.get("name", name), discs=[int(x) for x in d["discs"]],
                    min_min=int(d.get("min_min", 10)), max_min=int(d.get("max_min", 60)),
                    episodes=[int(x) for x in d.get("episodes", [])], ep_len=int(d.get("ep_len", 0)),
                    disc_eps={str(k): [int(x) for x in v] for k, v in d.get("disc_eps", {}).items()},
                    tvmaze_id=int(d.get("tvmaze_id", 0)), root=root)
    except (OSError, ValueError, KeyError):
        pass
    # guess from existing Season NN/discN folders
    seasons: dict[int, int] = {}
    for sd in folder.glob("Season *"):
        m = re.match(r"Season (\d+)$", sd.name)
        if not m:
            continue
        nums = [int(x.name[4:]) for x in sd.glob("disc*") if x.name[4:].isdigit()]
        seasons[int(m.group(1))] = max(nums, default=0)
    if not seasons:
        return None
    return Show(name=name, discs=[seasons.get(i, 0) for i in range(1, max(seasons) + 1)], root=root)


# ---------------------------------------------------------------- disc folder state
@dataclass
class DiscState:
    state: str            # new | partial | done | failed
    saved: int = 0        # titles with .done markers
    mkv: int = 0          # .mkv files present
    reason: str = ""


def disc_state(d: Path) -> DiscState:
    try:
        names = os.listdir(d)
    except OSError:
        return DiscState("new")
    saved = sum(1 for n in names if n.startswith(".done_t"))
    mkv = sum(1 for n in names if n.endswith(".mkv"))
    if ".failed" in names:
        try:
            reason = (d / ".failed").read_text().strip()
        except OSError:
            reason = ""
        return DiscState("failed", saved, mkv, reason)
    if ".incomplete" in names:
        return DiscState("partial" if (saved or mkv) else "new", saved, mkv)
    if mkv:
        return DiscState("done", saved, mkv)
    return DiscState("new", saved, mkv)


def keep_partial(f: Path) -> Path | None:
    """Rename an unfinished file to *.partial (never overwrites an older one)."""
    if not f.is_file():
        return None
    p = f.with_name(f.name + ".partial")
    if p.exists():
        p = f.with_name(f"{f.name}.partial.{int(time.time())}")
    f.rename(p)
    return p


# ---------------------------------------------------------------- drives
@dataclass
class Drive:
    dev: str
    model: str
    vendor: str
    nickname: str

    @property
    def label(self) -> str:
        return f"{self.nickname} ({self.dev})" if self.nickname else f"{self.vendor} {self.model} ({self.dev})".strip()


def list_drives(nicknames: dict) -> list[Drive]:
    try:
        out = subprocess.run(["lsblk", "-J", "-d", "-o", "NAME,TYPE,MODEL,VENDOR"],
                             capture_output=True, text=True, timeout=5).stdout
        devs = json.loads(out).get("blockdevices", [])
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    drives = []
    for b in devs:
        if b.get("type") != "rom":
            continue
        model = (b.get("model") or "").strip()
        drives.append(Drive(f"/dev/{b['name']}", model, (b.get("vendor") or "").strip(), nicknames.get(model, "")))
    return drives


def drive_media(dev: str) -> tuple[bool, str]:
    """(disc present, disc label) from udev - never touches the disc, so it can't hang on a bad one."""
    try:
        out = subprocess.run(["udevadm", "info", "-q", "property", "-n", dev],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False, ""
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    media = props.get("ID_CDROM_MEDIA") == "1"
    if not media:
        # udev's own probe can fail on a scratched disc and leave nothing; the kernel's disc size is still right
        try:
            media = int(Path(f"/sys/block/{Path(dev).name}/size").read_text()) > 0
        except (OSError, ValueError):
            pass
    return media, props.get("ID_FS_LABEL", "")


def parse_label(label: str) -> tuple[int | None, int | None]:
    """Season/disc from a disc label: THE_BLACKLIST_S5_D3, THE_100_SEASON_1_DISC_3, ...SEASON3_DISC5."""
    u = label.upper()
    s = re.search(r"(?:^|_)S(?:EASON)?_?(\d{1,2})(?=_|$)", u)
    d = re.search(r"(?:^|_|\d)D(?:IS[CK])?_?(\d{1,2})$", u)
    return (int(s.group(1)) if s else None, int(d.group(1)) if d else None)


# ---------------------------------------------------------------- MakeMKV
def makemkvcon_cmd() -> list[str]:
    """Flatpak MakeMKV if installed, else a native makemkvcon."""
    if shutil.which("flatpak"):
        r = subprocess.run(["flatpak", "info", "com.makemkv.MakeMKV"], capture_output=True)
        if r.returncode == 0:
            return ["flatpak", "run", "--command=makemkvcon", "com.makemkv.MakeMKV"]
    if shutil.which("makemkvcon"):
        return ["makemkvcon"]
    return []


def mkv_pids(dev: str) -> list[int]:
    """makemkvcon processes working on THIS drive only (matched by their dev:/dev/srN argument)."""
    pat = rf"^makemkvcon .*dev:{re.escape(dev)}( |$)"
    r = subprocess.run(["pgrep", "-f", "--", pat], capture_output=True, text=True)
    return [int(p) for p in r.stdout.split()]


def kill_mkv(dev: str, proc: subprocess.Popen | None = None) -> None:
    """Stop this drive's MakeMKV. Killing only the flatpak wrapper leaves makemkvcon running."""
    if proc and proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            pass
    for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 2)):
        pids = mkv_pids(dev)
        if not pids:
            break
        for p in pids:
            try:
                os.kill(p, sig)
            except OSError:
                pass
        end = time.time() + wait
        while time.time() < end and mkv_pids(dev):
            time.sleep(0.25)
    if proc:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass


def parse_titles(info: str) -> list[dict]:
    """TINFO lines -> [{idx, secs, length, name}] sorted by index."""
    titles: dict[int, dict] = {}
    for line in info.splitlines():
        if not line.startswith("TINFO:"):
            continue
        parts = line[6:].split(",", 3)
        if len(parts) < 4:
            continue
        idx, code, val = int(parts[0]), int(parts[1]), parts[3].strip().strip('"')
        t = titles.setdefault(idx, {"idx": idx, "secs": 0, "length": "", "name": ""})
        if code == 9:
            t["length"] = val
            secs = 0
            for x in val.split(":"):
                secs = secs * 60 + int(x or 0)
            t["secs"] = secs
        elif code == 27:
            t["name"] = val
    return [titles[i] for i in sorted(titles)]


def progress(text: str) -> tuple[str, int]:
    """(current phase, overall % of this MakeMKV run) from its -r --progress output."""
    phase, pct = "", 0
    for line in reversed(text.splitlines()):
        if not phase and line.startswith("PRGC:"):
            phase = line.split('"')[1] if '"' in line else ""
        elif not pct and line.startswith("PRGV:"):
            try:
                _cur, tot, mx = (int(x) for x in line[5:].split(","))
                pct = tot * 100 // mx if mx else 0
            except ValueError:
                pass
        if phase and pct:
            break
    return phase, pct


_NOISE = re.compile(r"minimum title length|looks empty|is identical to stream|was added \(")


def last_msg(text: str) -> str:
    """MakeMKV's last real message (skips harmless notes); prefers an error if there was one."""
    msgs = [l.split('"')[1] for l in text.splitlines() if l.startswith("MSG:") and '"' in l]
    msgs = [m for m in msgs if not _NOISE.search(m)]
    errs = [m for m in msgs if "error" in m.lower() or "fail" in m.lower()]
    return (errs or msgs or [""])[-1]


def saved_ok(text: str) -> bool:
    """MakeMKV reported 'Copy complete. 1 titles saved.'"""
    return any(re.match(r'^MSG:5036,.*,"1"$', l) for l in text.splitlines())


def notify(title: str, body: str, critical: bool = False) -> None:
    if shutil.which("notify-send"):
        subprocess.Popen(["notify-send", "-a", "Disc Ripper", "-u", "critical" if critical else "normal", title, body],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------- MakeMKV beta key
MKV_KEY_URL = "https://forum.makemkv.com/forum/viewtopic.php?t=1053"   # official "MakeMKV is free while in beta" post
MKV_SETTINGS = Path.home() / ".MakeMKV" / "settings.conf"            # used by both the Flatpak and native MakeMKV
_KEY_RE = re.compile(r"\bT-[A-Za-z0-9@_]{40,}")
_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
           "november", "december"]


def fetch_beta_key(timeout: int = 20) -> tuple[str, str]:
    """(key, validity text) from the official forum post. Raises RuntimeError with a readable reason."""
    import urllib.request
    req = urllib.request.Request(MKV_KEY_URL, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) disc-ripper"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            html = r.read().decode("utf-8", "replace")
    except OSError as e:
        raise RuntimeError(f"Couldn't reach the MakeMKV forum ({e}). Check the internet connection.") from e
    keys = _KEY_RE.findall(html)
    if not keys:
        raise RuntimeError("No key found on the MakeMKV forum page - the page may have changed. "
                           f"Get it by hand from {MKV_KEY_URL}")
    m = re.search(r"valid until ([^.<]{3,40})", html)
    return keys[0], (m.group(1).strip() if m else "")


def key_expired(validity: str, today: "time.struct_time | None" = None) -> bool:
    """'end of November 2026' -> True once that month is over. Unknown text -> False."""
    m = re.search(r"(" + "|".join(_MONTHS) + r")\w*\s+(\d{4})", validity.lower())
    if not m:
        return False
    month, year = _MONTHS.index(m.group(1)) + 1, int(m.group(2))
    t = today or time.localtime()
    return (t.tm_year, t.tm_mon) > (year, month)


def current_mkv_key(path: Path = MKV_SETTINGS) -> str:
    try:
        for line in path.read_text().splitlines():
            m = re.match(r'\s*app_Key\s*=\s*"([^"]*)"', line)
            if m:
                return m.group(1)
    except OSError:
        pass
    return ""


def install_mkv_key(key: str, path: Path = MKV_SETTINGS) -> Path | None:
    """Put app_Key into MakeMKV's settings.conf (replacing any old key). Returns the backup made, if any."""
    if not _KEY_RE.fullmatch(key):
        raise ValueError("That doesn't look like a MakeMKV key")
    path.parent.mkdir(parents=True, exist_ok=True)
    lines, backup = [], None
    if path.exists():
        old = path.read_text()
        backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        backup.write_text(old)
        lines = [l for l in old.splitlines() if not re.match(r"\s*app_Key\s*=", l)]
    lines.append(f'app_Key = "{key}"')
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(path)
    return backup


# ---------------------------------------------------------------- MakeMKV updates (Flatpak only)
MKV_FLATPAK = "com.makemkv.MakeMKV"


def mkv_is_flatpak() -> bool:
    return makemkvcon_cmd()[:1] == ["flatpak"]


def mkv_update_available(timeout: int = 120) -> bool:
    """Does Flathub have a newer MakeMKV? (read-only check)"""
    r = subprocess.run(["flatpak", "remote-ls", "--updates", "--app", "--columns=application"],
                       capture_output=True, text=True, timeout=timeout)
    return MKV_FLATPAK in r.stdout.split()


def update_mkv(timeout: int = 1800) -> tuple[bool, str]:
    """Update the MakeMKV Flatpak. Normally needs no password for the logged-in desktop user."""
    r = subprocess.run(["flatpak", "update", "-y", "--noninteractive", MKV_FLATPAK],
                       capture_output=True, text=True, timeout=timeout)
    out = (r.stdout + r.stderr).strip().splitlines()
    return r.returncode == 0, (out[-1] if out else "")


def mkv_version() -> str:
    try:
        r = subprocess.run(["flatpak", "info", MKV_FLATPAK], capture_output=True, text=True, timeout=20)
        m = re.search(r"Version:\s*(\S+)", r.stdout)
        return m.group(1) if m else ""
    except (OSError, subprocess.SubprocessError):
        return ""


_OLD_RE = re.compile(r"too old|expired|evaluation period|key not found|invalid key|registration", re.I)


def needs_update_hint(msg: str) -> str:
    """Extra advice for the failure banner when MakeMKV refused because it's out of date / unregistered."""
    if not _OLD_RE.search(msg or ""):
        return ""
    if mkv_is_flatpak():
        return ("MakeMKV says it's out of date or its key expired. Update it: "
                "flatpak update com.makemkv.MakeMKV  - then Settings → Get latest key, and try again.")
    return "MakeMKV says it's out of date or its key expired. Install the latest MakeMKV, get the latest key, try again."
