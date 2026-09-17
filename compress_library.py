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
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    import psutil  # optional at import; required for process priority / lock checks
except ImportError:  # pragma: no cover
    psutil = None

__version__ = "1.6.0"

VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".ts"}
HEVC_CODEC_NAMES = {"hevc"}          # ffprobe codec_name values meaning "already H.265"
AV1_CODEC_NAMES = {"av1"}            # ffprobe codec_name value meaning "already AV1"
HEVC_TAGS = {"hvc1", "hev1"}         # mp4 codec_tag fallbacks
GiB = 1024 ** 3

# Codecs where ffprobe's `field_order` metadata is frequently absent/"unknown"
# even on genuinely interlaced sources (common with older VC-1/MPEG-2 Blu-ray
# remuxes). For these, an ambiguous field_order triggers a real idet sample
# instead of being assumed progressive.
INTERLACE_PRONE_CODECS = {"vc1", "mpeg2video", "mpeg1video"}
INTERLACED_FIELD_ORDERS = {"tt", "bb", "tb", "bt"}

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
    # eia_608/eia_708 are embedded closed-caption data ffprobe reports as a
    # pseudo "subtitle" stream (common on Blu-ray remuxes). HandBrake's
    # scanner does not enumerate these as selectable subtitle tracks, so
    # --all-subtitles never copies them - counting them here made verify()
    # fail almost every title with an off-by-one "subtitle tracks N < input
    # N+1" false positive, even though nothing real was actually lost.
    _NON_TRACK_SUBTITLE_CODECS = {"eia_608", "eia_708"}
    subtitles = sum(
        1 for s in streams
        if s.get("codec_type") == "subtitle"
        and (s.get("codec_name") or "").lower() not in _NON_TRACK_SUBTITLE_CODECS
    )
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
        "field_order": (video.get("field_order") or "").lower(),
    }


def is_h265(info: dict) -> bool:
    return info["video_codec"] in HEVC_CODEC_NAMES or info["codec_tag"] in HEVC_TAGS


def is_av1(info: dict) -> bool:
    return info["video_codec"] in AV1_CODEC_NAMES


def already_compressed(info: dict) -> bool:
    """HEVC or AV1 - both are "already efficient" codecs where a blanket
    re-encode would be lossy-on-lossy, so both get the same skip treatment."""
    return is_h265(info) or is_av1(info)


def codec_label(info: dict) -> str:
    return "AV1" if is_av1(info) else "H.265"


def detect_interlaced_idet(path: Path, sample_frames: int = 100) -> bool | None:
    """Decode a short sample through ffmpeg's `idet` filter and compare
    TFF/BFF vs progressive frame counts. Returns True/False, or None if
    ffmpeg is unavailable or the sample couldn't be analyzed."""
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-an", "-sn",
             "-i", str(path), "-frames:v", str(sample_frames),
             "-filter:v", "idet", "-f", "null", "-"],
            capture_output=True, text=True, timeout=60,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    # ffmpeg can emit more than one "Multi frame detection" summary (e.g. an
    # early near-zero snapshot before the final tally) - always take the
    # last one, which reflects the full sample.
    matches = re.findall(
        r"Multi frame detection:\s*TFF:\s*(\d+)\s*BFF:\s*(\d+)\s*Progressive:\s*(\d+)",
        out.stderr,
    )
    if not matches:
        return None
    tff, bff, progressive = (int(g) for g in matches[-1])
    if tff + bff + progressive == 0:
        return None
    return (tff + bff) > progressive


def check_interlaced(path: Path, info: dict) -> bool:
    """Best-effort interlace detection: trust explicit field_order metadata
    first (free); only fall back to a real ffmpeg idet sample when the
    codec is known to under-report field_order (vc1/mpeg2/mpeg1)."""
    fo = info.get("field_order", "")
    if fo in INTERLACED_FIELD_ORDERS:
        return True
    if fo == "progressive":
        return False
    if info.get("video_codec") in INTERLACE_PRONE_CODECS:
        result = detect_interlaced_idet(path)
        if result is not None:
            return result
    return False


def resolve_interlaced(path: Path, info: dict, mode: str) -> bool:
    if mode == "off":
        return False
    if mode == "force":
        return True
    return check_interlaced(path, info)


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

    Windows: an open-without-sharing file cannot be renamed. POSIX has no
    mandatory-locking equivalent, so this is best-effort: try a non-blocking
    advisory flock (works if the holder also uses flock, e.g. some players),
    otherwise fall back to a plain open probe (catches permission issues only).
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
        import fcntl
        with open(path, "rb") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(f, fcntl.LOCK_UN)
                return False
            except OSError:
                return True
    except (ImportError, OSError):
        try:
            fd = os.open(path, os.O_RDWR)
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


