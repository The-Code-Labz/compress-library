#!/usr/bin/env python3
"""
compress-library - H.265 media library compressor.

Target hardware: Windows 11, i9-13900K / 128GB, Intel Arc A580 (QSV) + RTX 3070 (NVENC).
Two encodes run in parallel (one per GPU) at below-normal process priority.

Safety model (non-negotiable):
  - Source files are never touched until verification passes.
  - Failed or oversized encodes delete their temp file and move on.
  - Every action is logged with sizes, ratio, encoder, and status.
  - A SQLite manifest tracks done/failed/in-progress so --resume survives crashes.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    import psutil  # optional at import; required for process priority / lock checks
except ImportError:  # pragma: no cover
    psutil = None

VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".ts"}
HEVC_CODEC_NAMES = {"hevc"}          # ffprobe codec_name values meaning "already H.265"
HEVC_TAGS = {"hvc1", "hev1"}         # mp4 codec_tag fallbacks
GiB = 1024 ** 3

TOOL_DIR = Path(__file__).resolve().parent
DEFAULT_MANIFEST = TOOL_DIR / "manifest.db"
DEFAULT_LOG = TOOL_DIR / "compress-library.log"
DEFAULT_CONFIG = TOOL_DIR / "config.json"

log = logging.getLogger("compress-library")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path: str | None) -> dict:
    cfg: dict = {}
    if path and Path(path).is_file():
        cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    elif DEFAULT_CONFIG.is_file():
        cfg = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    return cfg


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------

def ffprobe_json(path: Path) -> dict | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-show_streams",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=120,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return None
        return json.loads(out.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return None


def stream_info(path: Path) -> dict | None:
    """Return duration_sec, audio, subtitles, video_codec for a media file."""
    data = ffprobe_json(path)
    if not data:
        return None
    streams = data.get("streams", [])
    audio = sum(1 for s in streams if s.get("codec_type") == "audio")
    subtitles = sum(1 for s in streams if s.get("codec_type") == "subtitle")
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    duration = 0.0
    try:
        duration = float(data.get("format", {}).get("duration", 0) or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        cand = [s.get("duration") for s in streams if s.get("duration")]
        if cand:
            try:
                duration = max(float(d) for d in cand)
            except (TypeError, ValueError):
                duration = 0.0
    return {
        "duration": duration,
        "audio": audio,
        "subtitles": subtitles,
        "video_codec": (video.get("codec_name") or "").lower(),
        "codec_tag": (video.get("codec_tag_string") or "").lower(),
    }


def is_h265(info: dict) -> bool:
    return info["video_codec"] in HEVC_CODEC_NAMES or info["codec_tag"] in HEVC_TAGS


# ---------------------------------------------------------------------------
# Manifest (SQLite)
# ---------------------------------------------------------------------------

class Manifest:
    def __init__(self, path: Path):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS files (
                   path TEXT PRIMARY KEY,
                   size INTEGER,
                   status TEXT,
                   encoder TEXT,
                   input_size INTEGER,
                   output_size INTEGER,
                   ratio REAL,
                   error TEXT,
                   attempts INTEGER DEFAULT 0,
                   updated_at REAL
               )"""
        )
        self._db.commit()

    def _exec(self, sql, params=()):
        with self._lock:
            self._db.execute(sql, params)
            self._db.commit()

    def get(self, path: str):
        with self._lock:
            row = self._db.execute(
                "SELECT path,size,status,encoder,input_size,output_size,ratio,error,attempts "
                "FROM files WHERE path=?", (path,)).fetchone()
        if not row:
            return None
        keys = ("path", "size", "status", "encoder", "input_size",
                "output_size", "ratio", "error", "attempts")
        return dict(zip(keys, row))

    def all(self):
        with self._lock:
            return self._db.execute(
                "SELECT path,size,status,encoder,input_size,output_size,ratio,error,attempts "
                "FROM files ORDER BY path").fetchall()

    def set_status(self, path: str, size: int, status: str, encoder: str | None = None,
                   error: str | None = None, **fields):
        cols = {"size": size, "status": status, "encoder": encoder,
                "error": error, "updated_at": time.time()}
        cols.update(fields)  # e.g. input_size, output_size, ratio
        existing = self.get(path)
        attempts = existing["attempts"] if existing else 0
        if status in ("encoding", "failed"):
            attempts += 1
        cols["attempts"] = attempts
        keys = list(cols.keys())
        insert_cols = ["path"] + keys
        placeholders = ",".join("?" * len(insert_cols))
        updates = ",".join(f"{k}=excluded.{k}" for k in keys)
        sql = (f"INSERT INTO files ({','.join(insert_cols)}) VALUES ({placeholders}) "
               f"ON CONFLICT(path) DO UPDATE SET {updates}")
        self._exec(sql, [path] + [cols[k] for k in keys])

    def reset_interrupted(self):
        with self._lock:
            self._db.execute(
                "UPDATE files SET status='pending' WHERE status='encoding'")
            self._db.commit()

    def close(self):
        with self._lock:
            self._db.close()


