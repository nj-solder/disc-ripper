"""Disc Ripper main window: TV Shows tab (disc grid) and Movies tab, sharing one rip panel."""
from __future__ import annotations

import re
import subprocess
import sys
import time

from PySide6.QtCore import QThread, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSpinBox, QSplitter, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from . import core, kodi
from .worker import RipJob, choose_episodes, choose_movie, movie_finisher

APP_NAME = "Disc Ripper"

# tile colours: background, text
TILE = {
    "new":     ("#3a3f4b", "#c9ced8"),
    "partial": ("#b7791f", "#ffffff"),
    "done":    ("#2f855a", "#ffffff"),
    "failed":  ("#c53030", "#ffffff"),
    "active":  ("#3182ce", "#ffffff"),
}
STATE_WORD = {"new": "not started", "partial": "partly done", "done": "done", "failed": "FAILED"}
LOG_COLOURS = {"info": "#c9ced8", "ok": "#68d391", "warn": "#f6ad55", "error": "#fc8181"}
TITLE_STATES = {
    "waiting": ("", "#c9ced8"), "ripping": ("ripping…", "#63b3ed"), "saved": ("saved ✓", "#68d391"),
    "done": ("already saved ✓", "#68d391"), "failed": ("FAILED ✗", "#fc8181"), "skipped": ("skipped", "#a0aec0"),
}

STYLE = """
QWidget { background: #1e2128; color: #e2e8f0; font-size: 10.5pt; }
QPushButton { background: #2d323c; border: 1px solid #454b57; border-radius: 6px; padding: 6px 12px; }
QPushButton:hover { background: #383e4a; }
QPushButton:disabled { color: #6b7280; }
QPushButton#stop { background: #c53030; border: none; color: white; font-weight: bold; padding: 6px 22px; }
QPushButton#primary { background: #3182ce; border: none; color: white; font-weight: bold; padding: 8px 18px; }
QPushButton#primary:disabled { background: #2a4365; color: #a0aec0; }
QComboBox, QLineEdit, QSpinBox { background: #2d323c; border: 1px solid #454b57; border-radius: 5px; padding: 4px 8px; }
QProgressBar { border: 1px solid #454b57; border-radius: 6px; text-align: center; height: 22px; background: #2d323c; }
QProgressBar::chunk { background: #3182ce; border-radius: 5px; }
QTableWidget, QListWidget { background: #23272f; gridline-color: #333844; border: 1px solid #333844; }
QListWidget::item { padding: 6px; }
QHeaderView::section { background: #2d323c; border: none; padding: 4px; }
QPlainTextEdit { background: #16181d; border: 1px solid #333844; font-family: monospace; font-size: 9.5pt; }
QScrollArea { border: none; }
QScrollBar:vertical { background: #1e2128; width: 10px; margin: 0; }
QScrollBar:horizontal { background: #1e2128; height: 10px; margin: 0; }
QScrollBar::handle { background: #454b57; border-radius: 5px; min-height: 30px; min-width: 30px; }
QScrollBar::handle:hover { background: #5a6170; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: none; }
QSplitter::handle { background: #2d323c; width: 3px; }
QTabWidget::pane { border: none; }
QTabBar::tab { background: #2d323c; padding: 8px 22px; border-top-left-radius: 6px; border-top-right-radius: 6px;
               margin-right: 3px; }
QTabBar::tab:selected { background: #3182ce; color: white; font-weight: bold; }
QLabel#h1 { font-size: 15pt; font-weight: bold; }
QLabel#muted { color: #a0aec0; }
"""


def fmt_t(s: int) -> str:
    return f"{s // 60}:{s % 60:02d}"


def nums(text: str) -> list[int]:
    return [int(x) for x in text.replace(",", " ").split()]


class EpisodeCounter(QThread):
    """Counts episodes per season in the background (ffprobe is slow the first time)."""
    counted = Signal(str, object)     # object: a dict with int keys doesn't convert as Qt "dict"

    def __init__(self, show: core.Show):
        super().__init__()
        self.show = show

    def run(self):
        try:
            self.counted.emit(self.show.name, core.count_episodes(self.show))
        except Exception:
            pass


class KeyFetcher(QThread):
    """Gets the public MakeMKV beta key from the official forum (network, so off the UI thread)."""
    done = Signal(object)            # (key, validity) or an error string

    def run(self):
        try:
            self.done.emit(core.fetch_beta_key())
        except Exception as e:
            self.done.emit(str(e))


class MkvUpdater(QThread):
    """Checks Flathub for a newer MakeMKV and installs it (off the UI thread). Runs each time the app opens."""
    done = Signal(str, str)          # result: updated | current | failed | skipped, detail

    def run(self):
        try:
            if not core.mkv_is_flatpak():
                self.done.emit("skipped", "MakeMKV isn't the Flatpak - update it by hand when needed")
                return
            if not core.mkv_update_available():
                self.done.emit("current", core.mkv_version())
                return
            ok, out = core.update_mkv()
            self.done.emit("updated" if ok else "failed", core.mkv_version() if ok else out)
        except Exception as e:
            self.done.emit("failed", str(e))


class _Call(QThread):
    """Run one function in the background; connect to .finished."""
    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        self.fn()


class KodiPlanThread(QThread):
    """Episode list (network) + plan (ffprobe/ffmpeg) - both slow, so off the UI thread."""
    done = Signal(object)            # (info, actions, notes) or an error string

    def __init__(self, show: core.Show, titles: bool, refresh: bool):
        super().__init__()
        self.show, self.titles, self.refresh = show, titles, refresh

    def run(self):
        try:
            info = kodi.lookup(self.show, refresh=self.refresh)
            acts, notes = kodi.plan(self.show, info, self.titles)
            self.done.emit((info, acts, notes))
        except Exception as e:
            self.done.emit(str(e))