# HandBrakeCLI writes its per-file progress as a repeatedly-overwritten line
# ("Encoding: task 1 of 1, 34.56 % (123.45 fps, avg 100.00 fps, ETA 00h05m23s)")
# using a bare \r, not \n - a plain `for line in proc.stdout` (line-buffered on
# \n) never sees it, which is why the GUI/CLI log looked dead for the entire
# multi-minute duration of a single encode.
_HB_PROGRESS_RE = re.compile(
    r"Encoding: task \d+ of \d+, (\d+(?:\.\d+)?)\s*%"
    r"(?:\s*\(([\d.]+) fps, avg ([\d.]+) fps, ETA ([0-9hms]+)\))?"
)


def _pump_handbrake_output(proc: subprocess.Popen, tail: list[str],
                           progress_cb) -> None:
    """Read HandBrakeCLI's merged stdout/stderr live, splitting on \\r or \\n
    so in-place progress updates are seen as they happen. Keeps a rolling
    tail of raw lines (for failure diagnostics) and forwards parsed
    percentages to progress_cb, throttled to whole-percent steps."""
    buf = ""
    last_pct = -1.0
    init_done = False
    init_count = 0
    try:
        while True:
            chunk = proc.stdout.read(256)
            if not chunk:
                break
            buf += chunk
            while True:
                cr, nl = buf.find("\r"), buf.find("\n")
                candidates = [i for i in (cr, nl) if i != -1]
                if not candidates:
                    break
                idx = min(candidates)
                line, buf = buf[:idx].strip(), buf[idx + 1:]
                if not line:
                    continue
                tail.append(line)
                del tail[:-40]
                m = _HB_PROGRESS_RE.search(line)
                if m and progress_cb:
                    init_done = True
                    pct = float(m.group(1))
                    if pct - last_pct >= 1.0 or pct >= 100.0:
                        last_pct = pct
                        fps = m.group(3) or "?"
                        eta = m.group(4) or "?"
                        progress_cb(f"{pct:.1f}% (avg {fps} fps, ETA {eta})")
                elif not init_done and progress_cb and init_count < 30:
                    # Before the first progress line, HandBrakeCLI prints its
                    # scan/init log - including the oneVPL/QSV adapter-selection
                    # line ("Impl ... adapter index N") and NVENC/CUDA device
                    # info. This is the only place that confirms which physical
                    # GPU --qsv-adapter/--encopts gpu= actually bound to.
                    # Previously it only lived in the 40-line rolling `tail`
                    # (dumped on FAILURE only) - on a successful/still-running
                    # job it silently scrolled out within seconds of the first
                    # progress update, so --verbose=1 never actually showed
                    # device selection in the Log tab no matter how long you
                    # watched. Surface it explicitly, once, here.
                    init_count += 1
                    progress_cb(f"[init] {line}")
    except (ValueError, OSError):
        pass  # pipe closed under us (process killed on timeout) - not fatal
    finally:
        line = buf.strip()
        if line:
            tail.append(line)
            del tail[:-40]