# ---------------------------------------------------------------------------
# Filesystem safety
# ---------------------------------------------------------------------------

def is_locked(path: Path) -> bool:
    """Probe whether another process (Plex/Jellyfin/mpv) holds the file open.

    Windows: an open-without-sharing file cannot be renamed; POSIX: fall back
    to an exclusive-mode open.
    """
    if os.name == "nt":
        try:
            os.rename(path, path)  # no-op rename probe
            return False
        except PermissionError:
            return True
        except OSError:
            return False
    try:
        fd = os.open(path, os.O_RDWR | os.O_EXCL)
        os.close(fd)
        return False
    except OSError:
        return True


def atomic_replace(src_tmp: Path, original: Path):
    """Replace the original file with the verified encode.

    Same filesystem: os.replace is atomic. Cross-filesystem: copy to
    original's directory + os.replace + unlink source.
    """
    if src_tmp.parent == original.parent:
        os.replace(src_tmp, original)
        return
    staging = original.with_name(original.name + ".compress-library-tmp")
    try:
        shutil.copy2(src_tmp, staging)
        os.replace(staging, original)
    finally:
        staging.unlink(missing_ok=True)
        src_tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------

def set_low_priority(proc: subprocess.Popen):
    """Drop the HandBrakeCLI child to below-normal priority (nice on POSIX)."""
    if psutil is None:
        return
    try:
        p = psutil.Process(proc.pid)
        if os.name == "nt":
            p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        else:
            p.nice(10)
    except (psutil.Error, OSError):
        pass  # priority is best-effort; never fail an encode over it


def handbrake_encode(src: Path, dst: Path, encoder: str, quality: float,
                     preset_import: str | None, extra_args: list[str],
                     hb_bin: str, timeout: int) -> None:
    args = [
        hb_bin,
        "-i", str(src),
        "-o", str(dst),
        "--preset", "Fast 1080p30",  # baseline; --encoder/--quality/--all-* override
        "--encoder", encoder,
        "--quality", str(quality),
        "--all-audio",
        "--aencoder", "copy",
        "--audio-copy-mask", "aac,ac3,eac3,dts,dtshd,truehd,mp3,flac,opus",
        "--audio-fallback", "ffac3",
        "--all-subtitles",
        "--subtitle-burned", "none",
        "--optimize",
    ]
    if preset_import:
        args += ["--preset-import-file", preset_import]
    args += extra_args

    log.debug("HandBrakeCLI: %s", " ".join(args))
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    set_low_priority(proc)
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise RuntimeError(f"encode timed out after {timeout}s")
    if rc != 0:
        raise RuntimeError(f"HandBrakeCLI exited with code {rc}")


def verify(src: Path, dst: Path, tolerance: float) -> list[str]:
    """Return a list of verification failures (empty = pass)."""
    problems = []
    src_info = stream_info(src)
    dst_info = stream_info(dst)
    if not dst_info:
        return ["output unreadable by ffprobe"]
    if not src_info:
        return ["input unreadable by ffprobe (cannot verify; NOT replacing)"]
    if dst_info["audio"] < src_info["audio"]:
        problems.append(
            f"audio tracks {dst_info['audio']} < input {src_info['audio']}")
    if dst_info["subtitles"] < src_info["subtitles"]:
        problems.append(
            f"subtitle tracks {dst_info['subtitles']} < input {src_info['subtitles']}")
    if src_info["duration"] > 0 and dst_info["duration"] > 0:
        if abs(src_info["duration"] - dst_info["duration"]) > tolerance:
            problems.append(
                f"duration mismatch {src_info['duration']:.1f}s vs {dst_info['duration']:.1f}s")
    if dst.stat().st_size >= src.stat().st_size:
        problems.append(
            f"output not smaller ({dst.stat().st_size} >= {src.stat().st_size})")
    return problems


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------

