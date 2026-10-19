"""The optional settings window (docs/33): Tkinter, which comes with Python, laid out like the Windows
program's window (three tabs: audio files, dictation, updates). The server runs in this process
while the window is open; closing the window stops it.

    clew-audio-server --gui

On Linux, Tkinter is the `python3-tk` package; without it, the console server still works.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import clew_audio_server as srv

from . import autostart, dictation, updates, version
from .service import Service
from .strings import t

INSTALL_DICTATION = 'pipx install --force "clew-tools[dictation] @ git+https://github.com/yaqalab/clew-tools"'
LANGS = [("", "lang.any"), ("en", "lang.en"), ("ja", "lang.ja"), ("fr", "lang.fr"), ("mg", "lang.mg")]
MASK = "●"


def human(n: int) -> str:
    return f"{n / 1024**3:.1f} GB" if n >= 1024**3 else f"{n / 1024**2:.0f} MB"


def open_path(path: str) -> None:
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    if shutil.which(opener):
        subprocess.Popen([opener, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Window:
    def __init__(self, root: tk.Tk, service: Service) -> None:
        self.root, self.svc = root, service
        self.dirty = threading.Event()
        self.svc.listeners.append(self.dirty.set)
        self.secret_shown = False
        self.key_shown = False
        self.upd_state = updates.State.load(self.svc.config.path.parent / "update.json")
        self.upd_text = t("upd.idle")
        self.upd_latest = ""
        self.checking = False
        self.download_problem = ""

        # Linux's default Tk look is dated; "clam" is Tk's own cleaner one. macOS keeps its native look.
        if sys.platform.startswith("linux") and "clam" in ttk.Style().theme_names():
            ttk.Style().theme_use("clam")
        root.title(t("app"))
        root.minsize(620, 560)
        root.protocol("WM_DELETE_WINDOW", self.close)
        pad = {"padx": 8, "pady": 4}

        self.tabs = ttk.Notebook(root)
        self.tabs.pack(fill="both", expand=True, padx=6, pady=6)
        self.audio = ttk.Frame(self.tabs, padding=6)
        self.stt = ttk.Frame(self.tabs, padding=6)
        self.upd = ttk.Frame(self.tabs, padding=6)
        self.tabs.add(self.audio, text=t("tab.audio"))
        self.tabs.add(self.stt, text=t("tab.dictation"))
        self.tabs.add(self.upd, text=t("tab.updates"))
        self._audio_page(pad)
        self._stt_page(pad)
        self._updates_page(pad)

        bottom = ttk.Frame(root, padding=(8, 0, 8, 8))
        bottom.pack(fill="x")
        self.autostart_var = tk.BooleanVar(value=autostart.is_on())
        ttk.Checkbutton(bottom, text=t("autostart"), variable=self.autostart_var, command=self.toggle_autostart).pack(anchor="w")
        ttk.Label(bottom, text=t("activity")).pack(anchor="w", pady=(6, 0))
        self.activity = tk.Listbox(bottom, height=4, activestyle="none")
        self.activity.pack(fill="x")
        row = ttk.Frame(bottom)
        row.pack(fill="x", pady=(6, 0))
        self.message = ttk.Label(row, text="")
        self.message.pack(side="left")
        ttk.Label(row, text=t("close.note"), foreground="gray").pack(side="right")

        self.fill()
        if not self.svc.config.folders:
            self.say(t("first.run"))
        self.root.after(500, self.poll)
        # The daily check a little after opening, then once an hour while the window is open.
        self.root.after(2000, self.maybe_check)

    # -- pages ------------------------------------------------------------------------------

    def _audio_page(self, pad: dict[str, int]) -> None:
        p = self.audio
        head = ttk.Frame(p)
        head.pack(fill="x")
        self.status = ttk.Label(head, text="", wraplength=440)
        self.status.pack(side="left", **pad)
        self.startstop = ttk.Button(head, text=t("stop"), command=self.toggle_server)
        self.startstop.pack(side="right", **pad)

        box = ttk.LabelFrame(p, text=t("folders"))
        box.pack(fill="x", **pad)
        self.folders = ttk.Treeview(box, columns=("on", "name", "path"), show="headings", height=4, selectmode="browse")
        for col, width in (("on", 50), ("name", 140), ("path", 300)):
            self.folders.heading(col, text=t(f"col.{col}"))
            self.folders.column(col, width=width, stretch=col == "path")
        self.folders.pack(side="left", fill="x", expand=True, padx=6, pady=6)
        self.folders.bind("<Double-1>", lambda e: self.toggle_folder())
        buttons = ttk.Frame(box)
        buttons.pack(side="right", padx=6)
        for key, fn in (("add", self.add_folder), ("remove", self.remove_folder), ("toggle", self.toggle_folder),
                        ("open", self.open_folder)):
            ttk.Button(buttons, text=t(key), command=fn).pack(fill="x", pady=2)

        conn = ttk.LabelFrame(p, text=t("connection"))
        conn.pack(fill="x", **pad)
        line = ttk.Frame(conn)
        line.pack(fill="x", padx=6, pady=4)
        ttk.Label(line, text=t("port")).pack(side="left")
        self.port = ttk.Entry(line, width=7)
        self.port.pack(side="left", padx=6)
        ttk.Label(line, text=t("hours")).pack(side="left", padx=(16, 0))
        self.hours = ttk.Entry(line, width=5)
        self.hours.pack(side="left", padx=6)
        ttk.Label(conn, text=t("origins"), wraplength=560).pack(anchor="w", padx=6)
        self.origins = tk.Text(conn, height=3, width=60)
        self.origins.pack(fill="x", padx=6, pady=4)

        sec = ttk.LabelFrame(p, text=t("secret"))
        sec.pack(fill="x", **pad)
        line = ttk.Frame(sec)
        line.pack(fill="x", padx=6, pady=4)
        self.secret = ttk.Entry(line, width=36, show=MASK)
        self.secret.pack(side="left")
        self.show_btn = ttk.Button(line, text=t("show"), command=self.toggle_secret)
        self.show_btn.pack(side="left", padx=4)
        ttk.Button(line, text=t("copy"), command=lambda: self.copy(self.svc.secret())).pack(side="left", padx=4)
        ttk.Button(line, text=t("new.secret"), command=self.new_secret).pack(side="left", padx=4)
        self.secret_hint = ttk.Label(sec, text="", wraplength=560)
        self.secret_hint.pack(anchor="w", padx=6, pady=(0, 4))

        conv = ttk.LabelFrame(p, text=t("conversion"))
        conv.pack(fill="x", **pad)
        line = ttk.Frame(conv)
        line.pack(fill="x", padx=6, pady=4)
        self.ffmpeg = ttk.Entry(line, width=40)
        self.ffmpeg.pack(side="left", fill="x", expand=True)
        ttk.Button(line, text=t("browse"), command=self.browse_ffmpeg).pack(side="left", padx=4)
        ttk.Button(line, text=t("find"), command=self.find_ffmpeg).pack(side="left", padx=4)
        ttk.Label(conv, text=t("ffmpeg.hint"), wraplength=560).pack(anchor="w", padx=6, pady=(0, 4))

        ttk.Button(p, text=t("save"), command=self.save).pack(anchor="e", **pad)

    def _stt_page(self, pad: dict[str, int]) -> None:
        p = self.stt
        head = ttk.Frame(p)
        head.pack(fill="x")
        self.stt_status = ttk.Label(head, text="", wraplength=440, justify="left")
        self.stt_status.pack(side="left", **pad)
        self.stt_startstop = ttk.Button(head, text=t("start"), command=self.toggle_dictation)
        self.stt_startstop.pack(side="right", **pad)

        box = ttk.LabelFrame(p, text=t("model"))
        box.pack(fill="x", **pad)
        self.models = tk.Listbox(box, height=7, exportselection=False, activestyle="none")
        self.models.pack(side="left", fill="x", expand=True, padx=6, pady=6)
        self.models.bind("<<ListboxSelect>>", lambda e: self.choose_model())
        self.download_btn = ttk.Button(box, text=t("download"), command=self.download)
        self.download_btn.pack(side="right", anchor="n", padx=6, pady=6)
        self.model_note = ttk.Label(p, text="", wraplength=560)
        self.model_note.pack(anchor="w", padx=8)

        folder = ttk.LabelFrame(p, text=t("models.folder"))
        folder.pack(fill="x", **pad)
        line = ttk.Frame(folder)
        line.pack(fill="x", padx=6, pady=4)
        self.models_dir = ttk.Entry(line, width=44)
        self.models_dir.pack(side="left", fill="x", expand=True)
        ttk.Button(line, text=t("change"), command=self.change_models_dir).pack(side="left", padx=4)
        ttk.Button(line, text=t("open"), command=lambda: self._open_models()).pack(side="left", padx=4)

        line = ttk.Frame(p)
        line.pack(fill="x", **pad)
        ttk.Label(line, text=t("language")).pack(side="left")
        self.lang = ttk.Combobox(line, state="readonly", width=18, values=[t(k) for _, k in LANGS])
        self.lang.pack(side="left", padx=6)
        self.lang.bind("<<ComboboxSelected>>", lambda e: self.choose_language())
        ttk.Label(line, text=t("port")).pack(side="left", padx=(16, 0))
        self.stt_port = ttk.Entry(line, width=7)
        self.stt_port.pack(side="left", padx=6)

        key = ttk.LabelFrame(p, text=t("key"))
        key.pack(fill="x", **pad)
        line = ttk.Frame(key)
        line.pack(fill="x", padx=6, pady=4)
        self.key = ttk.Entry(line, width=36, show=MASK)
        self.key.pack(side="left")
        self.key_btn = ttk.Button(line, text=t("show"), command=self.toggle_key)
        self.key_btn.pack(side="left", padx=4)
        ttk.Button(line, text=t("copy"), command=lambda: self.copy(self.svc.stt_key())).pack(side="left", padx=4)
        ttk.Button(line, text=t("new.key"), command=self.new_key).pack(side="left", padx=4)
        ttk.Label(key, text=t("key.hint"), wraplength=560).pack(anchor="w", padx=6, pady=(0, 4))

        self.stt_auto = tk.BooleanVar()
        ttk.Checkbutton(p, text=t("stt.autostart"), variable=self.stt_auto,
                        command=lambda: self.svc.set_stt(enabled=self.stt_auto.get())).pack(anchor="w", **pad)
        ttk.Label(p, text=t("stt.local"), foreground="gray").pack(anchor="w", padx=8)

    def _updates_page(self, pad: dict[str, int]) -> None:
        p = self.upd
        ttk.Label(p, text=t("version", version=version())).pack(anchor="w", **pad)
        box = ttk.LabelFrame(p, text=t("new.versions"))
        box.pack(fill="x", **pad)
        self.upd_auto = tk.BooleanVar(value=self.upd_state.check)
        ttk.Checkbutton(box, text=t("upd.auto"), variable=self.upd_auto, command=self.toggle_update_check).pack(anchor="w", padx=6, pady=4)
        line = ttk.Frame(box)
        line.pack(fill="x", padx=6, pady=4)
        self.upd_status = ttk.Label(line, text=self.upd_text, wraplength=420, justify="left")
        self.upd_status.pack(side="left")
        self.upd_btn = ttk.Button(line, text=t("upd.check"), command=lambda: self.check(quiet=False))
        self.upd_btn.pack(side="right")
        ttk.Button(box, text=t("upd.notes"), command=lambda: webbrowser.open(updates.release_page(self.upd_latest or None))).pack(anchor="w", padx=6, pady=4)
        ttk.Label(p, text=t("upd.how"), wraplength=560, justify="left").pack(anchor="w", **pad)

    # -- filling and refreshing --------------------------------------------------------------

    def fill(self) -> None:
        self.port.delete(0, "end")
        self.port.insert(0, str(self.svc.port))
        self.hours.delete(0, "end")
        self.hours.insert(0, str(self.svc.link_hours))
        self.origins.delete("1.0", "end")
        self.origins.insert("1.0", "\n".join(self.svc.config.origins))
        self.ffmpeg.delete(0, "end")
        self.ffmpeg.insert(0, self.svc.ffmpeg_setting)
        self._set_entry(self.secret, self.svc.secret() or "")
        self.secret_hint.configure(text=t("secret.hint", where=self.svc.keys.where) if self.svc.secret() else t("secret.unknown"))
        s = self.svc.stt
        self.models.delete(0, "end")
        for name, *_ in dictation.MODELS:
            self.models.insert("end", name)
        names = [m[0] for m in dictation.MODELS]
        if s["model"] in names:
            self.models.selection_set(names.index(s["model"]))
        self._set_entry(self.models_dir, s["modelsDir"])
        self.lang.current([code for code, _ in LANGS].index(s["language"]))
        self.stt_port.delete(0, "end")
        self.stt_port.insert(0, str(s["port"]))
        self._set_entry(self.key, self.svc.stt_key() if not dictation.available() else "")
        self.stt_auto.set(s["enabled"])
        self.fill_folders()
        self.refresh()

    @staticmethod
    def _set_entry(entry: ttk.Entry, value: str) -> None:
        entry.configure(state="normal")
        entry.delete(0, "end")
        entry.insert(0, value)
        entry.configure(state="readonly")

    def fill_folders(self) -> None:
        keep = self.folders.selection()
        self.folders.delete(*self.folders.get_children())
        entries = self.svc.config.folders
        for i, (name, e) in enumerate(zip(srv.folder_names([e["path"] for e in entries]), entries)):
            self.folders.insert("", "end", iid=str(i), values=("✓" if e["enabled"] else "—", name, e["path"]))
        if keep and self.folders.exists(keep[0]):
            self.folders.selection_set(keep[0])

    def refresh(self) -> None:
        svc = self.svc
        if svc.running():
            text = t("running", port=svc.port)
        elif svc.problem and not svc.stopped_by_user:
            text = t("not.running", why=svc.problem)
        else:
            text = t("stopped")
        self.status.configure(text=text)
        self.startstop.configure(text=t("stop") if svc.running() else t("start"))
        self.activity.delete(0, "end")
        for line in svc.recent:
            self.activity.insert("end", line)
        self.activity.see("end")

        d = svc.dictation
        why = dictation.available()
        s = svc.stt
        if why:
            st = t("stt.missing", command=INSTALL_DICTATION)
        elif d.state == "loading":
            st = t("stt.loading", model=s["model"])
        elif d.state == "ready":
            st = t("stt.ready", port=s["port"], model=s["model"])
        elif d.state == "failed":
            st = t("stt.failed", why=d.problem)
        else:
            st = t("stt.off")
        self.stt_status.configure(text=st)
        self.stt_startstop.configure(text=t("stop") if d.state in ("loading", "ready") else t("start"),
                                     state="disabled" if why else "normal")
        models_dir = Path(s["modelsDir"])
        for i, (name, size, use, _) in enumerate(dictation.MODELS):
            here = dictation.model_present(models_dir, name)
            line = t("model.line", name=name, size=human(size), use=t(f"use.{use}"), here=t("here" if here else "not.here"))
            if self.models.get(i) != line:
                selected = self.models.curselection()
                self.models.delete(i)
                self.models.insert(i, line)
                if selected:
                    self.models.selection_set(selected[0])
        chosen = self.chosen_model()
        self.download_btn.configure(
            state="normal" if not why and not d.downloading and chosen and not dictation.model_present(models_dir, chosen) else "disabled",
            text=f"{d.percent} %" if d.downloading else t("download"),
        )
        note = t("downloading", name=d.downloading, percent=d.percent) if d.downloading else (
            t("download.failed", why=self.download_problem) if self.download_problem else "")
        self.model_note.configure(text=note)
        self.upd_status.configure(text=self.upd_text)
        self.upd_btn.configure(state="disabled" if self.checking else "normal")

    def poll(self) -> None:
        if self.dirty.is_set():
            self.dirty.clear()
            self.refresh()
        self.root.after(500, self.poll)

    def say(self, text: str) -> None:
        self.message.configure(text=text)

    # -- audio actions ----------------------------------------------------------------------

    def toggle_server(self) -> None:
        if self.svc.running():
            self.svc.stop()
        else:
            problem = self.svc.start()
            if problem:
                self.say(problem)

    def _selected(self) -> int | None:
        sel = self.folders.selection()
        return int(sel[0]) if sel else None

    def add_folder(self) -> None:
        chosen = filedialog.askdirectory(parent=self.root, mustexist=True)
        if not chosen:
            return
        path = str(Path(chosen).resolve())
        entries = self.svc.config.folders
        if any(Path(e["path"]).resolve() == Path(path) for e in entries):
            self.say(t("already"))
            return
        self.say(self.svc.set_folders(entries + [{"path": path, "enabled": True}]) or t("saved"))
        self.fill_folders()

    def remove_folder(self) -> None:
        i = self._selected()
        entries = self.svc.config.folders
        if i is None or i >= len(entries):
            return
        if not messagebox.askokcancel(t("app"), t("remove.ask", path=entries[i]["path"]), parent=self.root):
            return
        self.say(self.svc.set_folders(entries[:i] + entries[i + 1:]) or t("saved"))
        self.fill_folders()

    def toggle_folder(self) -> None:
        i = self._selected()
        entries = self.svc.config.folders
        if i is None or i >= len(entries):
            return
        entries[i]["enabled"] = not entries[i]["enabled"]
        self.say(self.svc.set_folders(entries) or t("saved"))
        self.fill_folders()

    def open_folder(self) -> None:
        i = self._selected()
        entries = self.svc.config.folders
        if i is not None and i < len(entries):
            open_path(entries[i]["path"])

    def toggle_secret(self) -> None:
        self.secret_shown = not self.secret_shown
        self.secret.configure(show="" if self.secret_shown else MASK)
        self.show_btn.configure(text=t("hide") if self.secret_shown else t("show"))

    def copy(self, value: str | None) -> None:
        if not value:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(value)
        self.say(t("copied"))

    def new_secret(self) -> None:
        if messagebox.askokcancel(t("app"), t("new.secret.ask"), parent=self.root, default="cancel"):
            self._set_entry(self.secret, self.svc.reset_secret())
            self.secret_hint.configure(text=t("secret.hint", where=self.svc.keys.where))
            if not self.secret_shown:
                self.toggle_secret()

    def browse_ffmpeg(self) -> None:
        chosen = filedialog.askopenfilename(parent=self.root)
        if chosen:
            self.ffmpeg.delete(0, "end")
            self.ffmpeg.insert(0, chosen)

    def find_ffmpeg(self) -> None:
        found = shutil.which("ffmpeg")
        if found:
            self.ffmpeg.delete(0, "end")
            self.ffmpeg.insert(0, found)
        else:
            self.say(t("ffmpeg.none"))

    def save(self) -> None:
        try:
            port = int(self.port.get())
            assert 1024 <= port <= 65535
        except (ValueError, AssertionError):
            return self.say(t("bad.port"))
        try:
            hours = int(self.hours.get())
            assert 1 <= hours <= 168
        except (ValueError, AssertionError):
            return self.say(t("bad.hours"))
        origins = []
        for line in self.origins.get("1.0", "end").splitlines():
            line = line.strip().rstrip("/")
            if not line:
                continue
            if not (line.startswith("https://") or line.startswith("http://")) or "/" in line.split("://", 1)[1]:
                return self.say(t("bad.origin", line=line))
            origins.append(line)
        try:
            stt_port = int(self.stt_port.get())
            assert 1024 <= stt_port <= 65535 and stt_port != port
        except (ValueError, AssertionError):
            self.tabs.select(self.stt)
            return self.say(t("bad.port"))
        self.svc.set_stt(port=stt_port)
        self.say(self.svc.apply(port, hours, origins, self.ffmpeg.get().strip()) or t("saved"))
        self.fill()

    def toggle_autostart(self) -> None:
        if self.autostart_var.get():
            autostart.enable(gui=True)
        else:
            autostart.disable()

    # -- dictation actions ------------------------------------------------------------------

    def chosen_model(self) -> str | None:
        sel = self.models.curselection()
        return dictation.MODELS[sel[0]][0] if sel else None

    def choose_model(self) -> None:
        name = self.chosen_model()
        if name and name != self.svc.stt["model"]:
            self.svc.set_stt(model=name)
            if self.svc.dictation.state in ("loading", "ready"):
                self.svc.start_dictation()
        self.refresh()

    def choose_language(self) -> None:
        code = LANGS[self.lang.current()][0]
        self.svc.set_stt(language=code)
        if self.svc.dictation.state in ("loading", "ready"):
            self.svc.start_dictation()

    def toggle_dictation(self) -> None:
        if self.svc.dictation.state in ("loading", "ready"):
            self.svc.dictation.stop()
        else:
            self.svc.start_dictation()
        self.refresh()

    def download(self) -> None:
        name = self.chosen_model()
        if not name:
            return
        size = next(m[1] for m in dictation.MODELS if m[0] == name)
        folder = self.svc.stt["modelsDir"]
        if not messagebox.askokcancel(t("app"), t("download.ask", name=name, size=human(size), folder=folder), parent=self.root):
            return
        self.download_problem = ""

        def done(problem: str) -> None:
            self.download_problem = problem
            # The speaker model, which live transcription needs, comes after the first model.
            if not problem and not (Path(folder) / "speakers" / "campplus.onnx").is_file():
                self.svc.dictation.download("speakers", Path(folder))
            self.dirty.set()

        self.svc.dictation.download(name, Path(folder), done)

    def change_models_dir(self) -> None:
        chosen = filedialog.askdirectory(parent=self.root)
        if chosen:
            self.svc.set_stt(modelsDir=chosen)
            self._set_entry(self.models_dir, chosen)
            self.refresh()

    def _open_models(self) -> None:
        folder = Path(self.svc.stt["modelsDir"])
        folder.mkdir(parents=True, exist_ok=True)
        open_path(str(folder))

    def toggle_key(self) -> None:
        self.key_shown = not self.key_shown
        self.key.configure(show="" if self.key_shown else MASK)
        self.key_btn.configure(text=t("hide") if self.key_shown else t("show"))

    def new_key(self) -> None:
        if messagebox.askokcancel(t("app"), t("new.key.ask"), parent=self.root, default="cancel"):
            self._set_entry(self.key, self.svc.reset_stt_key())
            if not self.key_shown:
                self.toggle_key()

    # -- updates ----------------------------------------------------------------------------

    def _upd_path(self) -> Path:
        return self.svc.config.path.parent / "update.json"

    def toggle_update_check(self) -> None:
        self.upd_state.check = self.upd_auto.get()
        self.upd_state.save(self._upd_path())
        if self.upd_state.check:
            self.maybe_check()

    def maybe_check(self) -> None:
        if self.upd_state.due():
            self.check(quiet=True)
        self.root.after(60 * 60 * 1000, self.maybe_check)

    def check(self, quiet: bool) -> None:
        if self.checking:
            return
        self.checking = True
        self.upd_text = t("upd.checking")
        self.refresh()

        # The worker only sets fields and the dirty flag; Tk is touched from its own thread alone.
        def run() -> None:
            try:
                latest = updates.fetch_latest()
            except (OSError, ValueError) as e:
                self.upd_text = t("upd.idle") if quiet else t("upd.failed", why=str(e))
            else:
                self.upd_state.last_check = int(time.time())
                self.upd_latest = latest
                if updates.newer(latest):
                    self.upd_text = t("upd.available", version=latest, command=updates.UPGRADE)
                    self.upd_state.notified = latest
                else:
                    self.upd_text = t("upd.current")
                self.upd_state.save(self._upd_path())
            self.checking = False
            self.dirty.set()

        threading.Thread(target=run, daemon=True).start()

    # -- closing ----------------------------------------------------------------------------

    def close(self) -> None:
        self.svc.shutdown()
        self.root.destroy()


def open_window(config_path: Path, root: tk.Tk) -> Window:
    """Starts the server with the saved settings and builds the window on `root` (the tests drive it)."""
    service = Service(config_path)
    service.start()
    service.start_dictation_if_enabled()
    return Window(root, service)


def run(config_path: Path, minimized: bool = False) -> int:
    """Opens the window and serves until it is closed."""
    root = tk.Tk()
    open_window(config_path, root)
    if minimized:
        root.iconify()
    root.mainloop()
    return 0