class KodiDialog(QDialog):
    """Preview and apply Kodi-style episode names (reversible)."""

    def __init__(self, parent, show: core.Show, locked: bool):
        super().__init__(parent)
        self.tv, self.locked, self.actions = show, locked, []
        self.setWindowTitle(f"Kodi names - {show.name}")
        self.resize(1000, 680)
        v = QVBoxLayout(self)
        intro = QLabel("Gives each episode a name Kodi recognises, e.g. <b>The 100 S01E01 - Pilot.mkv</b>, using the "
                       "official episode list from TVmaze. Files stay in their disc folders. Extras and duplicate titles "
                       "move to a <i>_not-episodes</i> folder that Kodi ignores. Nothing is deleted, and <b>Undo</b> "
                       "puts every original name back.")
        intro.setWordWrap(True); v.addWidget(intro)
        self.status = QLabel("Looking up episodes…"); self.status.setObjectName("muted"); v.addWidget(self.status)
        self.titles_chk = QCheckBox("Include episode titles in the file names"); self.titles_chk.setChecked(True)
        self.titles_chk.toggled.connect(lambda: self._plan(False)); v.addWidget(self.titles_chk)
        self.seasons = QTableWidget(0, 2); self.seasons.setHorizontalHeaderLabels(["Season", "Status"])
        self.seasons.verticalHeader().hide(); self.seasons.setEditTriggers(QTableWidget.NoEditTriggers)
        self.seasons.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        v.addWidget(self.seasons, 1)
        self.changes = QTableWidget(0, 4); self.changes.setHorizontalHeaderLabels(["Season", "Now", "Becomes", "Type"])
        self.changes.verticalHeader().hide(); self.changes.setEditTriggers(QTableWidget.NoEditTriggers)
        self.changes.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        v.addWidget(self.changes, 2)
        row = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh episode list"); self.refresh_btn.clicked.connect(lambda: self._plan(True))
        self.undo_btn = QPushButton("Undo all renames"); self.undo_btn.clicked.connect(self._undo)
        self.apply_btn = QPushButton("Rename"); self.apply_btn.setObjectName("primary"); self.apply_btn.clicked.connect(self._apply)
        close = QPushButton("Close"); close.clicked.connect(self.accept)
        row.addWidget(self.refresh_btn); row.addWidget(self.undo_btn); row.addStretch(1)
        row.addWidget(self.apply_btn); row.addWidget(close)
        v.addLayout(row)
        self._plan(False)

    def _plan(self, refresh: bool):
        self.apply_btn.setEnabled(False); self.refresh_btn.setEnabled(False)
        self.status.setText("Looking up episodes and checking files (first time can take a minute)…")
        self._t = KodiPlanThread(self.tv, self.titles_chk.isChecked(), refresh)
        self._t.done.connect(self._planned); self._t.start()

    def _planned(self, res):
        self.refresh_btn.setEnabled(True)
        self.undo_btn.setEnabled(kodi.has_renames(self.tv))
        if isinstance(res, str):
            self.status.setText(f"<span style='color:#fc8181'>{res}</span>"); return
        info, self.actions, notes = res
        total = sum(len(e) for e in info["episodes"].values())
        self.status.setText(f"TVmaze: <b>{info['name']} ({info['year']})</b> - {len(info['episodes'])} seasons, "
                            f"{total} episodes.   <b>{len(self.actions)}</b> change(s) to make.")
        self.seasons.setRowCount(len(notes))
        for r, (se, note) in enumerate(sorted(notes.items())):
            self.seasons.setItem(r, 0, QTableWidgetItem(f"Season {se}"))
            it = QTableWidgetItem(note)
            it.setForeground(QColor("#68d391" if note.startswith("ready") and "missing" not in note else
                                    "#f6ad55" if note.startswith("ready") else "#a0aec0"))
            self.seasons.setItem(r, 1, it)
        self.changes.setRowCount(len(self.actions))
        for r, a in enumerate(self.actions):
            self.changes.setItem(r, 0, QTableWidgetItem(f"S{a.season:02d} {a.src.parent.name}"))
            self.changes.setItem(r, 1, QTableWidgetItem(a.src.name))
            self.changes.setItem(r, 2, QTableWidgetItem(
                a.dst.name if a.kind == "episode" else f"{kodi.NOT_EPISODES}/{a.dst.name}"))
            t = QTableWidgetItem(a.kind if a.kind == "episode" else f"{a.kind}: {a.note}")
            t.setForeground(QColor("#c9ced8" if a.kind == "episode" else "#f6ad55"))
            self.changes.setItem(r, 3, t)
        self.changes.resizeColumnsToContents()
        self.changes.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.apply_btn.setText(f"Rename ({len(self.actions)})")
        self.apply_btn.setEnabled(bool(self.actions) and not self.locked)
        if self.locked:
            self.status.setText(self.status.text() + "   <span style='color:#f6ad55'>A disc of this show is ripping - "
                                "rename after it finishes.</span>")

    def _apply(self):
        if QMessageBox.question(self, APP_NAME, f"Rename {len(self.actions)} file(s)?\n"
                                "You can put everything back with “Undo all renames”.") != QMessageBox.Yes:
            return
        n, errs = kodi.apply(self.tv, self.actions)
        QMessageBox.information(self, APP_NAME, f"Renamed {n} file(s)." + ("\n\nProblems:\n" + "\n".join(errs[:15]) if errs else ""))
        self._plan(False)

    def _undo(self):
        if QMessageBox.question(self, APP_NAME, "Put every file back to its original name?") != QMessageBox.Yes:
            return
        n, errs = kodi.undo(self.tv)
        QMessageBox.information(self, APP_NAME, f"Restored {n} file(s)." + ("\n\nProblems:\n" + "\n".join(errs[:15]) if errs else ""))
        self._plan(False)


def parse_layout(text: str) -> dict:
    """'S1: 5 5 4 4 4 | S5: 5 4 5 5 3' -> {'1': [5,5,4,4,4], '5': [5,4,5,5,3]}"""
    out = {}
    for part in re.split(r"[|;\n]+", text):
        m = re.match(r"\s*S?(\d+)\s*:\s*([\d\s,]+)$", part.strip(), re.I)
        if part.strip() and not m:
            raise ValueError(part)
        if m:
            out[str(int(m.group(1)))] = nums(m.group(2))
    return out


def layout_text(d: dict) -> str:
    return " | ".join(f"S{k}: {' '.join(map(str, v))}" for k, v in sorted(d.items(), key=lambda x: int(x[0])))


