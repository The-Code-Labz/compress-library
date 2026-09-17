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
2. Files already H.265 are skipped (ffprobe codec check) — unless `--recompress-hevc-over` is set (see below), which re-queues oversized HEVC files instead of skipping them.
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

- **Library / Options** — root folder picker, min size, recompress-HEVC-over
  threshold, quality (RF), encoder checkboxes (QSV/NVENC), GPU assign, temp
  dir, duration tolerance, interlace mode (auto/force/off), timeout,
  manifest/log/config paths, preset file, extra HandBrakeCLI args, resume
  toggle.
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
| `--recompress-hevc-over` | `0` (off) | Re-encode already-HEVC files whose size is ≥ this many GB, instead of always skipping HEVC. Useful for oversized HEVC remuxes that were themselves encoded at a low RF and can still shrink further at this run's `--quality`. The existing verification gate still applies — if the re-encode doesn't come out smaller than the source, it fails verification and the original is left untouched. `0` (default) preserves the original "never touch HEVC" behavior. |
| `--quality` | `25` | HandBrake RF constant quality. Lower = better/bigger. 20–28 is the useful range. |
| `--encoder` | both | `qsv_h265` and/or `nvenc_h265`. Passing both runs one encode per GPU. |
| `--gpu-assign` | `0,1` | Per-encoder adapter index. For `nvenc_h265` this is a CUDA device index sent as `--encopts gpu=N`; for `qsv_h265` this is a oneVPL adapter index sent as the top-level `--qsv-adapter=N` flag (they are two different mechanisms — see [Finding your GPU index](#finding-your-gpu-index) and caveats below). |
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

## Finding your GPU index

`--gpu-assign` indexes are **not** the order Windows/Device Manager/preflight
list adapters in, and QSV and NVENC do **not** share the mechanism:

- `nvenc_h265` → CUDA device index, sent as the `--encopts gpu=N` private
  encoder option. Only meaningful across your NVIDIA GPUs. On a box with a
  single NVIDIA GPU the only valid value is `0` — anything else fails fast
  with `HandBrakeCLI exited with code 3`.
- `qsv_h265` → Intel oneVPL adapter index, sent as the **top-level**
  `--qsv-adapter=N` CLI flag (not an `--encopts` key at all — HandBrake has
  no `gpu=` encopt for QSV; a version before v1.2.1 wrongly sent
  `--encopts gpu=N` here, which QSV silently ignored). HandBrake's own
  default with no `--qsv-adapter` given is **the Intel adapter with the
  highest hardware generation** — on a system with an Arc + UHD iGPU, that
  should already prefer Arc without any flag at all.

There's no single command that maps "adapter #3 in Device Manager" to
`--gpu-assign`'s index ahead of time, so confirm empirically:

1. Run `compress-library preflight` — it lists every GPU Windows sees, in
   Device Manager order, purely for identification (this list is **not**
   the index order).
2. Start a real encode (`compress-library-gui` → Start, or `run` without
   `--dry-run`) with your current guess, e.g. `--gpu-assign 0,0` (`0` is the
   only valid NVENC value on a single-NVIDIA box).
3. Open Task Manager → Performance tab → click each GPU tile, then
   right-click one of its 4 mini-graphs and pick **Video Encode** —
   the tile's headline % is whatever engine it's set to show (often "3D"
   by default), so an idle-looking GPU may just be showing the wrong
   engine, not actually be idle. Watch the Video Encode graph specifically
   for the QSV GPU and the NVENC GPU while the job runs.
4. If QSV (`qsv_h265`) lit up the wrong Intel adapter (e.g. UHD 770 instead
   of Arc), try an explicit `--qsv-adapter` value via `--gpu-assign` (e.g.
   `1,0`) and re-test. Indexes are small integers starting at 0 per-vendor,
   not a shared global list across QSV+NVENC.
5. `--log` / the Log tab also shows `ENCODE [qsv_h265 adapter=1] ...` per
   file so you can confirm which index was actually sent, without guessing
   from the command line.

Once you find the pair that lights up the correct adapters, it's stable for
that machine — no need to re-check on future runs unless you change hardware
or GPU drivers.

## Config (`config.json`, optional)

Copy `config.example.json` → `config.json` in the tool dir.

- `estimate_ratio` — ratio used by `--dry-run` output estimates. Tune to your real results.
- `encoder_blacklist` — per-extension encoder bans, e.g. `{".ts": ["qsv_h265"]}`
  forces NVENC for transport-stream files if QSV quality disappoints there.
  Listing **both** encoders skips the extension entirely.

## Caveats worth knowing

- **GPU adapter pinning uses two different HandBrakeCLI mechanisms**, one
  entry of `--gpu-assign` per `--encoder` slot (candidates are split into a
  fixed lane per encoder up front — `encoders[N % len(encoders)]` gets every
  Nth file — and each lane runs on its own dedicated thread bound to
  `gpu_assign[N % len(encoders)]` for its entire run): `nvenc_h265` gets
  `--encopts gpu=N`, `qsv_h265` gets the top-level `--qsv-adapter=N` flag.
  **Fixed in v1.2.1**: earlier versions sent `--encopts gpu=N` to `qsv_h265`
  too — QSV has no such encopt, so it was a silent no-op and QSV always fell
  back to HandBrake's own default adapter (the highest hardware-generation
  Intel GPU) regardless of what `--gpu-assign` said. If you supply your own
  conflicting `--encopts`/`--qsv-adapter` via `--extra-arg`, yours wins
  (HandBrakeCLI honors the last occurrence of a repeated option). See
  [Finding your GPU index](#finding-your-gpu-index) to determine which number
  maps to which physical card on your box — the preflight adapter list is
  Device Manager order, not the oneVPL/CUDA index. NVENC also has a known
  upstream driver quirk (HandBrake #7308) where some driver versions ignore
  `gpu=` and always use GPU 0 — only relevant if you have more than one
  NVIDIA GPU. The NVENC index is a **CUDA device index over NVIDIA GPUs
  only** (not a shared global adapter list with QSV) — on a box with a single
  NVIDIA GPU, the only valid value for the `nvenc_h265` slot is `0`; anything
  else fails fast with `HandBrakeCLI exited with code 3`.
- **Fixed (v1.2.0): encoder/GPU lanes could cross-contaminate.** Earlier
  versions queued every candidate into one shared thread pool tagged by list
  index; if one lane's jobs failed or finished faster than the other, the
  freed worker thread would grab the next queued file regardless of which
  encoder it was tagged for — so both threads could end up running the
  *same* encoder concurrently (e.g. two `qsv_h265` jobs stacked on one GPU)
  while the other lane sat idle, visible in the GUI's queue table as two rows
  simultaneously `encoding` with the same `Encoder` value. Each encoder now
  gets its own dedicated worker thread processing only its own
  pre-partitioned file list, so this can no longer happen.
- **HandBrakeCLI stderr is now captured on failure** (previously discarded to
  `DEVNULL`). Encode failures log the last ~20 lines of HandBrakeCLI's actual
  stderr output alongside the exit code, instead of just
  `HandBrakeCLI exited with code N`.
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
