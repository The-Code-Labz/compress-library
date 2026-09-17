# compress-library

H.265 media library compressor for dual-GPU Windows servers. Recursively scans a
library, transcodes fat video files to HEVC with full audio/subtitle passthrough,
verifies every encode, and only then atomically replaces the original — so a crash,
a locked file, or a bad encode never costs you the source.

**Target hardware ("Optimus"):**
Windows 11 · i9-13900K · 128 GB RAM
- GPU 0 — Intel Arc A580 (Quick Sync) → `qsv_h265`
- GPU 1 — RTX 3070 (NVENC) → `nvenc_h265`

Two encodes run in parallel, one per GPU, at below-normal process priority, so the
box stays responsive for Plex/Jellyfin and everything else.

## How it works

1. Recursively find `.mkv .mp4 .m4v .avi .ts` larger than `--min-size` GB (default 2).
2. Files already H.265 are skipped (ffprobe codec check).
3. Files locked/open by another process (Plex, Jellyfin, a player) are skipped, logged, and retried on the next run — never fatal.
4. Each file is encoded **to a temp dir on your SSD** — the source is never touched mid-encode.
   Audio and subtitles pass through untouched (`--all-audio --aencoder copy --all-subtitles`).
5. **Verification gate** (all must pass before anything is replaced):
   - output has ≥ input audio track count
   - output has ≥ input subtitle track count
   - duration matches within `--duration-tolerance` seconds
   - output is smaller than input
6. Only then: the output takes the **exact original filename** and the original is deleted (atomic `os.replace` on the same filesystem).
7. Any failure deletes the temp file, logs the reason, and moves to the next file.
8. A SQLite manifest (`manifest.db`) tracks done/failed/in-progress, so a crash or reboot resumes with `--resume` without re-encoding.

## Install (Windows)

```powershell
winget install HandBrake.HandBrakeCLI
winget install ffmpeg          # provides ffprobe; or install ffmpeg and add to PATH
pip install -e .
```

Restart the terminal afterward so PATH picks up both binaries. On Linux,
`HandBrakeCLI` + `ffmpeg` from your distro packages work the same way (Tkinter
also needs a system package on Linux, e.g. `apt install python3-tk`; it ships
built into the standard Windows/macOS Python installers).

`pip install -e .` registers two commands on PATH (a virtualenv is recommended):

```bash
compress-library preflight
compress-library /path/to/library --dry-run
compress-library-gui
```

`compress-library` also accepts `run` explicitly (`compress-library run /path/to/library ...`)
— the bare form is just a shorthand. If you'd rather not install the package,
`python compress_library.py ...` / `python gui.py` still work exactly the same.

> **QSV on Arc:** make sure the Intel Arc graphics driver is current — old drivers
> break Quick Sync encode in HandBrake. Run the preflight check below to confirm.

## GUI

A desktop control panel is included (`gui.py`, stdlib Tkinter — no extra deps
beyond `requirements.txt`):

```powershell
compress-library-gui     # if installed via `pip install -e .`
python gui.py            # or run the script directly
```

It never imports or modifies `compress_library.py` — it only builds a CLI
invocation from the form fields and launches `compress_library.py` as a
subprocess, so the encode/verify/replace core stays exactly as battle-tested
above. Features:

- **Library / Options** — root folder picker, min size, quality (RF),
  encoder checkboxes (QSV/NVENC), GPU assign, temp dir, duration tolerance,
  interlace mode (auto/force/off), timeout, manifest/log/config paths,
  preset file, extra HandBrakeCLI args, resume toggle.
- **Preflight** — runs `compress_library.py preflight` and streams the result.
- **Dry Run** — runs `--dry-run` and renders candidates into a sortable table
  (size, codec, interlaced, estimated output) instead of raw text.
- **Start / Stop** — runs a real batch; Stop kills the whole process tree
  (via `psutil`, if installed) so an in-flight HandBrakeCLI child doesn't
  survive as an orphan.
- **Log tab** — live tail of the subprocess's stdout/log stream.
- **Manifest tab** — reads `manifest.db` read-only and shows status/ratio/
  attempts/error per file; auto-refreshes while a batch runs.
- **Config tab** — view/edit `config.json` (`estimate_ratio`,
  `encoder_blacklist`) and save it back.
- **Help → Check for Updates…** — compares your local `compress_library.py`
  `__version__` against `main` on GitHub (also checked silently ~2s after
  launch). If a newer version exists, it offers to download and install it:
  both `compress_library.py` and `gui.py` are backed up to
  `<name>.bak-<timestamp>` next to the originals, the new versions are
  written atomically, and you're offered an immediate restart (re-execs the
  GUI process) to pick them up. Requires outbound HTTPS to
  `raw.githubusercontent.com`; fails quietly on the silent startup check,
  shows an error dialog if triggered manually.

## Usage

```powershell
python compress_library.py preflight                  # verify GPUs + binaries first
python compress_library.py "D:\Media" --dry-run       # see what would be encoded
python compress_library.py "D:\Media"                 # encode (both GPUs in parallel)
python compress_library.py "D:\Media" --quality 23    # higher quality, bigger files
python compress_library.py "D:\Media" --encoder nvenc_h265   # single GPU
python compress_library.py "D:\Media" --preset mypreset.json # custom preset import
```

