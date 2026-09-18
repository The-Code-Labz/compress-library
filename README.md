# compress-library

H.265 media library compressor for dual-GPU Windows servers. Recursively scans a
library, transcodes fat video files to HEVC with full audio/subtitle passthrough,
verifies every encode, and only then atomically replaces the original — so a crash,
a locked file, or a bad encode never costs you the source.

**Target hardware ("Optimus"):**
Windows 11 · i9-13900K · 128 GB RAM
- GPU 0 — Intel Arc A580 (Quick Sync) → `qsv_h265` (or `qsv_av1`, see [AV1 on Arc](#av1-on-arc))
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
| `--recompress-hevc-over` | `0` (off) | Re-encode already-HEVC/AV1 files whose size is ≥ this many GB, instead of always skipping them. Useful for oversized remuxes that were themselves encoded at a low RF and can still shrink further at this run's `--quality`. The existing verification gate still applies — if the re-encode doesn't come out smaller than the source, it fails verification and the original is left untouched. `0` (default) preserves the original "never touch an already-compressed file" behavior. |
| `--quality` | `25` | HandBrake RF constant quality. Lower = better/bigger. 20–28 is the useful range for HEVC. AV1's RF scale is NOT equivalent to HEVC's — do not reuse the same value blind; test on a sample first. |
| `--encoder` | both | `qsv_h265` and/or `nvenc_h265`, or `qsv_av1` in place of `qsv_h265`. See [AV1 on Arc](#av1-on-arc) below. |
| `--gpu-assign` | `0,1` | Per-encoder adapter index. For `nvenc_h265` this is a CUDA device index sent as `--encopts gpu=N`; for `qsv_h265`/`qsv_av1` this is a oneVPL adapter index sent as the top-level `--qsv-adapter=N` flag (they are two different mechanisms — see [Finding your GPU index](#finding-your-gpu-index) and caveats below). |
| `--preset` | — | HandBrake preset JSON (`--preset-import-file`). CLI flags still override the preset. |
| `--extra-arg` | — | Repeatable raw HandBrakeCLI args, e.g. `--extra-arg=--encopts=tune=ssim`. |
| `--temp-dir` | `./temp` | SSD scratch dir for encodes. Must have free space ≥ your largest file. |
| `--duration-tolerance` | `2` | Max allowed duration drift between input and output, seconds. |
| `--interlace-mode` | `auto` | `auto` detects interlaced sources and enables HandBrake's adaptive `--comb-detect`/`--decomb`; `force` always enables it; `off` never does. See [Interlaced sources](#interlaced-sources) below. |
| `--timeout` | `21600` | Per-file encode timeout (seconds). Kills runaway encodes. |
| `--manifest` | `./manifest.db` | SQLite manifest for resume. |
| `--log` | `./compress-library.log` | Log file — every file, encoder, sizes, ratio, status. |
| `--no-resume` | — | Re-encode files already marked done. |

## AV1 on Arc

`qsv_av1` uses Arc's dedicated AV1 hardware encode block instead of `qsv_h265`
on the QSV lane — pass it in place of `qsv_h265`, e.g.
`--encoder qsv_av1 nvenc_h265`.

**There is no NVENC AV1 encode on this hardware.** RTX 30-series (Ampere,
8th-gen NVENC) has no AV1 encode silicon — that only shipped starting with
Ada Lovelace (RTX 40-series). An RTX 3070 can hardware-*decode* AV1 fine but
cannot encode it; `av1_nvenc` would fail identically to an out-of-range
`--encopts gpu=N` (`No capable devices found`). So a mixed
`qsv_av1` + `nvenc_h265` run is not a choice between two AV1 options — it's
the only viable pairing if you want AV1 at all on this box; the NVENC lane
stays HEVC.

Before switching a whole library over:

- **RF scale is not shared with HEVC.** `--quality 25` does not target the
  same visual quality on `qsv_av1` as it does on `qsv_h265`/`nvenc_h265`.
  Test a value against a real sample and eyeball playback before committing
  a batch run.
- **Playback/decode compatibility is the real risk, not encode speed.**
  Hardware AV1 *decode* is far less universal than HEVC's — older smart
  TVs, streaming boxes, and pre-2020 clients often can't hardware-decode
  it, forcing a heavier CPU software transcode on playback. Confirm your
  actual playback devices (and, for Jellyfin/Plex, that a test AV1 file's
  transcode session shows hardware, not software, decode) before
  converting a whole library.
- **Already-AV1 files are now skip-detected** (`already_compressed()` /
  `codec_label()` cover both HEVC and AV1) — a second run over `qsv_av1`
  output correctly logs `SKIP (already AV1)` instead of re-encoding
  AV1→AV1 (lossy-on-lossy), the same protection HEVC always had.

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

**Confirmed via a live `--verbose=1` oneVPL device-enumeration trace on the
Arc A580 + UHD 770 reference box this tool targets:** oneVPL's own index
order is `0 = integrated (UHD 770)`, `1 = discrete (Arc A580)`. That is,
`--gpu-assign N,0` where `N` is `1` targets Arc for `qsv_h265`. This is
**not guaranteed on other hardware/driver combinations** (integrated vs.
discrete ordering can differ by system) — verify empirically per the steps
below before assuming this pairing on a different box. Also note: forcing
this index made no visible difference on the reference box, because
HandBrake's own no-flag default already picks "the adapter with the highest
hardware generation" (Arc, correctly, on this hardware) — `--qsv-adapter`
mainly matters when you *want* to override that default (e.g. force
UHD-only to keep Arc free for something else), not to "fix" QSV landing on
the wrong card by default.

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

- **Fixed (v1.7.0): `--all-subtitles` silently enabled HandBrake's "Foreign
  Audio Search" on any source with a forced subtitle track, which looked
  exactly like a hang.** Confirmed via a live `--verbose=1` trace on a
  44-subtitle-track remux: the constructed job JSON showed
  `Subtitle.Search.Enable: true` only when `--all-subtitles` was used, `false`
  when subtitles were explicitly restricted. Foreign Audio Search is a full
  *extra* decode-only pre-pass over the entire file to auto-pick a forced
  track to burn in, inserted as "task 1 of 2" before the real encode even
  starts. During that pre-pass HandBrake's `%` (bytes read) races ahead while
  `avg fps` (frames actually finished) stays pinned at `0.00` for the whole
  scan - on a multi-hour file this is indistinguishable from a genuine hang,
  and it affected both QSV and NVENC identically (it has nothing to do with
  either GPU). This is an upstream HandBrake issue: `--all-subtitles` is
  *documented* as unrelated to the `"scan"` pseudo-track that's supposed to be
  the only Foreign Audio Search trigger
  ([HandBrake/HandBrake#5731](https://github.com/HandBrake/HandBrake/issues/5731)),
  and there's no clean CLI flag to force it back off once it's on
  ([#7788](https://github.com/HandBrake/HandBrake/issues/7788)). Fixed by
  never passing `--all-subtitles`/the literal `"scan"` value - instead every
  real subtitle track is selected by its explicit 1-based index (`-s
  "1,2,...,N"`), built from the same count `verify()` already gets via
  `stream_info()`. Falls back to `--all-subtitles` only when subtitle count is
  unknown (ffprobe couldn't read the file at all - the `unprobeable`-codec
  edge case). If your runs previously appeared to hang forever at some
  percentage with `avg fps` stuck at `0.00`, pull latest - that was this bug,
  not your GPU/driver.

- **Fixed (v1.6.1): `qsv_av1` was named `av1_qsv` in v1.6.0 - not a real
  HandBrakeCLI encoder.** Confirmed via a live `HandBrakeCLI --help`/trace on
  real hardware: HandBrake's QSV family is `qsv_<codec>` (`qsv_h264`,
  `qsv_h265`, `qsv_av1`) - only the *NVENC* family flips the order
  (`hevc_nvenc`, `av1_nvenc`). `av1_qsv` doesn't exist, so every job failed
  immediately with `ERROR: Invalid video encoder (av1_qsv)` - but
  **HandBrakeCLI exits with code 0 even on this init failure**, so
  `handbrake_encode()`'s `returncode != 0` check never caught it; the
  pipeline fell through to `verify()`, which reported the missing output as
  `FAIL (verify): output unreadable by ffprobe` instead of a clear encode
  error. Renamed to the correct `qsv_av1` everywhere (CLI choice, GUI
  checkbox/variable, docs). If you hit `qsv_av1`/`av1_qsv` confusion on an
  older checkout, pull latest.

- **Added (v1.6.0): `qsv_av1` encoder option (Arc AV1 hardware encode, in
  place of `qsv_h265`).** See [AV1 on Arc](#av1-on-arc) above for the full
  picture. Two things fixed alongside it, not just added: the GPU-index
  detection in `handbrake_encode()` matched on `encoder.startswith("qsv")`,
  which was `False` for the (then-misnamed) AV1 encoder id - `--qsv-adapter`
  would have silently gone unset for the AV1 lane. Changed to `"qsv" in
  encoder`. The "already compressed, skip" logic (`is_h265()` /
  `HEVC_CODEC_NAMES`) was also HEVC-only - generalized to
  `already_compressed()` / `codec_label()` covering both HEVC and AV1, so
  `qsv_av1` output doesn't get silently re-encoded AV1→AV1 on the next run.

- **Fixed (v1.5.0): verify almost always failed with a false
  `subtitle tracks N < input N+1` on virtually every title.** Root cause
  confirmed with a live `HandBrakeCLI --verbose=1` trace: `--subtitle-burned`
  is a `getopt_long` **optional-argument** flag
  (`--subtitle-burned[=number, "native", or "none"]`). The command was built
  as two separate argv tokens - `["--subtitle-burned", "none"]` - and
  getopt_long does **not** bind a space-separated value to an optional-arg
  flag; it silently treats the flag as given with no argument at all, which
  falls back to its documented default: *"if number is omitted, the first
  track is burned."* So every single encode was silently burning the first
  subtitle track into the video (confirmed in the trace: `subtitle track 1
  ... -> Render/Burn-in, Default`) regardless of the `none` we thought we'd
  passed, dropping it from the passthrough count by exactly one - hence the
  always-off-by-1 pattern across every title, independent of how many real
  tracks it had. Switching to a single joined token, `--subtitle-burned=none`,
  fixes it (verified in the same trace: `-> Passthru, Default`, no burn).
  Nothing was ever actually lost (the safety gate kept every original
  untouched, as designed) - the cost was purely wasted GPU time per title.
  `--qsv-adapter` was changed to the same joined-token form defensively (see
  GPU pinning note below) even though it was independently confirmed to
  still bind correctly via space-separated args on this HandBrake build -
  the joined form is what HandBrake's own `--help` documents and is
  unambiguous under `getopt_long`, so there's no reason to rely on
  build-specific leniency for an optional-arg flag.
  (v1.4.1's `eia_608`/`eia_708` exclusion in `stream_info()` is kept as a
  harmless defensive measure - some sources do carry those as pseudo
  subtitle streams - but it was **not** the actual cause of the failures
  seen in practice; the getopt binding bug above was.)

- **Fixed (v1.5.1): `--verbose=1` never actually showed which physical
  adapter QSV/NVENC bound to, no matter how long you watched the Log tab.**
  HandBrakeCLI prints its adapter-selection line (`Impl ... - Intel(R)
  Arc(TM) A580 Graphics, adapter index N`) once, during its scan/init phase,
  before any `Encoding: task ...` progress line appears. The progress reader
  (`_pump_handbrake_output`) only ever kept a 40-line rolling tail of raw
  output and only surfaced it on **failure** - on a successful or
  still-running job, that one init line got pushed out of the 40-line window
  within seconds of the first progress update and was gone for good. Fixed:
  every line seen before the first progress update is now logged once as
  `[init] <line>` through the same `PROGRESS [...]` channel, so the real
  adapter-selection line is visible in the Log tab going forward instead of
  requiring an out-of-band trace to see it.

- **QSV encode sessions are invisible to Windows' "GPU Engine" performance
  counters (the same ones Task Manager reads) on at least some driver
  stacks.** Confirmed by direct measurement: a live NVENC job showed up
  correctly (`engtype_videoencode` engine busy, multi-GB dedicated VRAM on
  its adapter's LUID), but a concurrent QSV job registered **zero** activity
  on every GPU engine it had a handle on - it didn't even show an open
  handle to either Intel adapter. This is a known limitation of Intel Media
  SDK/oneVPL's legacy D3D9Ex-based hardware-encode path on some hybrid
  multi-GPU systems, not evidence the encode isn't happening. Don't use Task
  Manager's per-GPU "Video Encode" graph to judge whether QSV is running -
  use the `[init]` adapter-selection line above (or the file's total elapsed
  time vs. a CPU-software-encode estimate) instead.
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
- **HandBrakeCLI's own output is now captured** (previously discarded to
  `DEVNULL`). Encode failures log the last ~20 lines of its actual output
  alongside the exit code, instead of just `HandBrakeCLI exited with code N`.
- **Fixed (v1.4.0): the log looked dead for the entire duration of an encode.**
  `handbrake_encode()` used to block on `subprocess.communicate()` with
  stdout thrown away, so nothing appeared between a file's `ENCODE ...` line
  and its eventual completion/failure — for a large remux that's 20-40+
  minutes of apparent silence in both the CLI and the GUI's Log tab.
  HandBrakeCLI's own progress line
  (`Encoding: task 1 of 1, 34.56 % (123.45 fps, avg 100.00 fps, ETA 00h05m23s)`)
  updates in place with a bare `\r`, not `\n`, which is also why a naive
  line-buffered reader wouldn't have picked it up anyway. Output is now
  streamed live on a reader thread that splits on either `\r` or `\n`, and
  every whole-percent change is re-logged as
  `PROGRESS [encoder] filename: NN.N% (avg X fps, ETA HHhMMmSSs)` — both the
  CLI and the GUI's existing live log tail pick these up automatically, no
  GUI changes needed.
- **Audio is passthrough by default** — `--aencoder copy`. Some exotic audio
  (TrueHD, DTS:X) falls back to `ffac3` per `--audio-fallback`; that's by design.
- **`.avi` / `.ts` sources** keep their exact original filename (per spec) but
  carry an MKV container internally — players that sniff content handle this
  fine; strict extension-sniffing setups may care.
- **Verify failures keep the original.** If the output isn't smaller, or drops
  a track, the source stays put and the temp file is deleted.
- **Fixed (v1.3.1): GUI's "Extra HandBrakeCLI args" field crashed the run/dry-run
  before it started, whenever a line began with `--` (e.g. `--verbose=1`).** The
  GUI built each extra arg as two separate argv tokens (`--extra-arg`, `line`);
  argparse then read the `--`-prefixed value as a new flag instead of
  `--extra-arg`'s argument, failing immediately with
  `error: argument --extra-arg: expected one argument` (status bar showed
  `exit=2`, no encoding activity). Fixed to emit one joined `--extra-arg=<line>`
  token per line, matching the CLI's own required syntax.

## Resume / crash recovery

State lives in `manifest.db`. If the machine reboots mid-batch, running the same
command again picks up where it left off: `done` files are skipped, files stuck
in `encoding` are reset to pending, failures are retried. Delete `manifest.db`
to start the ledger over (does not touch media).

## Safety rules (enforced in code)

- Source files are never deleted or overwritten until verification passes.
- Failed encodes delete only their temp file.
- Every action is logged: file, encoder, sizes before/after, ratio, status.
