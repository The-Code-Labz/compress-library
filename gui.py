#!/usr/bin/env python3
"""
gui.py - interactive desktop control panel for compress-library.

Pure Tkinter (stdlib) front-end. Never imports compress_library internals and
never touches media itself - it only builds a CLI invocation and shells out to
`compress_library.py`, then observes progress via the subprocess's stdout log
stream and by reading the SQLite manifest. This keeps the encode/verify/replace
core completely untouched and testable on its own.

Run:
    python gui.py
"""
from __future__ import annotations

import json
import os
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is a requirements.txt dep, but degrade gracefully
    psutil = None

TOOL_DIR = Path(__file__).resolve().parent
SCRIPT_PATH = TOOL_DIR / "compress_library.py"
GUI_PATH = Path(__file__).resolve()
GiB = 1024 ** 3

# --- Auto-update -----------------------------------------------------------
# gui.py never imports compress_library internals (see module docstring), so
# the version is read from the script text via regex rather than importing
# it - this keeps the two processes fully decoupled while still letting the
# GUI compare its own bundled core against what's on GitHub.
UPDATE_REPO = "The-Code-Labz/compress-library"
UPDATE_BRANCH = "main"
RAW_BASE = f"https://raw.githubusercontent.com/{UPDATE_REPO}/{UPDATE_BRANCH}"
VERSION_RE = re.compile(r'__version__\s*=\s*"([\d.]+)"')


def _parse_version(text: str) -> tuple[int, ...] | None:
    m = VERSION_RE.search(text)
    if not m:
        return None
    return tuple(int(x) for x in m.group(1).split("."))


def _local_version() -> str | None:
    try:
        text = SCRIPT_PATH.read_text(encoding="utf-8")
    except OSError:
        return None
    m = VERSION_RE.search(text)
    return m.group(1) if m else None