`run` is the default command — `python compress_library.py "D:\Media"` works
without typing `run`.

## Flags

| Flag | Default | Description |
|---|---|---|
| `--dry-run` | off | List candidates with sizes + estimated output. Encodes nothing. |
| `--min-size` | `2` | Skip files smaller than this many GB. |
| `--quality` | `25` | HandBrake RF constant quality. Lower = better/bigger. 20–28 is the useful range. |
| `--encoder` | both | `qsv_h265` and/or `nvenc_h265`. Passing both runs one encode per GPU. |
| `--gpu-assign` | `0,1` | Advisory GPU adapter index per encoder. See caveats below. |
| `--preset` | — | HandBrake preset JSON (`--preset-import-file`). CLI flags still override the preset. |
| `--extra-arg` | — | Repeatable raw HandBrakeCLI args, e.g. `--extra-arg=--encopts=tune=ssim`. |
| `--temp-dir` | `./temp` | SSD scratch dir for encodes. Must have free space ≥ your largest file. |
| `--duration-tolerance` | `2` | Max allowed duration drift between input and output, seconds. |
| `--interlace-mode` | `auto` | `auto` detects interlaced sources and enables HandBrake's adaptive `--comb-detect`/`--decomb`; `force` always enables it; `off` never does. See [Interlaced sources](#interlaced-sources) below. |
| `--timeout` | `21600` | Per-file encode timeout (seconds). Kills runaway encodes. |
| `--manifest` | `./manifest.db` | SQLite manifest for resume. |
| `--log` | `./compress-library.log` | Log file — every file, encoder, sizes, ratio, status. |
| `--no-resume` | — | Re-encode files already marked done. |

## Interlaced sources

Older Blu-ray/WEB rips using VC-1 or MPEG-2 (`Rush Hour 3`, `The Mummy`
trilogy-style remuxes, etc.) are frequently interlaced, and their ffprobe
`field_order` metadata is often missing/`unknown` even when the source
genuinely is. Encoding an interlaced source without deinterlacing bakes in
combing artifacts.

With `--interlace-mode auto` (the default):
1. Explicit `field_order` (`tt`/`bb`/`tb`/`bt`) is trusted directly — no extra cost.
2. If `field_order` is `unknown`/missing **and** the codec is `vc1`,
   `mpeg2video`, or `mpeg1video` (the codecs known to under-report it), a
   short ffmpeg `idet` sample (100 frames) is decoded to get a real answer.
3. Everything else is assumed progressive (matches the vast majority of
   H.264/H.265 BluRay/WEB rips, and keeps scanning fast).

When a file is flagged interlaced, HandBrakeCLI gets
`--comb-detect=default --decomb=default` — HandBrake's adaptive filter,
which only touches frames it actually detects as combed, so it's safe even
if the interlace call is a false positive on a handful of frames.

`--dry-run` shows the verdict per file (`interlaced=yes|no`) so you can
review before committing to a batch. Use `--interlace-mode off` to disable
detection entirely (old behavior), or `force` to always deinterlace.

## Config (`config.json`, optional)

Copy `config.example.json` → `config.json` in the tool dir.

- `estimate_ratio` — ratio used by `--dry-run` output estimates. Tune to your real results.
- `encoder_blacklist` — per-extension encoder bans, e.g. `{".ts": ["qsv_h265"]}`
  forces NVENC for transport-stream files if QSV quality disappoints there.
  Listing **both** encoders skips the extension entirely.

## Caveats worth knowing

- **GPU adapter pinning is advisory.** HandBrakeCLI does not expose a clean
  per-adapter flag for either QSV or NVENC — encoders typically bind to the
  primary/Discrete GPU. `--gpu-assign` is recorded and validated, but on a
  mixed Arc + RTX box, expect QSV to land on the Arc and NVENC on the RTX as
  long as Arc is the compute/encode-preferred adapter in Intel graphics
  settings. Watch the log lines `ENCODE [qsv_h265] ...` to confirm behavior
  on your box; the preflight GPU list tells you what Windows sees.
- **Audio is passthrough by default** — `--aencoder copy`. Some exotic audio
  (TrueHD, DTS:X) falls back to `ffac3` per `--audio-fallback`; that's by design.
- **`.avi` / `.ts` sources** keep their exact original filename (per spec) but
  carry an MKV container internally — players that sniff content handle this
  fine; strict extension-sniffing setups may care.
- **Verify failures keep the original.** If the output isn't smaller, or drops
  a track, the source stays put and the temp file is deleted.

## Resume / crash recovery

State lives in `manifest.db`. If the machine reboots mid-batch, running the same
command again picks up where it left off: `done` files are skipped, files stuck
in `encoding` are reset to pending, failures are retried. Delete `manifest.db`
to start the ledger over (does not touch media).

## Safety rules (enforced in code)

- Source files are never deleted or overwritten until verification passes.
- Failed encodes delete only their temp file.
- Every action is logged: file, encoder, sizes before/after, ratio, status.
