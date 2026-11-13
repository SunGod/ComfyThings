<image src="images/ComfyThings_banner_1672x308.png" alt="ComfyThings_banner_1672x308.png">
   
# ComfyThings

**A Draw Things-to-ComfyUI image, video, workflow and metadata bulk exporter**  

**Note:** If you want to convert existing PNGs that you previously exported manually from Draw Things, use [Draw Things-to-ComfyUI PNG Metadata Converter](https://github.com/SunGod/DrawThings-to-ComfyUI-PNG-Metadata-Converter) instead.

**Platform:** macOS  
**Current release:** v1.02 (November 2026)

Draw Things keeps your generated images, videos, and generation history inside project databases. That's convenient while you're working in Draw Things, but not so convenient when you want your creations as individual files—or when you want to revisit them in ComfyUI.

**ComfyThings exports an entire Draw Things project in one run.** You get full-resolution PNG images with ComfyUI workflows built in, plus playable video files with their ComfyUI workflows embedded directly in the videos. Original generation details are preserved wherever available, so you can inspect and work with the results later without having to open the project database.

You don't have to export images one at a time, copy prompts by hand, or save a separate workflow JSON for every image. ComfyThings reads a **copy** of a Draw Things `.sqlite3` project database; it does not change the database or generate new images.

## Contents

- [What's Changed in v1.02 (Compared with v1.01)](#whats-changed-in-v102-compared-with-v101)
- [What you get](#what-you-get)
- [Requirements](#requirements)
- [Find and copy your Draw Things project on a Mac](#find-and-copy-your-draw-things-project-on-a-mac)
- [Install ComfyThings](#install-comfythings)
- [Export your first project](#export-your-first-project)
- [Use your exports in ComfyUI](#use-your-exports-in-comfyui)
- [More ways to export](#more-ways-to-export)
- [Complete command-line reference](#complete-command-line-reference)
- [Updating an older video from a workflow PNG](#updating-an-older-video-from-a-workflow-png)
- [Time, storage, and safety](#time-storage-and-safety)
- [Troubleshooting and limitations](#troubleshooting-and-limitations)

## What's Changed in v1.02 (Compared with v1.01)

In v1.01, ComfyThings already exported still-image PNGs with ComfyUI workflows embedded in them. It could also export videos, but their workflows were saved in separate first-frame PNG files. **Version 1.02 brings that same convenience to video exports:** the workflow is now stored inside the video itself.

Here's what changed:

- **Video workflows are now embedded by default.** Exported MP4, MOV, MKV, and WebM files contain ComfyUI `workflow` and `prompt` metadata. You can drag a supported video into ComfyUI to load its workflow without needing a separate PNG.
- **The original Draw Things generation details travel with the video.** In addition to the ComfyUI workflow, v1.02 embeds available source generation information and original history and clip records. Those records are compressed and integrity-checked.
- **Separate video workflow PNGs are no longer created automatically.** In v1.01 they were part of the default video export. In v1.02, use `--video-workflows` when you want those extra files, or `--video-workflows-only` when you want just the workflow PNGs.
- **More video models get connected ComfyUI workflows.** Reconstruction now supports LTX-2, LTX-2.3, and Wan 2.1 image-to-video through a shared system that handles different model architectures and generation tasks. When there is not enough information to build a meaningful graph, ComfyThings still provides an explanatory Note.
- **You can add workflows to videos exported previously.** The optional `--embed-existing-video-workflows` command matches older videos to their workflow PNGs and adds workflow and source metadata without re-encoding the video or audio.

**What hasn't changed:** Ordinary still images are still exported as **8-bit PNGs by default** (16-bit remains optional); ComfyThings still reads the Draw Things project database without changing it; and the existing video encoding quality settings remain the same. The `--no-video-workflows` flag has been removed because separate workflow PNGs are already disabled by default.

## What you get

**Images:** ComfyThings extracts generated still images at their recorded resolution and saves them as **8-bit RGB PNGs by default**. Each PNG includes a reconstructed ComfyUI workflow, its execution data, and available original Draw Things generation metadata. You can request **16-bit PNGs** if you prefer. Images are processed one at a time, not in parallel.

**Videos:** When a project contains video clips, ComfyThings exports playable **MP4 videos by default**, including audio if it exists in the source. It also supports MOV, MKV, and WebM, along with several quality settings. Each exported video contains its **ComfyUI workflow and original Draw Things source metadata**, including available generation and video-clip records. You do not need a companion workflow PNG to open a newly exported video's workflow.

**Optional extras:** You can choose to export every video frame as a PNG, a standalone WAV for each video with available audio, or a separate first-frame video workflow PNG. **Separate video workflow PNGs are *not* created by default** in v1.02.

ComfyThings reconstructs workflows from the information Draw Things stored. It supports connected workflows for several video architectures, including **LTX-2, LTX-2.3, and Wan 2.1 image-to-video**. When a graph cannot be reliably reconstructed, the exported metadata instead explains what was found. That way, the result remains useful without pretending to know settings or connections that were not recorded.

## Requirements

- **A Mac running macOS.** This release is intended for macOS, not Windows or Linux.
- **Python 3.9 or later** (available as `python3` in Terminal).
- The Python packages **numpy**, **fpzip**, and **certifi**.
- **FFmpeg and ffprobe** if you want to export or update videos. They are not needed for an images-only export.
- Enough free storage for the exported images and videos, and temporary files used during safe writes.
- A **copy of a Draw Things `.sqlite3` project database**.

## Find and copy your Draw Things project on a Mac

Draw Things stores project histories in SQLite database files, rather than keeping every generation as a separate image in a normal Finder folder. Here is how to get one ready for ComfyThings:

1. **Quit Draw Things** before making a copy. This helps ensure the database isn't changing while you copy it.
2. In **Finder**, open the **Go** menu and select **Go to Folder…** (or press **Shift-Command-G**).
3. Paste this common Draw Things data location and press Return:

   ```text
   ~/Library/Containers/com.liuliu.draw-things/Data/Documents/
   ```

4. Look for your project's **`.sqlite3`** file. The folder's display name can vary by macOS version; projects you moved to another drive will be wherever you stored them.
5. **Copy, don't move,** the database to a working directory—for example, `~/ComfyThings`. If there are matching `-wal` or `-shm` companion files, copy them with the database after Draw Things has quit. Keep your original project untouched.

For the examples below, assume you've copied a project named **`MyProject.sqlite3`** into `~/ComfyThings` and also put the ComfyThings files there. The examples are deliberately generic: use your project's *actual* filename.

## Install ComfyThings

Download the release and put **`ComfyThings.py`**, **`README.md`**, and **`requirements.txt`** together in a folder. The included `README.txt` is the same guide in plain-text form, if you prefer it.

Open Terminal and change to that folder:

```bash
cd ~/ComfyThings
```

Set up a Python environment and install the required packages:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

If you'll export videos, install FFmpeg. With [Homebrew](https://brew.sh/) already installed, run:

```bash
brew install ffmpeg
```

Confirm that ComfyThings starts:

```bash
python3 ComfyThings.py --version
python3 ComfyThings.py --help
```

The first command should report **v1.02**. On later Terminal visits, return to the folder and run `source .venv/bin/activate` before using ComfyThings.

## Export your first project

With `MyProject.sqlite3` in your current folder, the default command is:

```bash
python3 ComfyThings.py MyProject
```

You can leave off the `.sqlite3` extension. ComfyThings reads the project and creates an output directory called **`MyProject`** in your current directory. By default, it exports:

- **8-bit PNGs** for ordinary still images, with workflows embedded in each PNG.
- **MP4s** for video clips, with ComfyUI workflows and original generation metadata embedded in the video files.
- **Audio inside videos** when audio is available for those clips.

You can choose a different destination by adding it as the second argument:

```bash
python3 ComfyThings.py MyProject MyProject_Exports
```

If your project name or path has spaces, put it in quotes:

```bash
python3 ComfyThings.py "My Project.sqlite3" "My Project Exports"
```

You can also give an absolute path to a copied database:

```bash
python3 ComfyThings.py "/Volumes/Archive/Draw Things/MyProject.sqlite3" "MyProject_Exports"
```

### Where the files go

A typical default export looks like this:

```text
MyProject/
├── DrawThings_YYYYMMDD_HHMMSS_microseconds.png
├── DrawThings_YYYYMMDD_HHMMSS_microseconds.png
└── videos/
    ├── DrawThings_YYYYMMDD_HHMMSS_microseconds.mp4
    └── DrawThings_YYYYMMDD_HHMMSS_microseconds.mp4
```

The names use generation timestamps, including microseconds. Video clips are grouped as videos—not exported into hundreds of loose PNG frames unless you explicitly request those frames.

If you enable the extra video outputs, each clip can also have its own folder under `videos/`:

```text
videos/
├── DrawThings_YYYYMMDD_HHMMSS_microseconds.mp4
└── DrawThings_YYYYMMDD_HHMMSS_microseconds/
    ├── DrawThings_YYYYMMDD_HHMMSS_microseconds_workflow.png  (optional)
    ├── DrawThings_YYYYMMDD_HHMMSS_microseconds_000001.png    (optional frame)
    ├── DrawThings_YYYYMMDD_HHMMSS_microseconds_000002.png    (optional frame)
    └── DrawThings_YYYYMMDD_HHMMSS_microseconds.wav           (optional audio)
```

Only requested and available media are written. The clip folders do not need to exist for the normal MP4-only video output.

## Use your exports in ComfyUI

**For still images:** Drag an exported PNG into the ComfyUI canvas. ComfyUI can read the embedded workflow from the PNG, showing the reconstructed nodes and settings. The image is still a normal PNG you can view or edit with other applications.

**For videos:** Drag an exported MP4 into a ComfyUI installation that supports importing workflow metadata from video files. ComfyThings stores the standard `workflow` and `prompt` information directly in the video, so no separate workflow PNG is needed. The video itself also remains playable in ordinary video players.

A reconstructed workflow is a starting point for examining or continuing your generation work. Depending on the model and task, you may need to choose local model files or provide an original input image. For an image-to-video workflow, the original conditioning image is **not** inferred from the video's first generated frame: the `LoadImage` input is left for you to fill.

If ComfyThings does not have a connected workflow template for a particular generation, you'll still see a readable Note with the source settings rather than an invented graph.

## More ways to export

The normal command is enough for most people. These examples cover some common variations.

### Only images, or higher-precision PNGs

```bash
# Export only still images—no video processing
python3 ComfyThings.py MyProject MyImages --images-only

# Export 16-bit still PNGs instead of the default 8-bit
python3 ComfyThings.py MyProject MyImages16 --images-only --bit-depth 16

# Export a small sample of five still images
python3 ComfyThings.py MyProject SampleImages --images-only --limit 5
```

### Videos and optional companion media

```bash
# Export videos, but not ordinary still images
python3 ComfyThings.py MyProject VideoExports --videos-only

# Export videos and also save a separate first-frame workflow PNG for each clip
python3 ComfyThings.py MyProject VideoExports --videos-only --video-workflows

# Export only separate video workflow PNGs, without encoding videos
python3 ComfyThings.py MyProject VideoWorkflowPNGs --video-workflows-only

# Export video frames and standalone WAVs in addition to the video files
python3 ComfyThings.py MyProject VideoExports --videos-only --video-export-frames --video-export-audio

# Use a different video container
python3 ComfyThings.py MyProject VideoExports --videos-only --video-format mov

# Higher-quality H.264 output
python3 ComfyThings.py MyProject VideoExports --videos-only --video-quality high

# Lossless video (MKV is chosen automatically)
python3 ComfyThings.py MyProject VideoExports --videos-only --video-quality lossless
```

### Preview and other useful controls

```bash
# Preview what would be exported; no media is written
python3 ComfyThings.py MyProject --dry-run

# Use a custom filename prefix
python3 ComfyThings.py MyProject MyExports --filename-prefix MyRenders

# Skip the SQLite quick integrity check on a database copy you already trust
python3 ComfyThings.py MyProject MyExports --no-integrity-check

# Include the optional guarded editing-history recovery
python3 ComfyThings.py MyProject MyExports --recover-editing-sequences
```

The database integrity check and history scan can take a noticeable amount of time on large projects, **even in dry-run mode**. For a very large project, don't assume a preview is instantaneous.

## Complete command-line reference

These are **all command-line arguments in v1.02**. Options without an explicit value are switches: add the flag to enable the behavior.

### Project, images, and general options

| Argument or flag | What it does |
| --- | --- |
| `database` | The Draw Things project to read. Give the `.sqlite3` filename, its path, or its name without the extension. Required for normal exports; in existing-video mode this argument is instead the video file or directory. |
| `output_dir` | Optional output folder. Defaults to the project filename without `.sqlite3`, created under the current working directory. Not used in existing-video mode. |
| `--images-only` | Export ordinary still-image PNGs only. Disables all video-related outputs. |
| `--videos-only`, `--no-images` | Skip ordinary still images while retaining video exports and their chosen options. These two names do the same thing. |
| `--bit-depth {8,16}` | PNG color precision. **8** is the default; choose **16** for 16-bit RGB PNGs. |
| `--png-filter {adaptive,none}` | Choose PNG scanline filtering. **adaptive** is the default; both choices preserve pixel values. |
| `--compression-level N` | PNG compression from **0** (least) to **9** (most); default **9**. It changes size and compression time, not image quality. |
| `--filename-prefix PREFIX` | Change the `DrawThings` prefix used for generated filenames and clip folders. |
| `--limit N` | Process only the first **N** eligible ordinary still images; **0** means all. Since video clips must remain complete, use this with `--images-only` or `--no-videos`, not with default encoded-video export. |
| `--dry-run` | Show which outputs would be made without writing images or videos. Also works with existing-video embedding. |
| `--utc` | Use UTC rather than your Mac's local time when producing timestamp-based filenames. |
| `--no-set-birthtime` | Do not try to set the exported file's macOS **Date Created** from its generation history. |
| `--no-progress` | Turn off animated progress reporting and print simpler phase and summary output. |
| `--verbose`, `-v` | Print additional details for individual images or video clips; disables the live progress display. |
| `--verify` | Enable the additional post-write PNG pixel-stream check (already enabled by default). |
| `--no-verify` | Skip that optional *new-file* deep pixel-stream check. Existing-file resume validation still checks prior output. |
| `--no-integrity-check` | Skip the initial full SQLite `PRAGMA quick_check`. This can save time on a trusted database copy, but removes that pre-export check. |
| `--version` | Print the ComfyThings version and exit. |
| `--help`, `-h` | Show the built-in help and exit. |

### Recovering images from unusual history layouts

Draw Things can retain multiple image tensors for an editing history, or leave references that need careful interpretation. ComfyThings takes a conservative approach rather than guessing which image to export.

| Flag | What it does |
| --- | --- |
| `--recover-empty-slots` | Enable narrowly validated missing-tensor-ID recovery. **On by default**. |
| `--no-recover-empty-slots` | Turn off that recovery and use strict ordinary tensor selection. |
| `--recovery-only` | Export only images eligible under the missing-ID recovery rule, with no video export. |
| `--recover-editing-sequences` | Opt in to additional, strictly checked editing-sequence recovery. **Off by default**. |
| `--editing-recovery-only` | Export only images eligible under the additional editing-sequence rule. It automatically enables `--recover-editing-sequences`; it cannot be combined with `--recovery-only`. |

Most users won't need to change these options. The default missing-ID recovery is already enabled, and other ambiguous situations are left unresolved rather than filled in with guesses.

### Video options

| Flag | What it does |
| --- | --- |
| `--no-videos` | Do not encode videos. You can still explicitly request separate video workflow PNGs or frames using their own flags. |
| `--video-workflows` | **Also** save a standalone first-frame workflow PNG for each video, in addition to other requested media. Off by default. |
| `--video-workflows-only` | Export only first-frame video workflow PNGs: no stills, videos, extra frames, or WAVs. |
| `--video-export-frames` | Save all frames of each video as numbered PNGs in the corresponding clip folder. |
| `--video-export-audio` | Save an additional clip-named WAV when audio is available, alongside any audio in the encoded video. |
| `--video-format {mp4,mov,mkv,webm}` | Choose the video container. Default: **mp4**, except lossless output defaults to **mkv**. |
| `--video-quality {default,high,professional,lossless}` | Choose the encoding profile. See below. |
| `--embed-existing-video-workflows` | Instead of opening a database, update an existing video—or a directory of videos—from matching workflow PNGs. This is an optional utility for older exports. |
| `--recursive` | With `--embed-existing-video-workflows` in directory mode, include video files in subdirectories. Not used in normal project export mode. |

**Video quality profiles:**

| Quality | Output behavior |
| --- | --- |
| `default` | **H.264 CRF 18**, 8-bit `yuv420p`; AAC audio when available. |
| `high` | **H.264 CRF 14** for higher-quality lossy output. |
| `professional` | **10-bit HEVC** output. |
| `lossless` | **16-bit FFV1 video and floating-point PCM audio** in MKV. |

WebM uses a VP9/Opus profile where supported. Not every codec/profile combination is valid in every container; ComfyThings checks your selection before starting the export.

## Updating an older video from a workflow PNG

This is a **separate, optional operation**. You usually won't need it when exporting a new Draw Things project with v1.02.

If you have a video produced earlier and a corresponding ComfyThings workflow PNG, put them together with matching filename stems—for example, `Video001.mp4` and `Video001_workflow.png`. ComfyThings also recognizes `Video001.png`, or a workflow PNG in the corresponding clip-named subfolder.

For one video:

```bash
python3 ComfyThings.py --embed-existing-video-workflows Video001.mp4
```

For all video files in a folder:

```bash
python3 ComfyThings.py --embed-existing-video-workflows MyVideos
```

To include nested folders, add `--recursive`. To see which videos can be updated without changing files, add `--dry-run`.

The utility reads the original workflow and generation information from the PNG, then embeds them in the matched MP4, MOV, MKV, or WebM **without re-encoding the video or audio**. It can add source metadata to a video that already has a workflow, and can upgrade certain informational-only ComfyThings workflows when enough original metadata is present. It will not silently replace an unrelated workflow. It leaves the PNG untouched and reports which files were updated, skipped, or couldn't be updated, with reasons.

If the video has no useful generation metadata and you don't have its matching workflow PNG, ComfyThings cannot reconstruct the missing generation settings from the video pixels alone.

## Time, storage, and safety

**Allow time for large projects.** Before exporting, ComfyThings checks the copied SQLite database's integrity (unless disabled), scans its generation histories, and identifies image tensors. Full-resolution image decoding, PNG compression and verification, and video encoding can take considerably longer than just copying files. The time depends on your project size, image dimensions, video lengths, compression settings, and Mac/storage performance. There is no fixed per-project estimate.

**Your Draw Things database is not modified.** ComfyThings reads it in read-only mode. It processes image tensors one at a time, writes to temporary files, validates the result, and then publishes completed outputs. Existing exports are checked before reuse or repair; ComfyThings will not intentionally replace a different file merely because it shares a filename.

**Video metadata updates are not video re-encodes.** Adding a workflow to an existing video still requires copying the video container to a temporary file. Larger videos therefore take longer, and enough temporary free space is needed for at least the video being updated.

**Original generation metadata is retained.** Output PNGs and videos can contain your prompts, model names, timestamps, and other recorded generation settings, as well as technical source-history records. The separate project database is not required to load an embedded ComfyUI workflow.

## Troubleshooting and limitations

**`python3` or a Python package is missing.** Confirm Python is installed, activate `.venv`, and run `python3 -m pip install -r requirements.txt` from the folder containing `requirements.txt`.

**`ffmpeg` or `ffprobe` is missing.** Install FFmpeg (`brew install ffmpeg`) for video operations. An `--images-only` export does not require FFmpeg.

**The project database cannot be found.** Check the exact `.sqlite3` filename. If you are not in the folder containing the copied database, pass its full path in quotes.

**The export seems slow before producing files.** A large database's integrity check and history scan can take time before the first PNG appears. You can omit the integrity check with `--no-integrity-check` when working from a database copy you trust.

**Some images cannot be exported.** A source tensor can be missing, damaged, or ambiguously associated with a Draw Things history record. ComfyThings reports these cases rather than inventing pixels or choosing an uncertain image.

**An imported workflow contains only a Note.** The model or generation task may not have a supported connected workflow template, or sufficient source details may be unavailable. The Note preserves useful information about the original generation.

**A video workflow asks for an image.** For image-to-video workflows, choose the original input image in the `LoadImage` node. ComfyThings does not substitute the video's first generated frame for the original input.

**An existing video cannot be updated.** The optional embedding command needs a matching workflow PNG containing usable metadata. The terminal's **Videos not updated** section explains missing matches, ambiguous matches, unreadable metadata, and other reasons.

**An export directory already contains files.** ComfyThings can verify and resume compatible exports, but it may reject files that don't match the expected source or settings. Use a fresh destination if you want an independent conversion rather than a continuation of an earlier one.

---

**ComfyThings v1.02** · macOS · Draw Things project `.sqlite3` to ComfyUI-ready PNGs and workflow-bearing videos