def handbrake_encode(src: Path, dst: Path, encoder: str, quality: float,
                     preset_import: str | None, extra_args: list[str],
                     hb_bin: str, timeout: int, deinterlace: bool = False,
                     gpu_index: int | None = None, progress_cb=None) -> None:
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
        # --subtitle-burned takes an OPTIONAL argument (getopt_long style:
        # "[=number, \"native\", or \"none\"]"). Passing it as two separate
        # argv tokens ("--subtitle-burned", "none") does NOT bind "none" as
        # the value - getopt treats the option as given with no argument at
        # all, which falls back to its documented no-argument default:
        # "if number is omitted, the first track is burned". That silently
        # burned-in the first (usually default-flagged) subtitle track on
        # every single encode, dropping it from the passthrough count and
        # causing verify()'s near-universal "subtitle tracks N < input N+1"
        # false failure. Confirmed via a live HandBrakeCLI --verbose=1 trace
        # (space form: "-> Render/Burn-in, Default"; "=" form: "-> Passthru,
        # Default"). Must be one joined token.
        "--subtitle-burned=none",
        "--optimize",
    ]
    if deinterlace:
        # `default` mode on both filters is frame-adaptive: comb-detect flags
        # only actually-combed frames, decomb only touches those - safe to
        # apply even if a handful of frames in an otherwise-interlaced source
        # are progressive.
        args += ["--comb-detect=default", "--decomb=default"]
    if gpu_index is not None:
        # Pins the actual encode adapter. NVENC and QSV use two completely
        # different mechanisms in HandBrakeCLI - they are NOT interchangeable:
        #   - nvenc_h265: "gpu=N" is a private --encopts key forwarded to
        #     ffmpeg's h265_nvenc, where N is a CUDA device index.
        #   - qsv_h265: adapter selection is a *top-level* CLI flag,
        #     --qsv-adapter=N (oneVPL adapter index) - it is NOT an --encopts
        #     key. Passing "--encopts gpu=N" to qsv_h265 is a silent no-op:
        #     QSV has no "gpu" encopt, so it keeps using its own default
        #     (the adapter with the highest hardware generation), which is
        #     NOT guaranteed to be the adapter you asked for.
        # Placed before extra_args so a user override still wins
        # (HandBrakeCLI uses the last occurrence of a repeated option).
        if encoder.startswith("nvenc"):
            args += ["--encopts", f"gpu={gpu_index}"]
        elif "qsv" in encoder:  # qsv_h265, av1_qsv - both are oneVPL/QSV adapters
            # --qsv-adapter is declared as a getopt_long OPTIONAL-argument
            # flag ("--qsv-adapter[=index]"), same family as
            # --subtitle-burned above which was CONFIRMED to silently drop
            # a two-token "--subtitle-burned none" value. Live-traced
            # --qsv-adapter itself and its two-token form did bind
            # correctly in this HandBrakeCLI build (confirmed a bad index
            # via space form produced "failed to create hwdevice" - i.e.
            # the value WAS received), but the single joined "=" token is
            # the form HandBrake's own --help documents and is unambiguous
            # under getopt_long, so use it defensively rather than rely on
            # this build's specific (undocumented) leniency.
            args += [f"--qsv-adapter={gpu_index}"]
    if preset_import:
        args += ["--preset-import-file", preset_import]
    args += extra_args

    log.debug("HandBrakeCLI: %s", " ".join(args))
    proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            errors="replace", bufsize=1)
    set_low_priority(proc)
    tail: list[str] = []
    reader = threading.Thread(target=_pump_handbrake_output,
                              args=(proc, tail, progress_cb), daemon=True)
    reader.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        reader.join(timeout=5)
        raise RuntimeError(f"encode timed out after {timeout}s")
    reader.join(timeout=10)
    if proc.returncode != 0:
        # HandBrakeCLI's real diagnostic (encoder init failure, invalid
        # --encopts value, missing codec, etc.) is in its own output - surface
        # the tail of it instead of just the exit code, which is otherwise
        # useless for diagnosing e.g. an out-of-range --encopts gpu=N.
        msg = f"HandBrakeCLI exited with code {proc.returncode}"
        if tail:
            msg += "\n" + "\n".join(tail[-20:])
        raise RuntimeError(msg)


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
         encoder_blacklist: dict[str, list[str]], encoders: list[str]) -> list[Path]:
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
            # Skip only if every encoder actually selected for this run is
            # blacklisted for this extension - not a hardcoded qsv/nvenc pair,
            # otherwise a single-encoder run (--encoder nvenc_h265) would
            # ignore a blacklist that names only that one encoder.
            if ext_blacklist and all(e in ext_blacklist for e in encoders):
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