class ShowDialog(QDialog):
    """New show / edit show."""

    def __init__(self, parent, show: core.Show | None = None):
        super().__init__(parent)
        self.setWindowTitle("Edit show" if show else "New show")
        self.setMinimumWidth(460)
        f = QFormLayout(self)
        self.name = QLineEdit(show.name if show else "")
        self.name.setReadOnly(bool(show))
        self.discs = QLineEdit(" ".join(map(str, show.discs)) if show else "")
        self.discs.setPlaceholderText("e.g. 5 5 5 4")
        self.eps = QLineEdit(" ".join(map(str, show.episodes)) if show else "")
        self.eps.setPlaceholderText("optional, e.g. 22 22 23 22")
        self.lookup_btn = QPushButton("Look up online"); self.lookup_btn.clicked.connect(self._lookup)
        self.layout_edit = QLineEdit(layout_text(show.disc_eps) if show else "")
        self.layout_edit.setPlaceholderText("optional, e.g. S1: 5 5 4 4 4 | S5: 5 4 5 5 3")
        self._show = show
        self.eplen = QSpinBox(); self.eplen.setRange(0, 300); self.eplen.setSuffix(" min")
        self.eplen.setSpecialValueText("unknown"); self.eplen.setValue(show.ep_len if show else 0)
        self.lo = QSpinBox(); self.lo.setRange(1, 300); self.lo.setValue(show.min_min if show else 10)
        self.hi = QSpinBox(); self.hi.setRange(1, 600); self.hi.setValue(show.max_min if show else 60)
        f.addRow("Show name", self.name)
        f.addRow("Discs per season", self.discs)
        erow = QHBoxLayout(); erow.addWidget(self.eps, 1); erow.addWidget(self.lookup_btn)
        f.addRow("Episodes per season", erow)
        f.addRow("Episodes per disc", self.layout_edit)
        f.addRow("Typical episode length", self.eplen)
        f.addRow("Rip titles from (min)", self.lo)
        f.addRow("… up to (min)", self.hi)
        hint = QLabel("Titles outside the rip range are skipped (menus, “Play All”).\n"
                      "Typical: 10–60 for hour-long shows, 15–35 for sitcoms.\n"
                      "Episodes per season lets each season show e.g. “21/22 episodes” (“Look up online” fills it in).\n"
                      "Episodes per disc is only needed for Kodi names when a season has a damaged/missing disc.")
        hint.setObjectName("muted")
        f.addRow(hint)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok); bb.rejected.connect(self.reject)
        f.addRow(bb)

    def _lookup(self):
        name = self.name.text().strip()
        if not name:
            QMessageBox.information(self, APP_NAME, "Type the show name first."); return
        tmp = core.Show(name, [], root=self._show.root if self._show else "")
        if self._show:
            tmp.tvmaze_id = self._show.tvmaze_id
        self.lookup_btn.setEnabled(False); self.lookup_btn.setText("Looking up…")
        self._lk = _Call(lambda: self._lookup_run(tmp)); self._lk.finished.connect(self._lookup_done); self._lk.start()

    def _lookup_run(self, tmp):
        try:
            import urllib.parse, urllib.request, json as _j
            q = urllib.parse.urlencode({"q": tmp.name})
            url = (f"{kodi.TVMAZE}/shows/{tmp.tvmaze_id}?embed=episodes" if tmp.tvmaze_id
                   else f"{kodi.TVMAZE}/singlesearch/shows?{q}&embed=episodes")
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "disc-ripper"}), timeout=20) as r:
                d = _j.load(r)
            counts: dict[int, int] = {}
            for e in d.get("_embedded", {}).get("episodes", []):
                if e.get("season") and e.get("number"):
                    counts[e["season"]] = counts.get(e["season"], 0) + 1
            self._lk_res = (d["name"], d.get("premiered", "")[:4], d["id"], [counts[k] for k in sorted(counts)])
        except Exception as e:
            self._lk_res = str(e)

    def _lookup_done(self):
        self.lookup_btn.setEnabled(True); self.lookup_btn.setText("Look up online")
        res = self._lk_res
        if isinstance(res, str):
            QMessageBox.warning(self, APP_NAME, f"Couldn't look it up: {res}"); return
        name, year, tid, counts = res
        if QMessageBox.question(self, APP_NAME, f"Found <b>{name} ({year})</b>: {len(counts)} seasons, "
                                f"{sum(counts)} episodes ({' '.join(map(str, counts))}).<br>Use these numbers?") == QMessageBox.Yes:
            self.eps.setText(" ".join(map(str, counts)))
            self.result_tvmaze = tid
            if not self.discs.text().strip():
                self.discs.setPlaceholderText(f"{len(counts)} seasons - type the discs per season")

    def _ok(self):
        try:
            d = nums(self.discs.text())
            assert d and all(0 < x < 30 for x in d)
        except (ValueError, AssertionError):
            QMessageBox.warning(self, APP_NAME, "Discs per season: numbers separated by spaces, e.g. 5 5 5 4")
            return
        try:
            e = nums(self.eps.text())
            assert not e or len(e) == len(d)
        except (ValueError, AssertionError):
            QMessageBox.warning(self, APP_NAME, "Episodes per season: one number per season (or leave it empty).")
            return
        if not self.name.text().strip():
            QMessageBox.warning(self, APP_NAME, "Give the show a name.")
            return
        try:
            self.result_layout = parse_layout(self.layout_edit.text())
        except ValueError as e:
            QMessageBox.warning(self, APP_NAME, f"Episodes per disc: can't read “{e}”. Use e.g. S5: 5 4 5 5 3")
            return
        if self.lo.value() >= self.hi.value():
            QMessageBox.warning(self, APP_NAME, "“Rip titles from” must be less than “up to”.")
            return
        self.result_discs, self.result_eps = d, e
        self.accept()