def scan(root: Path, min_bytes: int, manifest: Manifest, resume: bool,
         encoder_blacklist: dict[str, list[str]]) -> list[Path]:
    """Find encode candidates. Returns sorted list of absolute paths."""
    candidates: list[Path] = []
    skipped_done = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix.lower() not in VIDEO_EXTS:
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size < min_bytes:
                continue
            ext_blacklist = encoder_blacklist.get(p.suffix.lower(), [])
            if ext_blacklist and all(e in ext_blacklist for e in ("qsv_h265", "nvenc_h265")):
                log.info("SKIP (extension blacklisted) %s", p)
                continue
            rec = manifest.get(str(p))
            if rec and rec["status"] == "done" and resume:
                skipped_done += 1
                continue
            candidates.append(p)
    candidates.sort()
    log.info("Scan: %d candidates (%d already done, skipped)", len(candidates), skipped_done)
    return candidates


def process_file(src: Path, args, encoders: list[str], manifest: Manifest,
                 worker_idx: int) -> str:
    encoder = encoders[worker_idx % len(encoders)]
    size = src.stat().st_size

    # 1. Locked by Plex/Jellyfin/etc?
    if is_locked(src):
        log.warning("SKIP (locked) %s", src)
        manifest.set_status(str(src), size, "locked")
        return "locked"

    # 2. Already H.265?
    info = stream_info(src)
    if info and is_h265(info):
        log.info("SKIP (already H.265) %s", src)
        manifest.set_status(str(src), size, "skipped-hevc")
        return "skipped-hevc"

    # 3. Encode to temp dir
    tmp_dir: Path = Path(args.temp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    dst = tmp_dir / src.name  # EXACT original filename, in temp dir
    dst.unlink(missing_ok=True)
    manifest.set_status(str(src), size, "encoding", encoder=encoder)
    log.info("ENCODE [%s] %s (%.2f GiB)", encoder, src, size / GiB)
    t0 = time.time()
    try:
        handbrake_encode(src, dst, encoder, args.quality, args.preset,
                         args.extra_arg or [], args.handbrake_cli,
                         args.timeout)
    except Exception as exc:  # noqa: BLE001 - any encode failure must not kill the batch
        dst.unlink(missing_ok=True)
        log.error("FAIL (encode) %s: %s", src, exc)
        manifest.set_status(str(src), size, "failed", encoder=encoder, error=str(exc))
        return "failed"

    # 4. Verify before ANY destructive action
    problems = verify(src, dst, args.duration_tolerance)
    if problems:
        dst.unlink(missing_ok=True)
        log.error("FAIL (verify) %s: %s", src, "; ".join(problems))
        manifest.set_status(str(src), size, "failed", encoder=encoder,
                            error="; ".join(problems))
        return "failed"

    # 5. Atomic replace (only after verification passed)
    try:
        atomic_replace(dst, src)
    except OSError as exc:
        dst.unlink(missing_ok=True)
        log.error("FAIL (replace) %s: %s", src, exc)
        manifest.set_status(str(src), size, "failed", encoder=encoder, error=str(exc))
        return "failed"

    elapsed = time.time() - t0
    out_size = src.stat().st_size
    ratio = out_size / size
    log.info("DONE %s: %.2f GiB -> %.2f GiB (%.1f%% of original) in %.0fs",
             src, size / GiB, out_size / GiB, ratio * 100, elapsed)
    manifest.set_status(str(src), size, "done", encoder=encoder,
                        input_size=size, output_size=out_size, ratio=ratio)
    return "done"


def cmd_run(args) -> int:
    if shutil.which(args.handbrake_cli) is None:
        log.error("HandBrakeCLI not found on PATH (set --handbrake-cli or fix PATH)")
        return 2
    if shutil.which("ffprobe") is None:
        log.error("ffprobe not found on PATH (install ffmpeg)")
        return 2

    root = Path(args.root).resolve()
    if not root.is_dir():
        log.error("Root directory does not exist: %s", root)
        return 2

    cfg = load_config(args.config)
    encoders = args.encoder if isinstance(args.encoder, list) else [args.encoder]
    gpu_assign = [int(g) for g in str(args.gpu_assign).split(",")]
    if len(gpu_assign) != len(encoders):
        log.warning("--gpu-assign has %d entries for %d encoders; "
                    "HandBrakeCLI adapter pinning is advisory (see README)",
                    len(gpu_assign), len(encoders))
    encoder_blacklist: dict[str, list[str]] = cfg.get("encoder_blacklist", {})
    est_ratio = float(cfg.get("estimate_ratio", 0.45))

    manifest = Manifest(Path(args.manifest))
    manifest.reset_interrupted()  # crash/reboot recovery

    candidates = scan(root, int(args.min_size * GiB), manifest,
                      resume=args.resume, encoder_blacklist=encoder_blacklist)

    if args.dry_run:
        total = 0
        rows = []
        for p in candidates:
            try:
                size = p.stat().st_size
            except OSError:
                continue
            info = stream_info(p)
            if info and is_h265(info):
                rows.append((p, size, info, True))
                continue
            rows.append((p, size, info, False))
        print(f"\nDRY RUN - {sum(1 for r in rows if not r[3])} file(s) would be encoded "
              f"({sum(1 for r in rows if r[3])} already H.265, skipped):\n")
        for p, size, info, hevc in rows:
            codec = f"{info['video_codec'] or 'unknown'}" if info else "unprobeable"
            if hevc:
                print(f"  SKIP (already H.265) {p}  [{size / GiB:.2f} GiB, codec={codec}]")
                continue
            est = size * est_ratio
            total += size
            print(f"  {p}  [{size / GiB:.2f} GiB, codec={codec}, "
                  f"est. out ~{est / GiB:.2f} GiB]")
        print(f"\nTotal input: {total / GiB:.2f} GiB | "
              f"Rough est. output: {total * est_ratio / GiB:.2f} GiB "
              f"(ratio {est_ratio}, tune via config.json 'estimate_ratio')\n")
        return 0

    if not candidates:
        log.info("Nothing to do.")
        return 0

    stats = {"done": 0, "failed": 0, "locked": 0, "skipped-hevc": 0}
    with ThreadPoolExecutor(max_workers=len(encoders),
                            thread_name_prefix="encoder") as pool:
        futures = {
            pool.submit(process_file, p, args, encoders, manifest, i): p
            for i, p in enumerate(candidates)
        }
        for fut in as_completed(futures):
            result = fut.result()
            stats[result] = stats.get(result, 0) + 1

    manifest.close()
    log.info("Batch complete: %s", ", ".join(f"{k}={v}" for k, v in sorted(stats.items())))
    return 0 if stats.get("failed", 0) == 0 else 1


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

def cmd_preflight(_args) -> int:
    ok = True

    def check(label: str, passed: bool, detail: str = ""):
        nonlocal ok
        ok = ok and passed
        print(f"  [{'OK ' if passed else 'FAIL'}] {label}{(' - ' + detail) if detail else ''}")

    print("compress-library preflight\n")
    hb = shutil.which("HandBrakeCLI")
    ff = shutil.which("ffprobe")
    check("HandBrakeCLI on PATH", hb is not None, hb or "not found")
    check("ffprobe on PATH", ff is not None, ff or "not found")
    check("psutil installed", psutil is not None,
          "required for low priority + lock detection" if psutil is None else "")

    if os.name == "nt":
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name"],
                capture_output=True, text=True, timeout=30)
            gpus = [l.strip() for l in out.stdout.splitlines() if l.strip()]
            print(f"\n  GPUs detected ({len(gpus)}):")
            for g in gpus:
                mark = ""
                if "arc" in g.lower():
                    mark = "  <- expected QSV (GPU 0)"
                elif "nvidia" in g.lower() or "geforce" in g.lower() or "rtx" in g.lower():
                    mark = "  <- expected NVENC (GPU 1)"
                print(f"    - {g}{mark}")
            check("Intel Arc (QSV) present", any("arc" in g.lower() for g in gpus))
            check("NVIDIA (NVENC) present", any("nvidia" in g.lower() or "geforce" in g.lower()
                                                or "rtx" in g.lower() for g in gpus))
        except (subprocess.SubprocessError, OSError) as exc:
            check("GPU enumeration", False, str(exc))
    else:
        print("\n  (non-Windows host: skipping WMI GPU enumeration)")

    tmp = Path("temp").resolve()
    try:
        tmp.mkdir(parents=True, exist_ok=True)
        probe = tmp / ".preflight-write-test"
        probe.write_text("ok")
        probe.unlink()
        check("temp dir writable", True, str(tmp))
    except OSError as exc:
        check("temp dir writable", False, str(exc))

    free = shutil.disk_usage(tmp if tmp.exists() else ".").free
    print(f"\n  Free space on temp volume: {free / GiB:.1f} GiB")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="compress-library",
        description="H.265 media library compressor (HandBrakeCLI + ffprobe, "
                    "dual-GPU: qsv_h265 GPU0 / nvenc_h265 GPU1)")
    sub = p.add_subparsers(dest="command")

    run = sub.add_parser("run", help="encode a library (default command)")
    run.add_argument("root", help="library root directory to scan")
    run.add_argument("--dry-run", action="store_true",
                     help="list what would be encoded; encode nothing")
    run.add_argument("--min-size", type=float, default=2.0,
                     help="skip files smaller than this many GB (default 2)")
    run.add_argument("--quality", type=float, default=25.0,
                     help="HandBrake constant-quality RF (default 25)")
    run.add_argument("--encoder", nargs="+",
                     choices=["qsv_h265", "nvenc_h265"],
                     default=["qsv_h265", "nvenc_h265"],
                     help="encoder(s); pass both for one-encode-per-GPU (default both)")
    run.add_argument("--gpu-assign", default="0,1",
                     help="GPU adapter index per encoder, comma list (default 0,1; "
                          "advisory - see README caveats)")
    run.add_argument("--preset", metavar="FILE",
                     help="HandBrake preset JSON to import (--preset-import-file)")
    run.add_argument("--extra-arg", action="append",
                     help="extra raw HandBrakeCLI arg (repeatable), e.g. "
                          "--extra-arg=--encopts=tune=ssim")
    run.add_argument("--temp-dir", default=str(TOOL_DIR / "temp"),
                     help="SSD temp dir for encodes (default ./temp)")
    run.add_argument("--duration-tolerance", type=float, default=2.0,
                     help="max duration drift in seconds (default 2)")
    run.add_argument("--timeout", type=int, default=6 * 3600,
                     help="per-file encode timeout, seconds (default 21600)")
    run.add_argument("--manifest", default=str(DEFAULT_MANIFEST),
                     help=f"SQLite manifest path (default {DEFAULT_MANIFEST})")
    run.add_argument("--log", dest="logfile", default=str(DEFAULT_LOG),
                     help=f"log file (default {DEFAULT_LOG})")
    run.add_argument("--config", default=str(DEFAULT_CONFIG),
                     help=f"JSON config path (default {DEFAULT_CONFIG})")
    run.add_argument("--handbrake-cli", default="HandBrakeCLI",
                     help="HandBrakeCLI binary name/path if not on PATH")
    run.add_argument("--resume", action="store_true", default=True,
                     help="skip files already marked done in the manifest (default on)")
    run.add_argument("--no-resume", dest="resume", action="store_false")
    run.set_defaults(func=cmd_run)

    pf = sub.add_parser("preflight", help="verify environment before a batch run")
    pf.set_defaults(func=cmd_preflight)
    return p


def setup_logging(logfile: str):
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    fh = logging.FileHandler(logfile, encoding="utf-8")
    fh.setFormatter(fmt)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(ch)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Allow `compress-library <root> ...` without the literal `run` subcommand.
    if argv and argv[0] not in ("run", "preflight", "-h", "--help"):
        argv.insert(0, "run")
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    if args.command == "run":
        setup_logging(args.logfile)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