def _fetch_remote(name: str, timeout: int = 10) -> str:
    req = urllib.request.Request(
        f"{RAW_BASE}/{name}",
        headers={"User-Agent": "compress-library-gui-updater"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")

ENCODE_RE = re.compile(r"ENCODE \[(?P<enc>[\w_]+)\] (?P<path>.+?) \(")
DONE_RE = re.compile(r"\bDONE (?P<path>.+?): .*\((?P<pct>[\d.]+)% of original\)")
FAIL_RE = re.compile(r"\bFAIL \((?P<stage>\w+)\) (?P<path>.+?): (?P<reason>.+)")
SKIP_LOCKED_RE = re.compile(r"SKIP \(locked\) (?P<path>.+)")
SKIP_HEVC_RE = re.compile(r"SKIP \(already (?:H\.265|AV1)\) (?P<path>.+)")
SCAN_RE = re.compile(r"Scan: (?P<n>\d+) candidates")
BATCH_DONE_RE = re.compile(r"Batch complete: (?P<summary>.+)")

DRY_ROW_RE = re.compile(
    r"^\s{2}(?P<path>.+?)\s+\[(?P<size>[\d.]+) GiB, codec=(?P<codec>\S+)"
    r"(?:, interlaced=(?P<interlaced>yes|no))?(?:, est\. out ~(?P<est>[\d.]+) GiB)?\]$"
)
DRY_SKIP_RE = re.compile(r"^\s{2}SKIP \(already (?:H\.265|AV1)\) (?P<path>.+?)\s+\[(?P<size>[\d.]+) GiB, codec=(?P<codec>\S+)\]$")
DRY_RECOMPRESS_PREFIX_RE = re.compile(r"^\s{2}RECOMPRESS \(oversized (?:H\.265|AV1)\) ")

PROC_DONE = "\x00PROC_DONE\x00"


class CompressLibraryGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("compress-library — Control Panel")
        root.geometry("1080x760")

        self.proc: subprocess.Popen | None = None
        self.mode = None  # "run" | "dry-run" | "preflight"
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.dry_run_buffer: list[str] = []
        self.stats = {"done": 0, "failed": 0, "locked": 0, "skipped-hevc": 0}
        self.total_candidates = 0
        self.start_time = 0.0

        self._build_menu()
        self._build_widgets()
        self.root.after(120, self._poll_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(2000, lambda: self._check_for_updates(silent=True))

    # ------------------------------------------------------------------
    # Menu / auto-update
    # ------------------------------------------------------------------
    def _build_menu(self):
        menubar = tk.Menu(self.root)
        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="Check for Updates…",
                              command=lambda: self._check_for_updates(silent=False))
        help_menu.add_separator()
        help_menu.add_command(label="About", command=self._show_about)
        menubar.add_cascade(label="Help", menu=help_menu)
        self.root.config(menu=menubar)

    def _show_about(self):
        v = _local_version() or "unknown"
        messagebox.showinfo("compress-library",
                            f"compress-library GUI\nCore version: {v}\n"
                            f"Repo: https://github.com/{UPDATE_REPO}")

    def _check_for_updates(self, silent: bool):
        threading.Thread(target=self._check_for_updates_worker, args=(silent,), daemon=True).start()

    def _check_for_updates_worker(self, silent: bool):
        local_v = _local_version()
        try:
            remote_text = _fetch_remote("compress_library.py")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if not silent:
                self.root.after(0, lambda: messagebox.showerror(
                    "compress-library", f"Update check failed: {exc}"))
            return
        remote_v = _parse_version(remote_text)
        local_tuple = tuple(int(x) for x in local_v.split(".")) if local_v else None
        if remote_v is None:
            if not silent:
                self.root.after(0, lambda: messagebox.showerror(
                    "compress-library", "Update check failed: couldn't read remote version."))
            return
        remote_str = ".".join(str(x) for x in remote_v)
        if local_tuple is not None and remote_v <= local_tuple:
            if not silent:
                self.root.after(0, lambda: messagebox.showinfo(
                    "compress-library", f"Up to date (v{local_v})."))
            return
        self.root.after(0, lambda: self._offer_update(local_v or "unknown", remote_str, remote_text))

    def _offer_update(self, local_v: str, remote_v: str, remote_core_text: str):
        if not messagebox.askyesno(
            "compress-library — Update available",
            f"A newer version is available: v{remote_v} (you have v{local_v}).\n\n"
            f"Download and install it now? Current files will be backed up, "
            f"and you'll need to restart the GUI to run the new code."
        ):
            return
        threading.Thread(target=self._do_update, args=(remote_core_text,), daemon=True).start()

    def _do_update(self, remote_core_text: str):
        try:
            remote_gui_text = _fetch_remote("gui.py")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self.root.after(0, lambda: messagebox.showerror(
                "compress-library", f"Update download failed: {exc}"))
            return
        if "def main(" not in remote_core_text or "def main(" not in remote_gui_text:
            self.root.after(0, lambda: messagebox.showerror(
                "compress-library", "Update aborted: downloaded files failed a sanity check."))
            return
        ts = time.strftime("%Y%m%d-%H%M%S")
        try:
            for path, text in ((SCRIPT_PATH, remote_core_text), (GUI_PATH, remote_gui_text)):
                backup = path.with_name(f"{path.name}.bak-{ts}")
                backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
                tmp = path.with_suffix(path.suffix + ".new")
                tmp.write_text(text, encoding="utf-8")
                os.replace(tmp, path)
        except OSError as exc:
            self.root.after(0, lambda: messagebox.showerror(
                "compress-library", f"Update install failed: {exc}"))
            return
        self.root.after(0, self._update_installed)

    def _update_installed(self):
        if messagebox.askyesno(
            "compress-library", "Update installed. Restart the GUI now to apply it?"
        ):
            self.root.destroy()
            os.execv(sys.executable, [sys.executable, str(GUI_PATH), *sys.argv[1:]])

    # ------------------------------------------------------------------
    # Widget construction
    # ------------------------------------------------------------------
    def _build_widgets(self):
        pad = {"padx": 6, "pady": 4}

        lib = ttk.LabelFrame(self.root, text="Library")
        lib.pack(fill="x", **pad)
        self.var_root = tk.StringVar()
        ttk.Entry(lib, textvariable=self.var_root).pack(side="left", fill="x", expand=True, padx=(6, 0), pady=6)
        ttk.Button(lib, text="Browse…", command=self._browse_root).pack(side="left", padx=6, pady=6)

        opts = ttk.LabelFrame(self.root, text="Options")
        opts.pack(fill="x", **pad)
        for c in range(4):
            opts.columnconfigure(c, weight=1)

        self.var_min_size = tk.DoubleVar(value=2.0)
        self.var_quality = tk.DoubleVar(value=25.0)
        self.var_qsv = tk.BooleanVar(value=True)
        self.var_nvenc = tk.BooleanVar(value=True)
        self.var_qsv_av1 = tk.BooleanVar(value=False)
        self.var_gpu_assign = tk.StringVar(value="0,1")
        self.var_temp_dir = tk.StringVar()
        self.var_duration_tol = tk.DoubleVar(value=2.0)
        self.var_timeout_hours = tk.DoubleVar(value=6.0)
        self.var_resume = tk.BooleanVar(value=True)
        self.var_manifest = tk.StringVar()
        self.var_log = tk.StringVar()
        self.var_config = tk.StringVar()
        self.var_hb_bin = tk.StringVar(value="HandBrakeCLI")
        self.var_preset = tk.StringVar()
        self.var_interlace_mode = tk.StringVar(value="auto")
        self.var_recompress_hevc_over = tk.DoubleVar(value=0.0)

        r = 0
        self._field(opts, r, 0, "Min size (GB)", ttk.Spinbox(opts, textvariable=self.var_min_size, from_=0, to=1000, increment=0.5, width=10))
        self._field(opts, r, 2, "Quality (RF)", ttk.Spinbox(opts, textvariable=self.var_quality, from_=10, to=51, increment=1, width=10))
        r += 1
        encf = ttk.Frame(opts)
        ttk.Label(opts, text="Encoders").grid(row=r, column=0, sticky="w", padx=6)
        encf.grid(row=r, column=1, sticky="w")
        ttk.Checkbutton(encf, text="qsv_h265 (GPU0)", variable=self.var_qsv).pack(side="left")
        ttk.Checkbutton(encf, text="nvenc_h265 (GPU1)", variable=self.var_nvenc).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(encf, text="qsv_av1 (Arc, replaces qsv_h265)", variable=self.var_qsv_av1).pack(side="left", padx=(8, 0))
        self._field(opts, r, 2, "GPU assign", ttk.Entry(opts, textvariable=self.var_gpu_assign, width=12))
        r += 1
        self._field_browse(opts, r, 0, "Temp dir", self.var_temp_dir, dir_only=True)
        self._field(opts, r, 2, "Duration tol. (s)", ttk.Spinbox(opts, textvariable=self.var_duration_tol, from_=0, to=60, increment=0.5, width=10))
        r += 1
        self._field(opts, r, 0, "Timeout (hours)", ttk.Spinbox(opts, textvariable=self.var_timeout_hours, from_=0.5, to=48, increment=0.5, width=10))
        ttk.Checkbutton(opts, text="Resume (skip files already done)", variable=self.var_resume).grid(row=r, column=2, columnspan=2, sticky="w", padx=6)
        r += 1
        self._field_browse(opts, r, 0, "Manifest DB", self.var_manifest, save=True, pattern="*.db")
        self._field_browse(opts, r, 2, "Log file", self.var_log, save=True, pattern="*.log")
        r += 1
        self._field_browse(opts, r, 0, "Config JSON", self.var_config, save=True, pattern="*.json")
        self._field_browse(opts, r, 2, "HandBrakeCLI path", self.var_hb_bin, file_only=True)
        r += 1
        self._field_browse(opts, r, 0, "Preset JSON (optional)", self.var_preset, save=True, pattern="*.json")
        self._field(opts, r, 2, "Interlace mode",
                    ttk.Combobox(opts, textvariable=self.var_interlace_mode,
                                 values=("auto", "force", "off"), state="readonly", width=10))
        r += 1
        self._field(opts, r, 0, "Recompress HEVC over (GB)",
                    ttk.Spinbox(opts, textvariable=self.var_recompress_hevc_over,
                                from_=0, to=1000, increment=0.5, width=10))
        r += 1
        ttk.Label(opts, text="Extra HandBrakeCLI args\n(one per line)").grid(row=r, column=0, sticky="nw", padx=6, pady=4)
        self.txt_extra_args = tk.Text(opts, height=3, width=50)
        self.txt_extra_args.grid(row=r, column=1, columnspan=3, sticky="ew", padx=6, pady=4)

        btns = ttk.Frame(self.root)
        btns.pack(fill="x", **pad)
        ttk.Button(btns, text="Preflight", command=self.preflight).pack(side="left", padx=4)
        ttk.Button(btns, text="Dry Run", command=self.dry_run).pack(side="left", padx=4)
        self.btn_start = ttk.Button(btns, text="▶ Start", command=self.start)
        self.btn_start.pack(side="left", padx=4)
        self.btn_stop = ttk.Button(btns, text="■ Stop", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=4)

        status = ttk.Frame(self.root)
        status.pack(fill="x", **pad)
        self.progress = ttk.Progressbar(status, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.var_status = tk.StringVar(value="Idle")
        ttk.Label(status, textvariable=self.var_status, width=60).pack(side="left")

        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, **pad)

        # --- Log tab ---
        log_tab = ttk.Frame(nb)
        nb.add(log_tab, text="Log")
        top = ttk.Frame(log_tab)
        top.pack(fill="x")
        self.var_autoscroll = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="Autoscroll", variable=self.var_autoscroll).pack(side="left", padx=4)
        ttk.Button(top, text="Clear", command=lambda: self.txt_log.delete("1.0", "end")).pack(side="left", padx=4)
        self.txt_log = tk.Text(log_tab, wrap="none", state="normal")
        self.txt_log.pack(fill="both", expand=True)

        # --- Dry-run preview tab ---
        preview_tab = ttk.Frame(nb)
        nb.add(preview_tab, text="Dry-Run Preview")
        cols = ("path", "size", "codec", "interlaced", "status", "est_out")
        self.tree_preview = ttk.Treeview(preview_tab, columns=cols, show="headings")
        for c, w in zip(cols, (480, 90, 90, 80, 130, 100)):
            self.tree_preview.heading(c, text=c.replace("_", " ").title())
            self.tree_preview.column(c, width=w, anchor="w")
        self.tree_preview.pack(fill="both", expand=True)
        self.var_preview_summary = tk.StringVar(value="")
        ttk.Label(preview_tab, textvariable=self.var_preview_summary).pack(fill="x")

        # --- Manifest tab ---
        manifest_tab = ttk.Frame(nb)
        nb.add(manifest_tab, text="Manifest")
        mtop = ttk.Frame(manifest_tab)
        mtop.pack(fill="x")
        ttk.Button(mtop, text="Refresh", command=self.refresh_manifest).pack(side="left", padx=4)
        self.var_auto_refresh = tk.BooleanVar(value=True)
        ttk.Checkbutton(mtop, text="Auto-refresh while running", variable=self.var_auto_refresh).pack(side="left", padx=4)
        mcols = ("path", "status", "encoder", "in_gib", "out_gib", "ratio", "attempts", "error")
        self.tree_manifest = ttk.Treeview(manifest_tab, columns=mcols, show="headings")
        widths = (420, 90, 90, 80, 80, 70, 70, 220)
        for c, w in zip(mcols, widths):
            self.tree_manifest.heading(c, text=c.replace("_", " ").title())
            self.tree_manifest.column(c, width=w, anchor="w")
        self.tree_manifest.pack(fill="both", expand=True)

        # --- Config tab ---
        config_tab = ttk.Frame(nb)
        nb.add(config_tab, text="Config")
        ctop = ttk.Frame(config_tab)
        ctop.pack(fill="x")
        ttk.Button(ctop, text="Load", command=self._load_config_text).pack(side="left", padx=4)
        ttk.Button(ctop, text="Save", command=self._save_config_text).pack(side="left", padx=4)
        ttk.Label(ctop, text="(estimate_ratio + encoder_blacklist; see config.example.json)").pack(side="left", padx=8)
        self.txt_config = tk.Text(config_tab, wrap="none")
        self.txt_config.pack(fill="both", expand=True)
        self._load_config_text()

    def _field(self, parent, row, col, label, widget):
        ttk.Label(parent, text=label).grid(row=row, column=col, sticky="w", padx=6, pady=4)
        widget.grid(row=row, column=col + 1, sticky="w", padx=6, pady=4)

    def _field_browse(self, parent, row, col, label, var, dir_only=False, file_only=False, save=False, pattern="*"):
        ttk.Label(parent, text=label).grid(row=row, column=col, sticky="w", padx=6, pady=4)
        f = ttk.Frame(parent)
        f.grid(row=row, column=col + 1, sticky="ew", padx=6, pady=4)
        ttk.Entry(f, textvariable=var, width=28).pack(side="left", fill="x", expand=True)

        def browse():
            if dir_only:
                p = filedialog.askdirectory()
            elif save:
                p = filedialog.asksaveasfilename(defaultextension=pattern.replace("*", ""), filetypes=[(pattern, pattern)])
            else:
                p = filedialog.askopenfilename(filetypes=[(pattern, pattern), ("All files", "*")])
            if p:
                var.set(p)

        ttk.Button(f, text="…", width=3, command=browse).pack(side="left")

    def _browse_root(self):
        p = filedialog.askdirectory()
        if p:
            self.var_root.set(p)

    # ------------------------------------------------------------------
    # Argument building
    # ------------------------------------------------------------------
    def _build_args(self, dry_run: bool) -> list[str] | None:
        root = self.var_root.get().strip()
        if not root or not Path(root).is_dir():
            messagebox.showerror("compress-library", "Choose a valid library root directory first.")
            return None
        encoders = []
        if self.var_qsv_av1.get():
            encoders.append("qsv_av1")  # replaces qsv_h265 on the same QSV/Arc lane
        elif self.var_qsv.get():
            encoders.append("qsv_h265")
        if self.var_nvenc.get():
            encoders.append("nvenc_h265")
        if not encoders:
            messagebox.showerror("compress-library", "Select at least one encoder.")
            return None

        args = [root]
        if dry_run:
            args.append("--dry-run")
        args += ["--min-size", str(self.var_min_size.get())]
        if self.var_recompress_hevc_over.get() > 0:
            args += ["--recompress-hevc-over", str(self.var_recompress_hevc_over.get())]
        args += ["--quality", str(self.var_quality.get())]
        args += ["--encoder", *encoders]
        args += ["--gpu-assign", self.var_gpu_assign.get().strip() or "0,1"]
        if self.var_temp_dir.get().strip():
            args += ["--temp-dir", self.var_temp_dir.get().strip()]
        args += ["--duration-tolerance", str(self.var_duration_tol.get())]
        args += ["--timeout", str(int(self.var_timeout_hours.get() * 3600))]
        if self.var_manifest.get().strip():
            args += ["--manifest", self.var_manifest.get().strip()]
        if self.var_log.get().strip():
            args += ["--log", self.var_log.get().strip()]
        if self.var_config.get().strip():
            args += ["--config", self.var_config.get().strip()]
        if self.var_hb_bin.get().strip():
            args += ["--handbrake-cli", self.var_hb_bin.get().strip()]
        if self.var_preset.get().strip():
            args += ["--preset", self.var_preset.get().strip()]
        args += ["--interlace-mode", self.var_interlace_mode.get() or "auto"]
        for line in self.txt_extra_args.get("1.0", "end").splitlines():
            line = line.strip()
            if line:
                args.append(f"--extra-arg={line}")
        if not self.var_resume.get():
            args.append("--no-resume")
        return args

    def _manifest_path(self) -> Path:
        p = self.var_manifest.get().strip()
        return Path(p) if p else (TOOL_DIR / "manifest.db")

    # ------------------------------------------------------------------
    # Process control
    # ------------------------------------------------------------------
    def _set_running(self, running: bool):
        self.btn_start.config(state="disabled" if running else "normal")
        self.btn_stop.config(state="normal" if running else "disabled")

    def start(self):
        if self.proc is not None:
            return
        args = self._build_args(dry_run=False)
        if args is None:
            return
        self.stats = {"done": 0, "failed": 0, "locked": 0, "skipped-hevc": 0}
        self.total_candidates = 0
        self.progress.config(value=0, maximum=100)
        self.mode = "run"
        self._launch(["run", *args])

    def dry_run(self):
        if self.proc is not None:
            return
        args = self._build_args(dry_run=True)
        if args is None:
            return
        self.dry_run_buffer = []
        for i in self.tree_preview.get_children():
            self.tree_preview.delete(i)
        self.mode = "dry-run"
        self._launch(["run", *args])

    def preflight(self):
        if self.proc is not None:
            return
        self.mode = "preflight"
        self._launch(["preflight"])

    def _launch(self, argv: list[str]):
        cmd = [sys.executable, str(SCRIPT_PATH), *argv]
        self.log_queue.put(f"\n$ {' '.join(cmd)}\n")
        self._set_running(True)
        self.var_status.set(f"Running ({self.mode})…")
        self.start_time = time.time()
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=str(TOOL_DIR), **kwargs,
        )
        threading.Thread(target=self._reader_thread, args=(self.proc,), daemon=True).start()

    def _reader_thread(self, proc: subprocess.Popen):
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                self.log_queue.put(line)
        except (OSError, ValueError):
            pass
        rc = proc.wait()
        self.log_queue.put(f"{PROC_DONE}{rc}\n")

    def stop(self):
        if self.proc is None:
            return
        self.log_queue.put("\n[stop requested by user]\n")
        pid = self.proc.pid
        if psutil is not None:
            try:
                parent = psutil.Process(pid)
                children = parent.children(recursive=True)
                for c in children:
                    try:
                        c.terminate()
                    except psutil.Error:
                        pass
                try:
                    parent.terminate()
                except psutil.Error:
                    pass
                _, alive = psutil.wait_procs(children + [parent], timeout=5)
                for p in alive:
                    try:
                        p.kill()
                    except psutil.Error:
                        pass
            except psutil.Error:
                self.proc.terminate()
        else:
            self.proc.terminate()
            self.log_queue.put("[psutil not installed: HandBrakeCLI child process may still be running]\n")

    def _on_close(self):
        if self.proc is not None:
            self.stop()
        self.root.after(300, self.root.destroy)

    # ------------------------------------------------------------------
    # Log queue / stats
    # ------------------------------------------------------------------
    def _poll_queue(self):
        try:
            while True:
                line = self.log_queue.get_nowait()
                if line.startswith(PROC_DONE):
                    rc = line[len(PROC_DONE):].strip()
                    self._on_process_done(rc)
                    continue
                self._append_log(line)
                if self.mode == "run":
                    self._parse_stats_line(line)
                elif self.mode == "dry-run":
                    self.dry_run_buffer.append(line)
        except queue.Empty:
            pass
        self.root.after(120, self._poll_queue)

    def _append_log(self, line: str):
        self.txt_log.insert("end", line)
        if self.var_autoscroll.get():
            self.txt_log.see("end")

    def _on_process_done(self, rc: str):
        self._set_running(False)
        elapsed = time.time() - self.start_time
        self.var_status.set(f"Finished ({self.mode}, exit={rc}, {elapsed:.0f}s)")
        if self.mode == "dry-run":
            self._parse_dry_run_output("".join(self.dry_run_buffer))
        elif self.mode == "run":
            self.refresh_manifest()
        self.proc = None
        self.mode = None

    def _parse_stats_line(self, line: str):
        m = SCAN_RE.search(line)
        if m:
            self.total_candidates = int(m.group("n"))
            self.progress.config(maximum=max(self.total_candidates, 1))
        elif DONE_RE.search(line):
            self.stats["done"] += 1
        elif FAIL_RE.search(line):
            self.stats["failed"] += 1
        elif SKIP_LOCKED_RE.search(line):
            self.stats["locked"] += 1
        elif SKIP_HEVC_RE.search(line):
            self.stats["skipped-hevc"] += 1
        else:
            return
        finished = sum(self.stats.values())
        self.progress.config(value=finished)
        self.var_status.set(
            f"Running: done={self.stats['done']} failed={self.stats['failed']} "
            f"locked={self.stats['locked']} skipped-hevc={self.stats['skipped-hevc']} "
            f"({finished}/{self.total_candidates or '?'})"
        )
        if self.var_auto_refresh.get():
            self.refresh_manifest()

    def _parse_dry_run_output(self, text: str):
        total_in = total_est = 0.0
        skip_count = enc_count = 0
        for line in text.splitlines():
            m = DRY_SKIP_RE.match(line)
            if m:
                skip_count += 1
                self.tree_preview.insert("", "end", values=(
                    m.group("path"), m.group("size"), m.group("codec"), "-",
                    f"already {m.group('codec')}", "-"))
                continue
            recompress = bool(DRY_RECOMPRESS_PREFIX_RE.match(line))
            line_for_row = DRY_RECOMPRESS_PREFIX_RE.sub("  ", line) if recompress else line
            m = DRY_ROW_RE.match(line_for_row)
            if m:
                size = float(m.group("size"))
                est = float(m.group("est")) if m.group("est") else 0.0
                total_in += size
                total_est += est
                enc_count += 1
                self.tree_preview.insert("", "end", values=(
                    m.group("path"), f"{size:.2f}", m.group("codec"),
                    m.group("interlaced") or "-",
                    (f"recompress (oversized {m.group('codec')})" if recompress else "to encode"),
                    f"{est:.2f}"))
        self.var_preview_summary.set(
            f"{enc_count} to encode ({total_in:.2f} GiB -> ~{total_est:.2f} GiB est.), "
            f"{skip_count} already-compressed skipped"
        )

    # ------------------------------------------------------------------
    # Manifest
    # ------------------------------------------------------------------
    def refresh_manifest(self):
        path = self._manifest_path()
        for i in self.tree_manifest.get_children():
            self.tree_manifest.delete(i)
        if not path.is_file():
            return
        try:
            uri = f"file:{path}?mode=ro"
            con = sqlite3.connect(uri, uri=True, timeout=2)
            rows = con.execute(
                "SELECT path,status,encoder,input_size,output_size,ratio,attempts,error "
                "FROM files ORDER BY updated_at DESC LIMIT 2000"
            ).fetchall()
            con.close()
        except sqlite3.Error as exc:
            self.log_queue.put(f"[manifest read error: {exc}]\n")
            return
        for path_, status, encoder, in_sz, out_sz, ratio, attempts, error in rows:
            in_gib = f"{in_sz / GiB:.2f}" if in_sz else "-"
            out_gib = f"{out_sz / GiB:.2f}" if out_sz else "-"
            ratio_pct = f"{ratio * 100:.1f}%" if ratio else "-"
            self.tree_manifest.insert("", "end", values=(
                path_, status, encoder or "-", in_gib, out_gib, ratio_pct, attempts, error or ""))

    # ------------------------------------------------------------------
    # Config editor
    # ------------------------------------------------------------------
    def _config_target_path(self) -> Path:
        p = self.var_config.get().strip()
        if p:
            return Path(p)
        cfg = TOOL_DIR / "config.json"
        return cfg if cfg.is_file() else (TOOL_DIR / "config.example.json")

    def _load_config_text(self):
        path = self._config_target_path()
        self.txt_config.delete("1.0", "end")
        if path.is_file():
            self.txt_config.insert("1.0", path.read_text(encoding="utf-8"))
        else:
            self.txt_config.insert("1.0", "{\n  \"estimate_ratio\": 0.45,\n  \"encoder_blacklist\": {}\n}\n")

    def _save_config_text(self):
        text = self.txt_config.get("1.0", "end")
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            messagebox.showerror("compress-library", f"Invalid JSON: {exc}")
            return
        target = Path(self.var_config.get().strip() or (TOOL_DIR / "config.json"))
        target.write_text(text, encoding="utf-8")
        messagebox.showinfo("compress-library", f"Saved {target}")


def main():
    if not SCRIPT_PATH.is_file():
        print(f"compress_library.py not found next to gui.py ({SCRIPT_PATH})", file=sys.stderr)
        return 2
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except tk.TclError:
        pass
    CompressLibraryGUI(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