class SettingsDialog(QDialog):
    def __init__(self, parent, s: dict):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(640)
        self.s = dict(s)
        f = QFormLayout(self)
        self.mnt = QLineEdit(s["drive_mnt"])
        self.root = QLineEdit(s["rips_root"])
        pick = QPushButton("Browse…"); pick.clicked.connect(self._pick)
        row = QHBoxLayout(); row.addWidget(self.root); row.addWidget(pick)
        self.stall = QSpinBox(); self.stall.setRange(2, 120); self.stall.setValue(int(s["stall_secs"]) // 60)
        self.eject = QCheckBox("Eject the disc when it finishes"); self.eject.setChecked(bool(s["auto_eject"]))
        self.autoupd = QCheckBox("Update MakeMKV automatically when Disc Ripper opens")
        self.autoupd.setChecked(bool(s.get("auto_update", True)))
        self.autokey = QCheckBox("Get the latest MakeMKV beta key when Disc Ripper opens")
        self.autokey.setChecked(bool(s.get("auto_key", True)))
        self.key_lbl = QLabel(); self.key_lbl.setWordWrap(True)
        self.key_btn = QPushButton("Get latest key"); self.key_btn.clicked.connect(self._get_key)
        self._show_key()
        self.mnt.setPlaceholderText("optional - e.g. /media/you/MyDrive")
        f.addRow("Require this drive mounted", self.mnt)
        f.addRow("Rips folder", row)
        f.addRow("Stall timeout (min)", self.stall)
        f.addRow(self.eject)
        krow = QHBoxLayout(); krow.addWidget(self.key_lbl, 1); krow.addWidget(self.key_btn)
        f.addRow("MakeMKV key", krow)
        f.addRow(self.autokey)
        f.addRow(self.autoupd)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        f.addRow(bb)

    def _show_key(self):
        have = core.current_mkv_key()
        valid = self.s.get("key_valid", "")
        if not have:
            self.key_lbl.setText("<span style='color:#fc8181'>No key set</span> (DVDs still work)")
        elif valid and core.key_expired(valid):
            self.key_lbl.setText(f"<span style='color:#fc8181'>Expired ({valid})</span> - get the latest key")
        else:
            self.key_lbl.setText(f"<span style='color:#68d391'>Set</span>" + (f" - valid until {valid}" if valid else ""))

    def _get_key(self):
        self.key_btn.setEnabled(False); self.key_lbl.setText("Checking the MakeMKV forum…")
        self._kf = KeyFetcher(); self._kf.done.connect(self._got_key); self._kf.start()

    def _got_key(self, res):
        self.key_btn.setEnabled(True)
        if isinstance(res, str):
            QMessageBox.warning(self, APP_NAME, res); self._show_key(); return
        key, valid = res
        self.s["key_valid"], self.s["key_checked"] = valid, time.strftime("%Y-%m-%d")
        if key == core.current_mkv_key():
            QMessageBox.information(self, APP_NAME, f"You already have the latest key" +
                                    (f" (valid until {valid})." if valid else "."))
        else:
            core.install_mkv_key(key)
            QMessageBox.information(self, APP_NAME, "New MakeMKV key installed" +
                                    (f" (valid until {valid})" if valid else "") +
                                    ".\nIf the MakeMKV app is open, close and reopen it.")
        self._show_key()

    def _pick(self):
        d = QFileDialog.getExistingDirectory(self, "Rips folder", self.root.text())
        if d:
            self.root.setText(d)

    def values(self) -> dict:
        self.s.update(drive_mnt=self.mnt.text().strip(), rips_root=self.root.text().strip(),
                      stall_secs=self.stall.value() * 60, auto_eject=self.eject.isChecked(),
                      auto_key=self.autokey.isChecked(), auto_update=self.autoupd.isChecked())
        return self.s


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1280, 800)
        self.settings = core.load_settings()
        self.show_: core.Show | None = None
        self.job: RipJob | None = None
        self.active: tuple[int, int] | None = None     # (season, disc) while a TV disc rips
        self.job_show: core.Show | None = None         # the show being ripped (you can browse others meanwhile)
        self.active_movie: str | None = None           # movie folder name while a movie rips
        self.tiles: dict[tuple[int, int], QPushButton] = {}
        self.season_lbls: dict[int, QLabel] = {}
        self.ep_counts: dict[int, int] = {}
        self.counter: EpisodeCounter | None = None
        self.title_rows: dict[int, int] = {}
        self._build()
        self._refresh_drives()
        self._refresh_disc()
        self._refresh_shows()
        self._refresh_movies()
        t = QTimer(self); t.timeout.connect(self._tick); t.start(3000)
        self.updating = False
        if "--offscreen-smoke" not in sys.argv:          # self-tests (e.g. the installer) never update/fetch anything
            QTimer.singleShot(1500, self._auto_update)
            QTimer.singleShot(2000, self._auto_key)

    # ================================================================ layout
    def _build(self):
        root = QWidget(); self.setCentralWidget(root)
        v = QVBoxLayout(root); v.setContentsMargins(12, 10, 12, 10); v.setSpacing(8)

        # top bar: drive, disc, eject, STOP
        top = QHBoxLayout()
        top.addWidget(QLabel("Drive"))
        self.drive_box = QComboBox(); self.drive_box.setMinimumWidth(190)
        self.drive_box.currentIndexChanged.connect(self._drive_changed)
        top.addWidget(self.drive_box)
        self.disc_lbl = QLabel("—"); self.disc_lbl.setObjectName("muted")
        top.addWidget(self.disc_lbl, 1)
        self.mount_lbl = QLabel(); top.addWidget(self.mount_lbl)
        b = QPushButton("Settings…"); b.clicked.connect(self._settings); top.addWidget(b)
        self.eject_btn = QPushButton("⏏  Eject"); self.eject_btn.clicked.connect(lambda: self._eject())
        top.addWidget(self.eject_btn)
        self.stop_btn = QPushButton("■  STOP"); self.stop_btn.setObjectName("stop")
        self.stop_btn.clicked.connect(self._stop)
        top.addWidget(self.stop_btn)
        v.addLayout(top)

        split = QSplitter(Qt.Horizontal); v.addWidget(split, 1)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_shows_tab(), "📺  TV Shows")
        self.tabs.addTab(self._build_movies_tab(), "🎬  Movies")
        split.addWidget(self.tabs)
        split.addWidget(self._build_job_panel())
        split.setSizes([720, 560])
        self._set_running(False)

    def _build_shows_tab(self) -> QWidget:
        w = QWidget(); lv = QVBoxLayout(w); lv.setContentsMargins(0, 8, 0, 0)
        self.left_pane = w
        sb = QHBoxLayout()
        sb.addWidget(QLabel("Show"))
        self.show_box = QComboBox(); self.show_box.setMinimumWidth(220)
        self.show_box.currentTextChanged.connect(self._show_changed)
        sb.addWidget(self.show_box, 1)
        b = QPushButton("New show…"); b.clicked.connect(self._new_show); sb.addWidget(b)
        b = QPushButton("Edit show…"); b.clicked.connect(self._edit_show); sb.addWidget(b)
        b = QPushButton("Kodi names…"); b.clicked.connect(self._kodi_names); sb.addWidget(b)
        lv.addLayout(sb)
        legend = QLabel("Click a disc to rip it.   "
                        + "  ".join(f"<span style='color:{TILE[k][0]}'>■</span> {n}" for k, n in
                                    (("new", "not started"), ("partial", "partly done"), ("done", "done"),
                                     ("failed", "failed"), ("active", "ripping now"))))
        legend.setObjectName("muted"); lv.addWidget(legend)
        self.grid_host = QWidget(); self.grid = QGridLayout(self.grid_host); self.grid.setSpacing(6)
        self.grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        sa = QScrollArea(); sa.setWidgetResizable(True); sa.setWidget(self.grid_host)
        lv.addWidget(sa, 1)
        self.summary_lbl = QLabel(); self.summary_lbl.setObjectName("muted"); lv.addWidget(self.summary_lbl)
        return w

    def _build_movies_tab(self) -> QWidget:
        w = QWidget(); lv = QVBoxLayout(w); lv.setContentsMargins(0, 8, 0, 0)
        f = QFormLayout()
        self.mv_title = QLineEdit(); self.mv_title.setPlaceholderText("e.g. John Wick")
        fill = QPushButton("Use disc name"); fill.clicked.connect(self._fill_title)
        row = QHBoxLayout(); row.addWidget(self.mv_title, 1); row.addWidget(fill)
        f.addRow("Movie title", row)
        self.mv_year = QLineEdit(); self.mv_year.setPlaceholderText("e.g. 2014 (optional)"); self.mv_year.setMaximumWidth(160)
        f.addRow("Year", self.mv_year)
        self.mv_extras = QCheckBox("Also rip extras longer than")
        self.mv_extras_min = QSpinBox(); self.mv_extras_min.setRange(1, 120); self.mv_extras_min.setValue(5)
        self.mv_extras_min.setSuffix(" min")
        row = QHBoxLayout(); row.addWidget(self.mv_extras); row.addWidget(self.mv_extras_min); row.addStretch(1)
        f.addRow("Extras", row)
        lv.addLayout(f)
        self.mv_where = QLabel(); self.mv_where.setObjectName("muted"); self.mv_where.setWordWrap(True)
        lv.addWidget(self.mv_where)
        self.mv_title.textChanged.connect(self._movie_where); self.mv_year.textChanged.connect(self._movie_where)
        self.mv_btn = QPushButton("🎬  Rip movie"); self.mv_btn.setObjectName("primary")
        self.mv_btn.clicked.connect(self._start_movie)
        lv.addWidget(self.mv_btn, 0, Qt.AlignLeft)
        lv.addSpacing(10)
        lv.addWidget(QLabel("Your movies"))
        self.mv_list = QListWidget(); self.mv_list.itemDoubleClicked.connect(self._open_movie)
        lv.addWidget(self.mv_list, 1)
        hint = QLabel("The longest title on the disc is taken as the movie. Saved as Movies/Title (Year)/Title (Year).mkv, "
                      "extras in an Extras folder. Double-click a movie to open its folder.")
        hint.setObjectName("muted"); hint.setWordWrap(True); lv.addWidget(hint)
        self._movie_where()
        return w

    def _build_job_panel(self) -> QWidget:
        right = QWidget(); rv = QVBoxLayout(right); rv.setContentsMargins(8, 0, 0, 0)
        self.job_lbl = QLabel("Nothing ripping"); self.job_lbl.setObjectName("h1"); rv.addWidget(self.job_lbl)
        self.banner = QLabel(); self.banner.setWordWrap(True); self.banner.hide(); rv.addWidget(self.banner)
        self.phase_lbl = QLabel(""); self.phase_lbl.setObjectName("muted"); rv.addWidget(self.phase_lbl)
        self.bar = QProgressBar(); self.bar.setRange(0, 100); rv.addWidget(self.bar)
        self.stats_lbl = QLabel(""); rv.addWidget(self.stats_lbl)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Rip", "Title", "Length", "Status"])
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.itemChanged.connect(self._title_ticked)
        rv.addWidget(self.table, 2)
        self.log_view = QPlainTextEdit(); self.log_view.setReadOnly(True); rv.addWidget(self.log_view, 1)
        return right

    # ================================================================ drives
    def _refresh_drives(self):
        cur = self.drive_box.currentData()
        drives = core.list_drives(self.settings["nicknames"])
        if [d.dev for d in drives] == [self.drive_box.itemData(i) for i in range(self.drive_box.count())]:
            return
        self.drive_box.blockSignals(True)
        self.drive_box.clear()
        for d in drives:
            self.drive_box.addItem(d.label, d.dev)
        want = cur or self.settings.get("last_drive")
        i = self.drive_box.findData(want)
        self.drive_box.setCurrentIndex(i if i >= 0 else 0)
        self.drive_box.blockSignals(False)
        if not drives:
            self.disc_lbl.setText("No disc drive found - plug one in")

    def _drive_changed(self):
        self.settings["last_drive"] = self.drive_box.currentData() or ""
        core.save_settings(self.settings)
        self._refresh_disc()

    def dev(self) -> str:
        return self.drive_box.currentData() or ""

    def _refresh_disc(self):
        dev = self.dev()
        if not dev:
            return
        media, label = core.drive_media(dev)
        self.disc_label = label if media else ""
        if not media:
            self.disc_lbl.setText("Drive empty / tray open")
            return
        s, d = core.parse_label(label)
        guess = f"   →  looks like Season {s} Disc {d}" if s and d else ""
        self.disc_lbl.setText(f"Disc in drive: <b>{label or '(no label)'}</b>{guess}")

    def _eject(self, force: bool = False):
        dev = self.dev()
        if not dev:
            return
        if not force and self.busy():
            QMessageBox.information(self, APP_NAME, "Stop the rip first.")
            return
        subprocess.Popen(["eject", dev], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._log(f"Ejecting {dev}", "info")

    def busy(self) -> bool:
        return bool(self.job and self.job.isRunning())

    def _check_ready(self) -> str | None:
        """Common checks before any rip. Returns the drive, or None after telling the user why not."""
        if self.busy():
            QMessageBox.information(self, APP_NAME, "A disc is already ripping. Stop it first.")
            return None
        if not core.is_mounted(self.settings["drive_mnt"]):
            QMessageBox.warning(self, APP_NAME, f"The hard drive is not mounted at {self.settings['drive_mnt']}.\n"
                                                "Plug it in first.")
            return None
        dev = self.dev()
        if not dev:
            QMessageBox.warning(self, APP_NAME, "No disc drive found.")
            return None
        if not core.drive_media(dev)[0]:
            QMessageBox.warning(self, APP_NAME, "There's no disc in the drive.")
            return None
        if self.updating:
            QMessageBox.information(self, APP_NAME, "MakeMKV is updating - wait a moment and click again.")
            return None
        if core.mkv_pids(dev):
            QMessageBox.warning(self, APP_NAME, "MakeMKV is already using this drive (maybe ripshow.sh in a terminal).\n"
                                                "Finish or stop that first.")
            return None
        return dev

    # ================================================================ shows
    def _refresh_shows(self):
        mounted = core.is_mounted(self.settings["drive_mnt"])
        if not self.settings["drive_mnt"]:
            self.mount_lbl.setText(f"<span style='color:#a0aec0'>Saving to {self.settings['rips_root']}</span>")
        else:
            self.mount_lbl.setText("<span style='color:#68d391'>● Hard drive mounted</span>" if mounted else
                                   "<span style='color:#fc8181'>● Hard drive NOT mounted</span>")
        names = core.list_shows(self.settings["rips_root"]) if mounted else []
        cur = self.show_box.currentText() or self.settings.get("last_show", "")
        if names != [self.show_box.itemText(i) for i in range(self.show_box.count())]:
            self.show_box.blockSignals(True)
            self.show_box.clear(); self.show_box.addItems(names)
            if cur in names:
                self.show_box.setCurrentText(cur)
            self.show_box.blockSignals(False)
            self._show_changed(self.show_box.currentText())

    def _show_changed(self, name: str):
        self.show_ = core.load_show(self.settings["rips_root"], name) if name else None
        self.ep_counts = {}
        if name:
            self.settings["last_show"] = name
            core.save_settings(self.settings)
        self._build_grid()
        self._count_episodes()

    def _count_episodes(self):
        if not self.show_ or (self.counter and self.counter.isRunning()):
            return
        self.counter = EpisodeCounter(self.show_)
        self.counter.counted.connect(self._on_counted)
        self.counter.start()

    def _on_counted(self, name: str, counts: dict):
        if self.show_ and name == self.show_.name:
            self.ep_counts = counts
            self._refresh_tiles()

    def _new_show(self):
        if not core.is_mounted(self.settings["drive_mnt"]):
            QMessageBox.warning(self, APP_NAME, "Plug in / mount the hard drive first.")
            return
        dlg = ShowDialog(self)
        if dlg.exec():
            sh = core.Show(dlg.name.text().strip(), dlg.result_discs, dlg.lo.value(), dlg.hi.value(),
                           dlg.result_eps, dlg.eplen.value(), dlg.result_layout,
                           getattr(dlg, "result_tvmaze", 0), root=self.settings["rips_root"])
            sh.save()
            self._refresh_shows()
            self.show_box.setCurrentText(sh.name)

    def _edit_show(self):
        if not self.show_:
            return
        dlg = ShowDialog(self, self.show_)
        if dlg.exec():
            sh = self.show_
            sh.discs, sh.episodes, sh.ep_len = dlg.result_discs, dlg.result_eps, dlg.eplen.value()
            sh.min_min, sh.max_min = dlg.lo.value(), dlg.hi.value()
            sh.disc_eps = dlg.result_layout
            sh.tvmaze_id = getattr(dlg, "result_tvmaze", sh.tvmaze_id)
            sh.save()
            self._build_grid()
            self._count_episodes()

    def _kodi_names(self):
        if not self.show_:
            return
        if not core.is_mounted(self.settings["drive_mnt"]):
            QMessageBox.warning(self, APP_NAME, "Plug in / mount the hard drive first."); return
        locked = self.busy() and self.job_show is not None and self.job_show.name == self.show_.name
        KodiDialog(self, self.show_, locked).exec()
        self._count_episodes()

    def _settings(self):
        dlg = SettingsDialog(self, self.settings)
        if dlg.exec():
            self.settings = dlg.values()
            core.save_settings(self.settings)
            self.show_box.clear()
            self._refresh_shows()
            self._refresh_movies()

    # ---------------------------------------------------------------- disc grid
    def _build_grid(self):
        while self.grid.count():
            w = self.grid.takeAt(0).widget()
            if w:
                w.deleteLater()
        self.tiles.clear(); self.season_lbls.clear()
        if not self.show_:
            self.summary_lbl.setText("Pick a show or create one with “New show…”.")
            return
        for si, n in enumerate(self.show_.discs, 1):
            lbl = QLabel(f"Season {si}"); lbl.setMinimumWidth(124)
            self.grid.addWidget(lbl, si - 1, 0)
            self.season_lbls[si] = lbl
            for d in range(1, n + 1):
                b = QPushButton(); b.setFixedSize(104, 58); b.setCursor(Qt.PointingHandCursor)
                b.clicked.connect(lambda _=False, s=si, d=d: self._start_show(s, d))
                b.setContextMenuPolicy(Qt.CustomContextMenu)
                b.customContextMenuRequested.connect(lambda _p, s=si, d=d: self._tile_menu(s, d))
                self.grid.addWidget(b, si - 1, d)
                self.tiles[(si, d)] = b
        need = 134 + max(self.show_.discs) * (104 + 6) + 24
        self.left_pane.setMinimumWidth(min(need, 900))
        self._refresh_tiles()

    def _refresh_tiles(self):
        if not self.show_:
            return
        counts = {"done": 0, "partial": 0, "failed": 0, "new": 0}
        season_done: dict[int, bool] = {}
        for (s, d), b in self.tiles.items():
            st = core.disc_state(self.show_.disc_dir(s, d))
            season_done[s] = season_done.get(s, True) and st.state == "done"
            ripping_here = self.job_show is not None and self.job_show.name == self.show_.name
            key = "active" if ripping_here and self.active == (s, d) else st.state
            counts[st.state] += 1
            text = {"new": "", "partial": f"{st.saved} saved", "done": f"✓ {st.mkv} files",
                    "failed": "✗ failed", "active": "ripping…"}[key]
            b.setText(f"Disc {d}\n{text}")
            bg, fg = TILE[key]
            b.setStyleSheet(f"QPushButton {{ background:{bg}; color:{fg}; border:none; border-radius:8px; font-weight:600; }}"
                            f"QPushButton:hover {{ border:2px solid #e2e8f0; }}")
            tip = f"Season {s} Disc {d}: {STATE_WORD[st.state]}"
            if st.reason:
                tip += f"\n{st.reason}"
            b.setToolTip(tip + f"\n{self.show_.disc_dir(s, d)}")
        # season labels: "Season 5 · 21/22 eps"
        for s, lbl in self.season_lbls.items():
            have, want = self.ep_counts.get(s), self.show_.expected(s)
            if have is None or not want:
                lbl.setText(f"Season {s}" + (f"<br><span style='color:#a0aec0; font-size:9pt'>{have} eps</span>"
                                             if have else ""))
                continue
            if have == want:
                colour, note = "#68d391", f"{have}/{want} eps ✓"
            elif have > want:
                colour, note = "#f6ad55", f"{have}/{want} · extra?"
            elif season_done.get(s):
                colour, note = "#fc8181", f"{have}/{want} · {want - have} missing"
            else:
                colour, note = "#a0aec0", f"{have}/{want} eps"
            lbl.setText(f"Season {s}<br><span style='color:{colour}; font-size:9pt'>{note}</span>")
        total = sum(counts.values())
        eps = sum(self.ep_counts.values()) if self.ep_counts else None
        want = sum(self.show_.episodes) if self.show_.episodes else None
        ep_txt = f" · {eps}/{want} episodes" if eps is not None and want else ""
        self.summary_lbl.setText(f"{counts['done']}/{total} discs done · {counts['partial']} partly done · "
                                 f"{counts['failed']} failed · {counts['new']} not started{ep_txt}")

    def _tile_menu(self, s: int, d: int):
        m = QMenu(self)
        a = QAction(f"Rip Season {s} Disc {d}", self); a.triggered.connect(lambda: self._start_show(s, d)); m.addAction(a)
        ddir = self.show_.disc_dir(s, d)
        a = QAction("Open folder", self)
        a.triggered.connect(lambda: subprocess.Popen(["xdg-open", str(ddir if ddir.exists() else ddir.parent)]))
        m.addAction(a)
        if (ddir / ".failed").exists():
            a = QAction("Clear 'failed' mark", self)
            a.triggered.connect(lambda: ((ddir / ".failed").unlink(missing_ok=True), self._refresh_tiles())); m.addAction(a)
        m.exec(self.cursor().pos())

    def _start_show(self, s: int, d: int):
        if not self.show_:
            return
        st = core.disc_state(self.show_.disc_dir(s, d))
        if st.state == "done" and not self.busy():
            if QMessageBox.question(self, APP_NAME,
                                    f"Season {s} Disc {d} is already done ({st.mkv} files).\n\n"
                                    "Scan it again and rip anything that's missing?\n"
                                    "Titles already saved are skipped - nothing is re-ripped.") != QMessageBox.Yes:
                return
        dev = self._check_ready()
        if not dev:
            return
        label = core.drive_media(dev)[1]
        ls, ld = core.parse_label(label)
        if (ls and ls != s) or (ld and ld != d):
            got = f"Season {ls} Disc {ld}" if ls else f"Disc {ld}"
            box = QMessageBox(self); box.setIcon(QMessageBox.Warning); box.setWindowTitle("Wrong disc?")
            box.setText(f"You clicked <b>Season {s} Disc {d}</b>, but the disc in the drive is labelled "
                        f"<b>{label}</b> ({got}).")
            use_label = box.addButton(f"Rip it as {got}", QMessageBox.AcceptRole) if ls and ld else None
            anyway = box.addButton(f"Rip as Season {s} Disc {d} anyway", QMessageBox.DestructiveRole)
            box.addButton("Cancel", QMessageBox.RejectRole)
            box.exec()
            if use_label is not None and box.clickedButton() is use_label:
                if ls > len(self.show_.discs) or ld > self.show_.discs[ls - 1]:
                    QMessageBox.warning(self, APP_NAME, f"{got} isn't in this show's disc list.")
                    return
                return self._start_show(ls, ld)
            if box.clickedButton() is not anyway:
                return
        sh = self.show_
        self.active, self.active_movie, self.job_show = (s, d), None, sh
        job = RipJob(dev, sh.disc_dir(s, d), f"S{s:02d} D{d}", self.settings, sh.min_min * 60,
                     choose_episodes(sh.min_min, sh.max_min))
        self._launch(job, f"{sh.name} · Season {s} · Disc {d}")

    # ================================================================ movies
    def _refresh_movies(self):
        if self.busy() and self.active_movie:
            pass
        mounted = core.is_mounted(self.settings["drive_mnt"])
        items = core.list_movies(self.settings["rips_root"]) if mounted else []
        sig = [(n, st.state) for n, st in items]
        if getattr(self, "_movie_sig", None) == sig:
            return
        self._movie_sig = sig
        self.mv_list.clear()
        if not items:
            self.mv_list.addItem("No movies yet." if mounted else "Hard drive not mounted.")
            return
        for name, st in items:
            key = "active" if self.active_movie == name else st.state
            it = QListWidgetItem(f"{name}    —  {STATE_WORD.get(st.state, st.state) if key != 'active' else 'ripping…'}")
            it.setForeground(QColor({"done": "#68d391", "partial": "#f6ad55", "failed": "#fc8181",
                                     "active": "#63b3ed"}.get(key, "#c9ced8")))
            it.setData(Qt.UserRole, name)
            if st.reason:
                it.setToolTip(st.reason)
            self.mv_list.addItem(it)

    def _movie_name(self) -> str:
        return core.movie_folder_name(self.mv_title.text(), self.mv_year.text().strip())

    def _movie_where(self):
        n = self._movie_name()
        self.mv_where.setText(f"Will be saved as: Movies/{n}/{n}.mkv" if self.mv_title.text().strip()
                              else "Type the movie's title (and year, for Plex/Jellyfin).")

    def _fill_title(self):
        label = getattr(self, "disc_label", "")
        if not label:
            QMessageBox.information(self, APP_NAME, "No disc label to use - insert the disc first.")
            return
        self.mv_title.setText(core.title_from_label(label))

    def _open_movie(self, item: QListWidgetItem):
        name = item.data(Qt.UserRole)
        if name:
            subprocess.Popen(["xdg-open", str(core.movies_root(self.settings["rips_root"]) / name)])

    def _start_movie(self):
        if not self.mv_title.text().strip():
            QMessageBox.warning(self, APP_NAME, "Type the movie's title first.")
            return
        year = self.mv_year.text().strip()
        if year and not (year.isdigit() and len(year) == 4):
            QMessageBox.warning(self, APP_NAME, "Year should be 4 digits, e.g. 2014 (or leave it empty).")
            return
        dev = self._check_ready()
        if not dev:
            return
        name = self._movie_name()
        folder = core.movies_root(self.settings["rips_root"]) / name
        if core.disc_state(folder).state == "done":
            if QMessageBox.question(self, APP_NAME, f"“{name}” is already ripped. Rip it again into the same folder?") \
                    != QMessageBox.Yes:
                return
            for m in folder.glob(".done_t*"):
                m.unlink()
        extras = self.mv_extras.isChecked()
        minlen = min(self.mv_extras_min.value() * 60, 600) if extras else 600
        self.active, self.active_movie, self.job_show = None, name, None
        job = RipJob(dev, folder, name, self.settings, minlen,
                     choose_movie(extras, self.mv_extras_min.value()), movie_finisher(name))
        self._launch(job, f"🎬  {name}")
        self._movie_sig = None
        self._refresh_movies()

    # ================================================================ running a job
    def _launch(self, job: RipJob, heading: str):
        self.job = job
        job.log.connect(self._log)
        job.titles.connect(self._on_titles)
        job.title_state.connect(self._on_title_state)
        job.status.connect(self._on_status)
        job.finished_disc.connect(self._on_done)
        self.table.setRowCount(0); self.title_rows.clear()
        self.banner.hide(); self.bar.setValue(0); self.stats_lbl.setText(""); self.phase_lbl.setText("")
        self.job_lbl.setText(heading)
        self._log(f"Start: {heading} on {job.dev}", "info")
        self._set_running(True)
        self._refresh_tiles()
        job.start()

    def _stop(self):
        dev = self.dev()
        if self.busy():
            self.job.stop()
            self._log("STOP pressed - stopping MakeMKV", "warn")
        elif dev:
            core.kill_mkv(dev)          # stray MakeMKV on this drive (e.g. from the old script)
            self._log("STOP pressed - killed any MakeMKV on this drive", "warn")

    def _set_running(self, on: bool):
        self.eject_btn.setEnabled(not on)
        self.drive_box.setEnabled(not on)
        self.mv_btn.setEnabled(not on)

    def _on_titles(self, titles: list):
        self.table.blockSignals(True)
        self.table.setRowCount(len(titles)); self.title_rows.clear()
        for r, t in enumerate(titles):
            chk = QTableWidgetItem(); chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            chk.setCheckState(Qt.Checked if t["wanted"] else Qt.Unchecked)
            chk.setData(Qt.UserRole, t["idx"])
            self.table.setItem(r, 0, chk)
            self.table.setItem(r, 1, QTableWidgetItem(f"#{t['idx']}  {t['name']}"))
            self.table.setItem(r, 2, QTableWidgetItem(t["length"]))
            self.table.setItem(r, 3, QTableWidgetItem("" if t["wanted"] else "skipped (length)"))
            self.title_rows[t["idx"]] = r
            if self.job and (self.job.dir / f".done_t{t['idx']}").exists():
                self._paint_state(r, "done")
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.blockSignals(False)

    def _title_ticked(self, item: QTableWidgetItem):
        if item.column() != 0 or not self.job:
            return
        idx = item.data(Qt.UserRole)
        if item.checkState() == Qt.Checked:
            self.job.excluded.discard(idx)
        else:
            self.job.excluded.add(idx)

    def _paint_state(self, row: int, state: str):
        text, colour = TITLE_STATES[state]
        it = QTableWidgetItem(text); it.setForeground(QColor(colour))
        self.table.blockSignals(True); self.table.setItem(row, 3, it); self.table.blockSignals(False)

    def _on_title_state(self, idx: int, state: str):
        if idx in self.title_rows:
            self._paint_state(self.title_rows[idx], state)
        self._refresh_tiles()

    def _on_status(self, st: dict):
        self.phase_lbl.setText(f"{st['what']} — {st['phase']}")
        self.bar.setValue(st["pct"])
        parts = []
        if st["bytes"]:
            parts.append(f"{st['bytes'] / 1048576:,.0f} MB  ·  {st['rate'] / 1048576:.1f} MB/s")
        parts.append(f"elapsed {fmt_t(st['elapsed'])}")
        if st["idle"] >= 60:
            parts.append(f"<span style='color:#f6ad55'>no progress {fmt_t(st['idle'])} "
                         f"(gives up at {fmt_t(st['stall'])})</span>")
        self.stats_lbl.setText("   ".join(parts))

    def _on_done(self, ok: bool, msg: str):
        what = (f"Season {self.active[0]} Disc {self.active[1]}" if self.active else f"“{self.active_movie}”")
        was_show = self.active is not None
        job_show = self.job_show
        self.active, self.active_movie, self.job_show = None, None, None
        self._set_running(False)
        self._refresh_tiles()
        self._movie_sig = None
        self._refresh_movies()
        self.phase_lbl.setText(""); self.stats_lbl.setText("")
        if ok:
            self.bar.setValue(100)
            if was_show:
                nxt = self._next_disc(job_show)
                tail = (f"Next: insert {job_show.name} Season {nxt[0]} Disc {nxt[1]} and click it." if nxt
                        else f"All {job_show.name} discs done!")
                self._count_episodes()
            else:
                tail = msg
            self._banner(f"✓  {what} finished.  {tail}", "#2f855a")
            self._log(f"{what} finished. {msg}", "ok")
            core.notify(f"{what} done", tail)
            if self.settings.get("auto_eject"):
                self._eject(force=True)
        elif msg == "Stopped":
            self._banner(f"■  Stopped. {what} is marked unfinished - start it again to continue.", "#4a5568")
            self._log("Stopped", "warn")
        else:
            hint = core.needs_update_hint(msg)
            self._banner(f"✗  FAILED: {what}\n{msg}\n" + (hint or
                         "Anything already saved is kept. Check/clean the disc and start it again to finish it."), "#c53030")
            self._log(f"{what} FAILED: {msg}", "error")
            core.notify(f"FAILED: {what}", msg, critical=True)
            QApplication.alert(self)

    def _next_disc(self, show: core.Show):
        for si, n in enumerate(show.discs, 1):
            for d in range(1, n + 1):
                if core.disc_state(show.disc_dir(si, d)).state != "done":
                    return si, d
        return None

    def _banner(self, text: str, bg: str):
        self.banner.setText(text)
        self.banner.setStyleSheet(f"background:{bg}; color:white; font-weight:bold; font-size:12pt; "
                                  f"padding:10px; border-radius:8px;")
        self.banner.show()

    def _log(self, msg: str, level: str = "info"):
        colour = LOG_COLOURS.get(level, "#c9ced8")
        self.log_view.appendHtml(f"<span style='color:#718096'>{time.strftime('%H:%M:%S')}</span> "
                                 f"<span style='color:{colour}'>{msg}</span>")
        self.log_view.moveCursor(QTextCursor.End)
        target = self.job.dir.parent.parent if self.job and self.active else (
            core.movies_root(self.settings["rips_root"]) if self.active_movie else None)
        if target is None and self.show_:
            target = self.show_.folder
        if target and core.is_mounted(self.settings["drive_mnt"]):
            try:
                target.mkdir(parents=True, exist_ok=True)
                with open(target / "rip-app.log", "a") as f:
                    f.write(f"{time.strftime('%F %T')} [{self.dev()}] {msg}\n")
            except OSError:
                pass

    # ================================================================ MakeMKV updates
    def _auto_update(self):
        s = self.settings
        if not s.get("auto_update", True) or self.busy():
            return
        if subprocess.run(["pgrep", "-x", "makemkvcon"], capture_output=True).returncode == 0:
            self._log("MakeMKV is running (another rip?) - skipping today's update check", "info")
            return
        self.updating = True
        self._log("Checking for a MakeMKV update…", "info")
        self._upd = MkvUpdater(); self._upd.done.connect(self._auto_update_done); self._upd.start()

    def _auto_update_done(self, result: str, detail: str):
        self.updating = False
        if result in ("updated", "current", "skipped"):
            self.settings["update_checked"] = time.strftime("%Y-%m-%d")
            core.save_settings(self.settings)
        if result == "updated":
            self._log(f"MakeMKV updated to {detail}", "ok")
        elif result == "current":
            self._log(f"MakeMKV is up to date ({detail})", "info")
        elif result == "skipped":
            self._log(detail, "info")
        else:
            self._log(f"MakeMKV update failed: {detail}. Try: flatpak update com.makemkv.MakeMKV", "warn")

    # ================================================================ MakeMKV key
    def _auto_key(self):
        """Each time the app opens: fetch the public beta key and install it if it changed."""
        s = self.settings
        if not s.get("auto_key", True):
            if s.get("key_valid") and core.key_expired(s["key_valid"]):
                self._log(f"MakeMKV key expired ({s['key_valid']}) - Settings → Get latest key", "warn")
            return
        self._kf = KeyFetcher(); self._kf.done.connect(self._auto_key_done); self._kf.start()

    def _auto_key_done(self, res):
        if isinstance(res, str):
            self._log(f"MakeMKV key check failed: {res}", "warn")
            return
        key, valid = res
        self.settings["key_valid"], self.settings["key_checked"] = valid, time.strftime("%Y-%m-%d")
        core.save_settings(self.settings)
        if key != core.current_mkv_key():
            try:
                core.install_mkv_key(key)
                self._log("New MakeMKV beta key installed" + (f" (valid until {valid})" if valid else ""), "ok")
            except (OSError, ValueError) as e:
                self._log(f"Couldn't install the MakeMKV key: {e}", "warn")
        else:
            self._log("MakeMKV key is up to date" + (f" (valid until {valid})" if valid else ""), "info")

    # ================================================================ timer / close
    def _tick(self):
        self._refresh_drives()
        self._refresh_disc()
        if not self.busy():
            self._refresh_shows()
        self._refresh_tiles()
        self._refresh_movies()

    def _wait_threads(self):
        """Let background checks (update, key, episode count) finish before Qt tears things down."""
        for t in (getattr(self, "_upd", None), getattr(self, "_kf", None), self.counter):
            if t is not None and t.isRunning():
                t.wait(20000)

    def closeEvent(self, e):
        if self.busy():
            if QMessageBox.question(self, APP_NAME, "A disc is ripping. Stop it and quit?") != QMessageBox.Yes:
                e.ignore()
                return
            self.job.stop()
            self.job.wait(15000)
        e.accept()


def main(argv=None):
    argv = sys.argv if argv is None else argv
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName("disc-ripper")
    app.setStyleSheet(STYLE)
    w = MainWindow()
    app.aboutToQuit.connect(w._wait_threads)
    if "--offscreen-smoke" in argv:
        QTimer.singleShot(1500, app.quit)
    w.show()
    return app.exec()
