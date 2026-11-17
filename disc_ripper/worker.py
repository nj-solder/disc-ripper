"""Background job that rips one disc: info pass, then one title at a time, with stall detection and one retry.

The same job handles TV discs and movies; what differs is which titles get picked (`choose`) and what
happens to the files at the end (`finish`).
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QThread, Signal

from . import core


def choose_episodes(min_min: int, max_min: int) -> Callable[[list], set]:
    """TV: every title inside the episode length window."""
    lo, hi = min_min * 60, max_min * 60
    return lambda titles: {t["idx"] for t in titles if lo <= t["secs"] <= hi}


def choose_movie(extras: bool, extras_min: int) -> Callable[[list], set]:
    """Movie: the longest title (the main feature), plus extras over extras_min minutes if wanted."""
    def pick(titles):
        if not titles:
            return set()
        main = max(titles, key=lambda t: (t["secs"], -t["idx"]))
        got = {main["idx"]}
        if extras:
            got |= {t["idx"] for t in titles if t["secs"] >= extras_min * 60}
        return got
    return pick


class RipJob(QThread):
    log = Signal(str, str)              # message, level: info | ok | warn | error
    titles = Signal(object)             # [{idx, secs, length, name, wanted}]
    title_state = Signal(int, str)      # title index, ripping | saved | failed | skipped | done
    status = Signal(object)             # phase, pct, bytes, rate, elapsed, idle, what
    finished_disc = Signal(bool, str)   # ok, message

    def __init__(self, dev: str, out_dir: Path, tag: str, settings: dict, minlen_secs: int,
                 choose: Callable[[list], set], finish: Callable[["RipJob"], str] | None = None):
        super().__init__()
        self.dev, self.dir, self.tag = dev, Path(out_dir), tag
        self.minlen = minlen_secs
        self.choose, self.finish = choose, finish
        self.stall_secs = int(settings.get("stall_secs", 1200))
        self.mount = settings.get("drive_mnt", "")
        self.excluded: set[int] = set()   # title indexes the user unticked
        self.saved: dict[int, Path] = {}  # title index -> file saved this run
        self.scanned: list[dict] = []     # titles from the last info pass
        self._stop = False
        self._proc: subprocess.Popen | None = None
        self._tmp = Path(tempfile.mkdtemp(prefix="disc-ripper-"))

    # ------------------------------------------------------------ control
    def stop(self) -> None:
        """Immediate stop: kill this drive's MakeMKV now (in the background so the window never freezes)."""
        self._stop = True
        threading.Thread(target=core.kill_mkv, args=(self.dev, self._proc), daemon=True).start()

    # ------------------------------------------------------------ main
    def run(self) -> None:
        try:
            ok, msg = self._run()
        except Exception as e:  # never leave the UI hanging
            ok, msg = False, f"Internal error: {e}"
            self.log.emit(msg, "error")
        self.finished_disc.emit(ok, msg)

    def _run(self) -> tuple[bool, str]:
        if not core.is_mounted(self.mount):
            return False, f"The hard drive is not mounted at {self.mount}"
        self.cmd = core.makemkvcon_cmd()
        if not self.cmd:
            return False, "MakeMKV is not installed"
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / ".incomplete").touch()
        (self.dir / ".failed").unlink(missing_ok=True)

        msg = ""
        for attempt in (1, 2):
            if attempt == 2:
                self.log.emit("Retrying disc (attempt 2)…", "warn")
                time.sleep(5)
            ok, msg = self._attempt()
            if self._stop:
                return False, "Stopped"
            if ok:
                done_msg = "All titles saved"
                if self.finish:
                    try:
                        done_msg = self.finish(self) or done_msg
                    except OSError as e:
                        self.log.emit(f"Saved, but renaming failed: {e}", "warn")
                (self.dir / ".incomplete").unlink(missing_ok=True)
                return True, done_msg
        (self.dir / ".failed").write_text(msg + "\n")
        return False, msg

    def _attempt(self) -> tuple[bool, str]:
        minlen = f"--minlength={self.minlen}"
        # ---- info pass
        self.log.emit("Scanning disc…", "info")
        info = self._tmp / "info.txt"
        # A freshly inserted disc takes a while to spin up; MakeMKV then says "Failed to open disc".
        # That's not a failure - wait for the drive and try again (up to ~1 minute) before giving up.
        for wait in (0, 10, 15, 20, 20):
            if wait:
                self.log.emit(f"Disc not ready yet (still spinning up?) - trying again in {wait} s…", "warn")
                self.status.emit({"what": f"{self.tag} scan", "phase": "waiting for the disc to spin up", "pct": 0,
                                  "bytes": 0, "rate": 0, "elapsed": 0, "idle": 0, "stall": self.stall_secs})
                for _ in range(wait):
                    if self._stop:
                        return False, "stopped"
                    time.sleep(1)
            res = self._watch(info, None, None, [minlen, "info", f"dev:{self.dev}"], f"{self.tag} scan")
            text = _read(info)
            titles = core.parse_titles(text)
            if titles or res in ("stall", "stopped") or "Failed to open disc" not in text:
                break
        if res != "ok" or not titles:
            return False, f"Scan failed ({self._why(res, text)})"
        picked = self.choose(titles)
        for t in titles:
            t["wanted"] = t["idx"] in picked and t["idx"] not in self.excluded
        self.scanned = titles
        self.titles.emit(titles)
        wanted = [t for t in titles if t["wanted"]]
        if not wanted:
            return False, "No titles to rip on this disc (check the length settings)"
        self.log.emit("To rip: " + ", ".join(f"#{t['idx']} {t['length']}" for t in wanted), "info")

        # ---- one title at a time
        for n, t in enumerate(wanted, 1):
            idx = t["idx"]
            if (self.dir / f".done_t{idx}").exists():
                self.title_state.emit(idx, "done")
                continue
            if idx in self.excluded:                   # unticked while ripping
                self.title_state.emit(idx, "skipped")
                continue
            if t["name"]:
                left = core.keep_partial(self.dir / t["name"])   # leftover from an unfinished run
                if left:
                    self.log.emit(f"Old unfinished file kept as {left.name}", "warn")
            self.title_state.emit(idx, "ripping")
            self.log.emit(f"Ripping title #{idx} ({t['length']})…", "info")
            start = self._tmp / f"start_{idx}"
            start.touch()
            out = self._tmp / f"rip_{idx}.txt"
            res = self._watch(out, self.dir, start, [minlen, "mkv", f"dev:{self.dev}", str(idx), str(self.dir)],
                              f"{self.tag} title {n}/{len(wanted)}")
            text = _read(out)
            new = _new_files(self.dir, start)
            good = [f for f in new if f.stat().st_size > 1_000_000]
            if res == "ok" and core.saved_ok(text) and good:
                (self.dir / f".done_t{idx}").touch()
                self.saved[idx] = good[0]
                self.title_state.emit(idx, "saved")
                self.log.emit(f"Title #{idx} saved as {good[0].name}", "ok")
                continue
            for f in new:                                  # never leave a half file looking finished
                p = core.keep_partial(f)
                if p:
                    self.log.emit(f"Unfinished file kept as {p.name}", "warn")
            self.title_state.emit(idx, "failed")
            why = self._why(res, text)
            self.log.emit(f"Title #{idx} FAILED: {why}", "error")
            return False, f"Title #{idx} failed: {why}"
        return True, ""

    def _why(self, res: str, text: str) -> str:
        if res == "stall":
            return f"stalled - no progress for {self.stall_secs // 60} min"
        if res == "stopped":
            return "stopped"
        return core.last_msg(text) or res

    # ------------------------------------------------------------ run MakeMKV and watch it
    def _watch(self, out: Path, watch_dir: Path | None, start: Path | None, args: list[str], what: str) -> str:
        """Run makemkvcon; returns ok | rc=N | stall | stopped."""
        if self._stop:
            return "stopped"
        with open(out, "w") as fh:
            self._proc = subprocess.Popen(self.cmd + ["-r", "--progress=-same"] + args, stdout=fh,
                                          stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
        began = changed = time.time()
        last_sig, prev_bytes = None, 0
        while self._proc.poll() is None:
            time.sleep(1)
            if self._stop:
                core.kill_mkv(self.dev, self._proc)
                return "stopped"
            now = time.time()
            written = sum(f.stat().st_size for f in _new_files(watch_dir, start)) if watch_dir else 0
            sig = (out.stat().st_size if out.exists() else 0, written)
            if sig != last_sig:
                last_sig, changed = sig, now
            phase, pct = core.progress(_read(out))
            idle = int(now - changed)
            self.status.emit({"what": what, "phase": phase or "starting", "pct": pct, "bytes": written,
                              "rate": max(0, written - prev_bytes), "elapsed": int(now - began), "idle": idle,
                              "stall": self.stall_secs})
            prev_bytes = written
            if idle >= self.stall_secs:
                self.log.emit(f"STALL: no progress for {self.stall_secs // 60} min ({phase}) - stopping MakeMKV", "error")
                core.kill_mkv(self.dev, self._proc)
                return "stall"
        rc = self._proc.returncode
        return "ok" if rc == 0 else f"rc={rc}"


def movie_finisher(name: str) -> Callable[[RipJob], str]:
    """After a movie rips: main feature -> '<name>.mkv', anything else -> Extras/ (Plex/Jellyfin layout)."""
    def finish(job: RipJob) -> str:
        if not job.saved:
            return "Nothing new saved (already done?)"
        picked = [t for t in job.scanned if t.get("wanted")]
        main_idx = max(picked, key=lambda t: (t["secs"], -t["idx"]))["idx"] if picked else None
        main = job.dir / f"{name}.mkv"
        if main_idx in job.saved:                      # main feature ripped this run
            if main.exists():
                main = job.dir / f"{name} - {main_idx}.mkv"
            job.saved[main_idx].rename(main)
            job.log.emit(f"Main feature saved as {main.name}", "ok")
        extras = [i for i in job.saved if i != main_idx]
        if extras:
            ex = job.dir / "Extras"
            ex.mkdir(exist_ok=True)
            for i in extras:
                job.saved[i].rename(ex / job.saved[i].name)
            job.log.emit(f"{len(extras)} extra(s) moved to Extras/", "ok")
        return f"Saved {main.name}" + (f" + {len(extras)} extras" if extras else "")
    return finish


def _read(p: Path) -> str:
    try:
        return p.read_text(errors="replace")
    except OSError:
        return ""


def _new_files(d: Path | None, start: Path | None) -> list[Path]:
    """.mkv files in d written since start was touched."""
    if not d or not start or not start.exists():
        return []
    t0 = start.stat().st_mtime
    out = []
    try:
        for e in os.scandir(d):
            if e.name.endswith(".mkv") and e.stat().st_mtime > t0:
                out.append(Path(e.path))
    except OSError:
        pass
    return out