def process_file(src: Path, args, encoder: str, manifest: Manifest,
                 gpu_index: int | None = None) -> str:
    size = src.stat().st_size

    # 1. Locked by Plex/Jellyfin/etc?
    if is_locked(src):
        log.warning("SKIP (locked) %s", src)
        manifest.set_status(str(src), size, "locked")
        return "locked"

    # 2. Already HEVC/AV1? Skip unless it's oversized enough that HandBrake at
    # this quality/RF setting could still meaningfully shrink it (e.g. a
    # high-bitrate HEVC remux that was itself encoded at a low RF) - only
    # applies when --recompress-hevc-over is set; 0/unset preserves the
    # original "never touch an already-compressed file" behavior.
    info = stream_info(src)
    recompress_over = getattr(args, "recompress_hevc_over", 0.0) or 0.0
    if info and already_compressed(info):
        if not (recompress_over > 0 and size >= recompress_over * GiB):
            log.info("SKIP (already %s) %s", codec_label(info), src)
            manifest.set_status(str(src), size, "skipped-hevc")
            return "skipped-hevc"
        log.info("RECOMPRESS (oversized %s, %.2f GiB >= %.2f GiB threshold) %s",
                  codec_label(info), size / GiB, recompress_over, src)

    # 3. Encode to temp dir. Prefix with a hash of the full source path so two
    # files that share a basename in different library folders (common with
    # "S01E01.mkv" style naming) can never collide while encoding in parallel.
    tmp_dir: Path = Path(args.temp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    name_hash = hashlib.sha1(str(src).encode("utf-8")).hexdigest()[:12]
    dst = tmp_dir / f"{name_hash}_{src.name}"
    dst.unlink(missing_ok=True)
    mode = getattr(args, "interlace_mode", "auto")
    deinterlace = resolve_interlaced(src, info, mode) if info else (mode == "force")
    manifest.set_status(str(src), size, "encoding", encoder=encoder)
    log.info("ENCODE [%s%s]%s %s (%.2f GiB)", encoder,
             f" adapter={gpu_index}" if gpu_index is not None else "",
             " [interlaced: decomb enabled]" if deinterlace else "", src, size / GiB)
    t0 = time.time()

    def _progress_cb(msg: str, _name=src.name, _enc=encoder) -> None:
        log.info("PROGRESS [%s] %s: %s", _enc, _name, msg)

    try:
        handbrake_encode(src, dst, encoder, args.quality, args.preset,
                         args.extra_arg or [], args.handbrake_cli,
                         args.timeout, deinterlace=deinterlace,
                         gpu_index=gpu_index, progress_cb=_progress_cb)
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
                    "extra/missing entries are ignored, unmapped encoders "
                    "get no adapter pin (see README)",
                    len(gpu_assign), len(encoders))
    encoder_blacklist: dict[str, list[str]] = cfg.get("encoder_blacklist", {})
    est_ratio = float(cfg.get("estimate_ratio", 0.45))

    manifest = Manifest(Path(args.manifest))
    manifest.reset_interrupted()  # crash/reboot recovery

    candidates = scan(root, int(args.min_size * GiB), manifest,
                      resume=args.resume, encoder_blacklist=encoder_blacklist,
                      encoders=encoders)

    recompress_over = getattr(args, "recompress_hevc_over", 0.0) or 0.0

    if args.dry_run:
        total = 0
        rows = []
        for p in candidates:
            try:
                size = p.stat().st_size
            except OSError:
                continue
            info = stream_info(p)
            if info and already_compressed(info) and not (recompress_over > 0 and size >= recompress_over * GiB):
                rows.append((p, size, info, True))
                continue
            rows.append((p, size, info, False))
        print(f"\nDRY RUN - {sum(1 for r in rows if not r[3])} file(s) would be encoded "
              f"({sum(1 for r in rows if r[3])} already HEVC/AV1, skipped):\n")
        for p, size, info, already in rows:
            codec = f"{info['video_codec'] or 'unknown'}" if info else "unprobeable"
            if already:
                print(f"  SKIP (already {codec_label(info)}) {p}  [{size / GiB:.2f} GiB, codec={codec}]")
                continue
            est = size * est_ratio
            total += size
            interlaced = resolve_interlaced(p, info, args.interlace_mode) if info \
                else (args.interlace_mode == "force")
            recompress_prefix = f"RECOMPRESS (oversized {codec_label(info)}) " if (info and already_compressed(info)) else ""
            print(f"  {recompress_prefix}{p}  [{size / GiB:.2f} GiB, codec={codec}, "
                  f"interlaced={'yes' if interlaced else 'no'}, "
                  f"est. out ~{est / GiB:.2f} GiB]")
        print(f"\nTotal input: {total / GiB:.2f} GiB | "
              f"Rough est. output: {total * est_ratio / GiB:.2f} GiB "
              f"(ratio {est_ratio}, tune via config.json 'estimate_ratio')\n")
        return 0

    if not candidates:
        log.info("Nothing to do.")
        return 0

    stats = {"done": 0, "failed": 0, "locked": 0, "skipped-hevc": 0}
    stats_lock = threading.Lock()

    # Partition candidates into one fixed lane per encoder up front, then run
    # each lane on its own dedicated thread. A shared ThreadPoolExecutor fed
    # index-parity-tagged jobs (the old approach) breaks the encoder/GPU
    # binding as soon as one lane finishes or fails faster than the other:
    # the freed worker just pulls the next queued job regardless of which
    # slot it was tagged for, so both threads can end up running the SAME
    # encoder (e.g. two qsv_h265 jobs stacked on one GPU while the nvenc lane
    # sits idle). Dedicated per-lane threads guarantee each encoder/GPU only
    # ever processes its own queue, one file at a time, matching the log.
    lanes: list[list[Path]] = [[] for _ in encoders]
    for i, p in enumerate(candidates):
        lanes[i % len(encoders)].append(p)

    def run_lane(lane_files: list[Path], encoder: str, gpu_index: int | None):
        for p in lane_files:
            result = process_file(p, args, encoder, manifest, gpu_index)
            with stats_lock:
                stats[result] = stats.get(result, 0) + 1

    threads = []
    for slot, encoder in enumerate(encoders):
        gpu_index = gpu_assign[slot] if gpu_assign and slot < len(gpu_assign) else None
        t = threading.Thread(target=run_lane, args=(lanes[slot], encoder, gpu_index),
                             name=f"encoder-{slot}-{encoder}", daemon=False)
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

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

    tmp = (TOOL_DIR / "temp").resolve()  # matches --temp-dir's default in `run`
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
    p.add_argument("--version", action="version", version=f"compress-library {__version__}")
    sub = p.add_subparsers(dest="command")

    run = sub.add_parser("run", help="encode a library (default command)")
    run.add_argument("root", help="library root directory to scan")
    run.add_argument("--dry-run", action="store_true",
                     help="list what would be encoded; encode nothing")
    run.add_argument("--min-size", type=float, default=2.0,
                     help="skip files smaller than this many GB (default 2)")
    run.add_argument("--recompress-hevc-over", type=float, default=0.0,
                     help="re-encode already-HEVC files at or above this many GB "
                          "instead of unconditionally skipping them (default 0 = "
                          "disabled, HEVC is always skipped). Use for oversized "
                          "HEVC remuxes that were themselves encoded at a low RF "
                          "and can still shrink further at this run's --quality")
    run.add_argument("--quality", type=float, default=25.0,
                     help="HandBrake constant-quality RF (default 25)")
    run.add_argument("--encoder", nargs="+",
                     choices=["qsv_h265", "nvenc_h265", "av1_qsv"],
                     default=["qsv_h265", "nvenc_h265"],
                     help="encoder(s); pass both qsv_h265+nvenc_h265 for one-encode-per-GPU "
                          "(default). av1_qsv uses Arc's AV1 hardware encode block instead of "
                          "HEVC on the QSV lane (Arc-only; no consumer NVENC GPU can hardware-"
                          "encode AV1 as of Ada/40-series, so there is no av1_nvenc) - e.g. "
                          "--encoder av1_qsv nvenc_h265")
    run.add_argument("--gpu-assign", default="0,1",
                     help="adapter index per encoder, comma list (default 0,1); "
                          "for nvenc_h265 this is a CUDA device index sent as "
                          "--encopts gpu=N (only 0 is valid on a single-NVIDIA-GPU "
                          "box); for qsv_h265/av1_qsv this is a oneVPL adapter index sent "
                          "as --qsv-adapter=N (0 = HandBrake's own default, "
                          "the highest hardware-generation Intel GPU present)")
    run.add_argument("--preset", metavar="FILE",
                     help="HandBrake preset JSON to import (--preset-import-file)")
    run.add_argument("--extra-arg", action="append",
                     help="extra raw HandBrakeCLI arg (repeatable), e.g. "
                          "--extra-arg=--encopts=tune=ssim")
    run.add_argument("--temp-dir", default=str(TOOL_DIR / "temp"),
                     help="SSD temp dir for encodes (default ./temp)")
    run.add_argument("--duration-tolerance", type=float, default=2.0,
                     help="max duration drift in seconds (default 2)")
    run.add_argument("--interlace-mode", choices=["auto", "force", "off"], default="auto",
                     help="deinterlacing: auto detects interlaced sources (field_order, "
                          "or an ffmpeg idet sample for vc1/mpeg2/mpeg1 codecs whose "
                          "field_order is unreliable) and enables HandBrake's adaptive "
                          "--comb-detect/--decomb; force always enables it; off never does "
                          "(default auto)")
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
    if argv and argv[0] not in ("run", "preflight", "-h", "--help", "--version"):
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
