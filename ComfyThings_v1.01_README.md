# ComfyThings v1.01

**Draw Things → ComfyUI metadata and media exporter**  
**Platform:** macOS  
**Release:** v1.01 (October 2026)

ComfyThings exports images stored in **Draw Things SQLite project databases** as full-resolution PNG files containing reconstructed **ComfyUI workflows** and available original generation information. It can also export encoded videos, per-video workflow PNGs, optional individual frames, and audio.

The exporter reads the source database **without modifying it**, operates on **one image at a time**, and supports verifying and resuming existing exports. Reconstructed workflows are best-effort: model availability and ComfyUI node compatibility depend on the local ComfyUI installation.

## Contents

- [Requirements](#requirements)
- [Installation](#installation)
- [Quick start](#quick-start)
- [How conversion works](#how-conversion-works)
- [What's new in v1.01](#whats-new-in-v101)
- [Command-line reference](#command-line-reference)
- [Examples](#examples)
- [Output layout](#output-layout)
- [Performance and safety](#performance-and-safety)
- [Known limitations and troubleshooting](#known-limitations-and-troubleshooting)

## Requirements

- **macOS**. Other platforms are not supported by this release.
- **Python 3.9 or newer**.
- Python packages: **numpy**, **fpzip**, **certifi** (listed in `requirements.txt`).
- **FFmpeg and ffprobe** on your `PATH` to encode video. They are not required for images-only exports.
- Sufficient free disk space for PNGs, video files, and temporary output during atomic writes.
- A Draw Things project database (`.sqlite3`), preferably a copy of the original.

Some ComfyUI workflows require compatible model weights and additional nodes. The exporter does **not** download or substitute checkpoint files automatically.

## Installation

Copy `ComfyThings.py`, `README.md`, and `requirements.txt` into a directory. In Terminal:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

If you will export videos, install FFmpeg, for example with Homebrew:

```bash
brew install ffmpeg
```

Verify the command-line interface:

```bash
python3 ComfyThings.py --version
python3 ComfyThings.py --help
```

**Protect the source database.** Quit Draw Things before copying the project. If the source database has `-wal` or `-shm` sidecar files, copy the necessary sidecars together with it while the database is in a consistent state. Export from the copy, not a live project being updated by Draw Things. ComfyThings opens the database read-only.

## Quick start

From the directory containing `ComfyThings.py`, with a copied database named `MyProject.sqlite3`:

```bash
# Default export: 8-bit still PNGs, encoded MP4s, and video workflow PNGs
python3 ComfyThings.py MyProject

# Choose a separate destination
python3 ComfyThings.py MyProject MyProject_Exports

# Ordinary still PNGs only
python3 ComfyThings.py MyProject MyProject_Images --images-only

# Request 16-bit still PNGs explicitly
python3 ComfyThings.py MyProject MyProject_16bit --images-only --bit-depth 16

# Preview the export plan without writing media
python3 ComfyThings.py MyProject --dry-run
```

The required `database` argument accepts a project name, a `.sqlite3` filename, or a path to the database. The optional second positional argument is the output directory; if omitted, the project stem is used in the current working directory.

**Defaults:** 8-bit RGB PNG for ordinary stills; MP4 using H.264 CRF18 and AAC when audio exists; video workflow PNGs included. `--bit-depth 16` is optional and **does not change the default**.

## How conversion works

1. **Reads Draw Things history and tensor records.** Source database access is read-only. Image tensor associations must be supported by the stored history and metadata; ambiguous associations are not guessed.
2. **Decodes stored FPZIP image tensors.** Outputs full-resolution RGB PNGs, with no arbitrary pixel replacement or resizing to hide invalid source geometry.
3. **Builds ComfyUI metadata.** Each generated PNG embeds ComfyUI `workflow` and `prompt` information, reconstruction notes, available original generation fields, and source-history provenance. No separate workflow JSON file is needed.
4. **Identifies model families conservatively.** ComfyThings includes an embedded Draw Things model catalog and can query Civitai when additional model identification is warranted and network access is available. Uncertain classifications are identified as such rather than assigned unsupported model architectures.
5. **Exports videos when enabled.** Video clips are encoded using FFmpeg. First-frame workflow PNGs are produced where available. Video workflows may be connected or informational, depending on the model architecture and supported nodes.
6. **Verifies and resumes output.** Existing files are checked against their recorded provenance and relevant export settings. Conflicting or unverified content is not silently overwritten.

### Image-tensor recovery modes

ComfyThings conservatively handles several Draw Things history layouts:

- **Missing-ID recovery (enabled by default):** allows narrowly constrained tensor associations when one stored slot has an absent tensor ID and the other supplies a valid image, including carefully limited later history reuse. Use `--no-recover-empty-slots` to disable it.
- **Editing-sequence recovery (opt-in):** `--recover-editing-sequences` permits a strict multi-slot append-successor pattern only when consecutive history records and supporting metadata establish the relationship. This is **not** a blanket “last tensor wins” rule.
- **Recovery-only exports:** `--recovery-only` limits output to the missing-ID recovery category; `--editing-recovery-only` limits output to the opt-in editing-sequence category. These two modes cannot be combined.

## What's new in v1.01

Compared with the previous full release, **v1.00**, v1.01 adds or expands:

- **Architecture-aware ComfyUI workflow reconstruction**, including model-family notes and more useful connected graphs where architecture support permits.
- **Conservative tensor association and recovery** for additional valid Draw Things history layouts. Missing-ID recovery is enabled by default; editing-sequence recovery is available as an explicit opt-in.
- **8-bit RGB PNG as the default ordinary still-image format**, while retaining optional full 16-bit PNG output.
- **Expanded video export controls:** MP4, MOV, MKV, and WebM containers; configurable encoding-quality profiles; per-clip frame PNGs; optional standalone WAV audio; and individual video workflow PNGs.
- **Resumable, verified media exports**, with additional protection against mismatched destination files and incompatible settings.
- **Customizable output filenames**, progress reporting, and additional command-line controls.

These are changes delivered in the full **v1.01** release.

## Command-line reference

### General and image options

| Option | Behavior |
|---|---|
| `database` | Required source `.sqlite3` database path or filename stem. |
| `output_dir` | Optional destination directory. Defaults to source stem in current directory. |
| `--images-only` | Export ordinary still PNGs only; disable all video-related output. |
| `--videos-only`, `--no-images` | Suppress ordinary still PNGs, retaining configured video outputs. |
| `--bit-depth {8,16}` | RGB PNG output precision; **8-bit default**. |
| `--png-filter {adaptive,none}` | Lossless scanline filtering; `adaptive` default. |
| `--compression-level N` | PNG zlib compression level 0–9; **9 default**. |
| `--filename-prefix PREFIX` | Customize generated image/video basename prefix; `DrawThings` default. |
| `--limit N` | Limit generated still-image records processed; `0` means unlimited. |
| `--dry-run` | Show the export plan without writing media files. |
| `--utc` | Use UTC rather than local system time in timestamp-based filenames. |
| `--no-set-birthtime` | Do not attempt to set macOS Created timestamps. |
| `--no-progress` | Hide animated progress bars and spinner. |
| `--verbose`, `-v` | Print more processing details. |
| `--verify`, `--no-verify` | Enable/disable post-write PNG pixel-stream verification; enabled by default. |
| `--no-integrity-check` | Skip SQLite `PRAGMA quick_check`; intended only for trusted copies. |
| `--version`, `--help` | Show version or command-line help. |

### Recovery options

| Option | Behavior |
|---|---|
| `--recover-empty-slots` | Enable strictly validated missing-ID recovery; **enabled by default**. |
| `--no-recover-empty-slots` | Disable missing-ID recovery. |
| `--recovery-only` | Export only eligible missing-ID still-image recoveries, without videos. |
| `--recover-editing-sequences` | Opt in to guarded multi-slot editing-history recovery; **off by default**. |
| `--editing-recovery-only` | Export only newly eligible editing-sequence histories; automatically opts in to the rule. |

### Video options

| Option | Behavior |
|---|---|
| `--no-videos` | Do not encode video files; separate workflows are also suppressed unless explicitly enabled. |
| `--video-workflows` | Explicitly export separate first-frame video workflow PNGs. |
| `--no-video-workflows` | Suppress separate video workflow PNGs. |
| `--video-workflows-only` | Export only video workflow PNGs; no ordinary stills, video files, extra frames, or WAVs. |
| `--video-export-frames` | Export each video frame as a PNG in its clip directory. |
| `--video-export-audio` | Export standalone per-clip WAV files in addition to any encoded-video audio. |
| `--video-format {mp4,mov,mkv,webm}` | Select the video container; MP4 is the normal default. |
| `--video-quality {default,high,professional,lossless}` | Select codec and quality profile. |

The standard video profile is **H.264 CRF18**, 8-bit `yuv420p`, with AAC audio if available. `high` uses H.264 CRF14; `professional` uses 10-bit HEVC; `lossless` uses FFV1 with 16-bit RGB output in MKV and floating-point PCM audio. WebM uses VP9/Opus. Compatibility depends on the selected container and encoder support in your FFmpeg installation.

## Examples

### Selected image exports

```bash
# A small, bounded export into a separate directory
python3 ComfyThings.py MyProject MyProject_Sample --images-only --limit 5

# Export 16-bit images without changing any defaults
python3 ComfyThings.py MyProject MyProject_16bit --images-only --bit-depth 16

# Use a trusted database copy without the potentially lengthy integrity check
python3 ComfyThings.py MyProject MyProject_Images --images-only --no-integrity-check

# Enable the optional editing-sequence recovery during a normal export
python3 ComfyThings.py MyProject MyProject_Exports --recover-editing-sequences

# Export only images eligible under that recovery rule
python3 ComfyThings.py MyProject MyProject_Editing --editing-recovery-only

# Export only the strict missing-ID recovery category
python3 ComfyThings.py MyProject MyProject_Recovered --recovery-only

# Customize output filenames and hide animated progress
python3 ComfyThings.py MyProject MyProject_Custom --filename-prefix Rendered --no-progress
```

### Videos and video-related media

```bash
# Video files with their default workflow PNGs; no ordinary stills
python3 ComfyThings.py MyProject --videos-only

# Export only the video workflow PNGs
python3 ComfyThings.py MyProject --video-workflows-only

# Alternate output containers
python3 ComfyThings.py MyProject --videos-only --video-format mov
python3 ComfyThings.py MyProject --videos-only --video-format mkv
python3 ComfyThings.py MyProject --videos-only --video-format webm

# Higher-quality encoded video
python3 ComfyThings.py MyProject --videos-only --video-quality high

# Lossless FFV1 in MKV
python3 ComfyThings.py MyProject --videos-only --video-quality lossless

# Export additional video frames and standalone audio
python3 ComfyThings.py MyProject --videos-only --video-export-frames --video-export-audio

# Media assets without encoding the video itself
python3 ComfyThings.py MyProject --no-images --no-videos --video-workflows --video-export-frames --video-export-audio
```

## Output layout

With the default filename prefix, output resembles:

```text
MyProject_Exports/
├── DrawThings_YYYYMMDD_HHMMSS_microseconds.png
└── videos/
    ├── DrawThings_YYYYMMDD_HHMMSS_microseconds.mp4
    └── DrawThings_YYYYMMDD_HHMMSS_microseconds/
        ├── DrawThings_YYYYMMDD_HHMMSS_microseconds_workflow.png
        ├── DrawThings_YYYYMMDD_HHMMSS_microseconds_000001.png  # optional frames
        └── DrawThings_YYYYMMDD_HHMMSS_microseconds.wav          # optional audio
```

Files and per-clip folders are generated only for the media requested and available in the source. Optional video frames are stored within their clip folders rather than filling the main export directory. The video workflow is included in the first-frame workflow PNG; not every architecture produces an executable video graph.

## Performance and safety

**Large exports may run for a substantial amount of time.** The source-database integrity check can be expensive on large projects, and full-resolution FPZIP decoding, PNG compression, output verification, and video encoding add processing time. Runtime depends on database size, image dimensions, storage performance, and chosen encoding settings.

- For a small amount of work, start with `--images-only --limit 5` in a **new destination**. This bounds ordinary still-image work but does not predict full video-export time.
- Default PNG zlib compression uses level 9, prioritizing smaller lossless files over speed. Changing compression affects file size and runtime, not image precision.
- Use `--no-integrity-check` only for a trusted database copy; it skips a database-level check, **not** image decoding or export verification.
- Keep separate output directories for differing settings, especially **8-bit versus 16-bit** exports. Do not rely on resuming one bit depth over files created at the other depth.
- Source image processing is **sequential**: ComfyThings processes one image tensor at a time, not images in parallel.
- Resume checks protect existing outputs; the exporter rejects conflicting provenance rather than silently treating unrelated files as its own.
- Invalid source data is reported instead of replaced with fabricated pixels or resized to misleading dimensions.

## Known limitations and troubleshooting

**Damaged source tensors.** FPZIP input can occasionally contain nonfinite numbers or invalid tensor geometry. Those images cannot always be rendered faithfully; ComfyThings reports failures rather than inventing data.

**Unsupported or unknown architectures.** ComfyUI workflow reconstruction is best-effort. A model may need compatible weights, installed nodes, or manual adjustments. Unsupported video architectures may produce informational Note-only workflows instead of connected executable graphs.

**Video playback compatibility.** MP4 is the standard output. MOV, MKV, WebM, and alternate quality profiles have not all been comprehensively validated with every macOS playback application or FFmpeg build.

**Draw Things round-trip compatibility.** Available source-generation information is retained where supported, but reimporting every exported file into Draw Things is not guaranteed.

**Orphaned video records.** Some projects may contain clip headers without corresponding frame histories. ComfyThings does not invent frames from a header's claimed frame count.

**Missing dependencies.** If `numpy`, `fpzip`, or `certifi` cannot be imported, install `requirements.txt` into the same Python environment used to run the script. For video encoding, ensure `ffmpeg` and `ffprobe` are installed and available on `PATH`.

**No exported files appear.** Check the destination shown in the console, whether `--dry-run` is enabled, and whether the chosen mode suppresses the requested media type. Use `--verbose` for more detail.

**Export files already exist.** ComfyThings verifies ownership and settings before reuse. If you're changing output precision, naming, or workflow options, choose a new destination.

---

**ComfyThings v1.01** — stable release for macOS. This README is the complete public user guide, release summary, and limitations reference for v1.01.
