#!/usr/bin/env python3
"""ComfyThings v1.01 — Draw Things database exporter.

Usage:
    python3 ComfyThings.py Default
    python3 ComfyThings.py Default --video-format webm
    python3 ComfyThings.py Default --videos-only
    python3 ComfyThings.py Default --video-workflows-only

Works on a copied Draw Things .sqlite3 project in read-only mode. Exports
still-image PNGs with ComfyUI workflows and, by default, MP4 videos with
embedded audio plus one first-frame video-workflow PNG per clip. MP4, MOV,
MKV, and WebM are available; individual frames and WAVs are optional.

Requires Python, numpy and fpzip for image export; certifi supports Civitai
HTTPS; default MP4 export additionally requires FFmpeg and ffprobe on PATH.
"""
from __future__ import annotations

import argparse
import csv
import copy
import ctypes
from datetime import datetime
import hashlib
import math
import io
import json
import os
from pathlib import Path
import re
import shutil
import struct
import sys
import tempfile
import textwrap
import xml.etree.ElementTree as ET
import zlib

VERSION = "1.01"
PNG_SIG = b"\x89PNG\r\n\x1a\n"
TEXT_TYPES = (b"tEXt", b"zTXt", b"iTXt")
XMP_KEY = "XML:com.adobe.xmp"
MAX_TEXT = 32 * 1024 * 1024
BUF = 1024 * 1024
BAK_RE = re.compile(r"\.bak\d+\.png$", re.I)
# Short names encode the six-digit filesystem creation microseconds.
# Optional _1/_2/... suffixes distinguish truly identical creation timestamps.
SHORT_NAME_RE = re.compile(
    r"^DrawThings_\d{8}_\d{6}_\d{6}(?:_[1-9]\d*)?$", re.I)
FILETIME_EPOCH = 116444736000000000  # 100 ns ticks between 1601 and 1970

SAMPLE_MAP = {
    "euler": "euler", "euler a": "euler_ancestral", "euler ancestral": "euler_ancestral",
    "euler a substep": "euler_ancestral", "heun": "heun", "plms": None,
    "lms": "lms", "ddim": "ddim", "tcd": "lcm", "lcm": "lcm",
    "dpm++ 2m": "dpmpp_2m", "dpm++ 2s a": "dpmpp_2s_ancestral",
    "dpm++ sde": "dpmpp_sde", "dpm++ 2m sde": "dpmpp_2m_sde",
    "dpm++ 3m sde": "dpmpp_3m_sde", "unipc": "uni_pc", "uni pc": "uni_pc",
    "dpm2": "dpm_2", "dpm2 a": "dpm_2_ancestral",
}


class ConversionError(Exception):
    pass


def json_text(obj):
    return json.dumps(obj, ensure_ascii=True, separators=(",", ":"))


def _text_value(tag, payload):
    """Return keyword, content from un/compressed PNG text chunks."""
    key, sep, tail = payload.partition(b"\0")
    if not sep or not 1 <= len(key) <= 79:
        return None, None
    try:
        keyword = key.decode("latin-1")
        if tag == b"tEXt":
            return keyword, tail.decode("latin-1")
        if tag == b"zTXt":
            if len(tail) < 2 or tail[0] != 0:
                return keyword, None
            return keyword, _bounded_inflate(tail[1:]).decode("latin-1")
        if tag == b"iTXt":
            if len(tail) < 2:
                return keyword, None
            flag, method = tail[:2]
            lang, sep, rest = tail[2:].partition(b"\0")
            if not sep:
                return keyword, None
            translated, sep, value = rest.partition(b"\0")
            if not sep or method != 0 or flag not in (0, 1):
                return keyword, None
            if flag:
                value = _bounded_inflate(value)
            return keyword, value.decode("utf-8")
    except (UnicodeError, zlib.error, ConversionError):
        return keyword, None
    return None, None


def _bounded_inflate(data):
    dec = zlib.decompressobj()
    raw = dec.decompress(data, MAX_TEXT + 1)
    if len(raw) > MAX_TEXT or dec.unconsumed_tail:
        raise ConversionError("compressed metadata exceeds size limit")
    raw += dec.flush(MAX_TEXT + 1 - len(raw))
    if len(raw) > MAX_TEXT or not dec.eof:
        raise ConversionError("invalid or oversized compressed metadata")
    return raw


def scan_png(path):
    """Read PNG structure/check CRC without decoding or recompressing pixels."""
    chunks = []
    texts = {}
    xmp_payloads = []
    idat = hashlib.sha256()
    dimensions = None
    seen_idat = False
    seen_iend = False
    with open(path, "rb") as stream:
        if stream.read(8) != PNG_SIG:
            raise ConversionError("invalid PNG signature")
        while True:
            pos = stream.tell()
            header = stream.read(8)
            if not header:
                break
            if len(header) != 8:
                raise ConversionError("truncated PNG chunk header")
            length, kind = struct.unpack(">I4s", header)
            if not all((65 <= c <= 90 or 97 <= c <= 122) for c in kind):
                raise ConversionError("invalid PNG chunk type")
            crc = zlib.crc32(kind)
            captured = bytearray() if kind in TEXT_TYPES and length <= MAX_TEXT else None
            first = bytearray() if kind == b"IHDR" and length == 13 else None
            remaining = length
            while remaining:
                part = stream.read(min(remaining, BUF))
                if not part:
                    raise ConversionError("truncated PNG chunk data")
                crc = zlib.crc32(part, crc)
                if captured is not None:
                    captured.extend(part)
                if first is not None:
                    first.extend(part)
                if kind == b"IDAT":
                    idat.update(part)
                remaining -= len(part)
            expected = stream.read(4)
            if len(expected) != 4:
                raise ConversionError("truncated PNG CRC")
            if struct.unpack(">I", expected)[0] != crc & 0xffffffff:
                raise ConversionError("PNG CRC mismatch: " + kind.decode("ascii"))
            keyword = None
            if captured is not None:
                keyword, val = _text_value(kind, bytes(captured))
                if keyword is not None and val is not None:
                    texts.setdefault(keyword, []).append(val)
                if keyword == XMP_KEY:
                    xmp_payloads.append(val)
            if kind == b"IHDR":
                if chunks or first is None:
                    raise ConversionError("invalid PNG IHDR")
                w, h = struct.unpack(">II", first[:8])
                if not w or not h:
                    raise ConversionError("invalid image dimensions")
                dimensions = (w, h)
            if kind == b"IDAT":
                seen_idat = True
            chunks.append((pos, 12 + length, kind, keyword))
            if kind == b"IEND":
                if length != 0:
                    raise ConversionError("invalid IEND chunk")
                seen_iend = True
                tail_len = len(stream.read())
                if tail_len:
                    raise ConversionError("unexpected data after PNG IEND")
                break
    if not dimensions or not seen_idat or not seen_iend:
        raise ConversionError("missing PNG IHDR, IDAT or IEND")
    return {"chunks": chunks, "texts": texts, "xmp": xmp_payloads,
            "dims": dimensions, "idat_sha256": idat.hexdigest()}


def drawthings_json(scan):
    """Find original Draw Things JSON in XMP EXIF UserComment."""
    json_objects = []
    for item in scan["xmp"]:
        if not item:
            continue
        try:
            root = ET.fromstring(item)
        except ET.ParseError:
            continue
        for node in root.iter():
            if node.tag == "{http://ns.adobe.com/exif/1.0/}UserComment":
                for candidate in ("".join(node.itertext()).strip(),):
                    try:
                        value = json.loads(candidate)
                        if isinstance(value, dict) and (
                                ("model" in value and "sampler" in value) or
                                ("v2" in value and isinstance(value["v2"], dict))):
                            json_objects.append(value)
                    except (json.JSONDecodeError, ValueError):
                        pass
    if not json_objects:
        raise ConversionError("no Draw Things JSON in XMP exif:UserComment")
    return json_objects[0]


def _as_int(value, name, low, high):
    if isinstance(value, bool):
        raise ConversionError("invalid " + name)
    try:
        num = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ConversionError("missing/invalid " + name)
    if num < low or num > high:
        raise ConversionError("out-of-range " + name)
    return num


def _as_float(value, name, low, high):
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ConversionError("missing/invalid " + name)
    if not low <= num <= high:
        raise ConversionError("out-of-range " + name)
    return num


def sampler_mapping(label, warnings):
    if not isinstance(label, str) or not label.strip():
        raise ConversionError("Draw Things sampler is missing")
    lower = " ".join(label.lower().replace("_", " ").split())
    suffix = None
    for ending, schedule in ((" karras", "karras"), (" exponential", "exponential"),
                             (" trailing", "normal"), (" ays", "normal"),
                             (" substep", "normal")):
        if lower.endswith(ending):
            lower = lower[:-len(ending)].strip()
            suffix = ending.strip()
            scheduler = schedule
            break
    else:
        scheduler = "normal"
    if lower == "euler a" and suffix == "substep":
        lower = "euler a"
    core = SAMPLE_MAP.get(lower, "unsupported")
    if core is None or core == "unsupported":
        raise ConversionError("unsupported Draw Things sampler: " + label)
    if suffix in ("trailing", "ays", "substep"):
        warnings.append("Draw Things '%s' schedule has no exact KSampler counterpart; used 'normal'" % suffix)
    if lower == "tcd":
        warnings.append("TCD is approximated by ComfyUI LCM; sampler algorithms may differ")
    return core, scheduler


class Graph:
    def __init__(self):
        self.nodes, self.links, self.api = [], [], {}

    def add(self, kind, title, pos, inputs, outputs, fields, widgets=None,
            *, frontend_only=False):
        nid = len(self.nodes) + 1
        ins = [{"name": name, "type": typ, "link": None} for name, typ in inputs]
        outs = [{"name": name, "type": typ, "links": None, "slot_index": i}
                for i, (name, typ) in enumerate(outputs)]
        n = {"id": nid, "type": kind, "pos": list(pos),
             "size": [350, 110 + max(0, len(fields) - 1) * 30],
             "flags": {}, "order": nid - 1, "mode": 0,
             "inputs": ins, "outputs": outs,
             "properties": {"Node name for S&R": kind},
             "widgets_values": widgets if widgets is not None else list(fields.values())}
        n["title"] = title
        self.nodes.append(n)
        if not frontend_only:
            self.api[str(nid)] = {"inputs": dict(fields), "class_type": kind, "_meta": {"title": title}}
        return nid

    def note(self, title, body, pos):
        """Add a native canvas-only Note with content-fitted dimensions.

        Note text and its intentional blank section separators are unchanged.
        The dimensions are estimates for ComfyUI's default Note widget, not
        coordinates for the original PNG image or executable KSampler graph.
        """
        node_id = self.add("Note", title, pos, [], [], {}, [body], frontend_only=True)
        self.nodes[node_id - 1]["size"] = list(_note_size(title, body))
        return node_id

    def connect(self, src, source_name, dst, dest_name):
        source = self.nodes[src-1]
        target = self.nodes[dst-1]
        outs = [i for i, o in enumerate(source["outputs"]) if o["name"] == source_name]
        ins = [i for i, o in enumerate(target["inputs"]) if o["name"] == dest_name]
        if len(outs) != 1 or len(ins) != 1:
            raise AssertionError("invalid graph port")
        o, i = outs[0], ins[0]
        typ = source["outputs"][o]["type"]
        if target["inputs"][i]["type"] != typ:
            raise AssertionError("mismatched graph socket type")
        if target["inputs"][i]["link"] is not None:
            raise AssertionError("duplicate graph connection")
        link_id = len(self.links) + 1
        self.links.append([link_id, src, o, dst, i, typ])
        source["outputs"][o]["links"] = (source["outputs"][o]["links"] or []) + [link_id]
        target["inputs"][i]["link"] = link_id
        self.api[str(dst)]["inputs"][dest_name] = [str(src), o]

    def finish(self):
        workflow = {"last_node_id": len(self.nodes), "last_link_id": len(self.links),
                    "nodes": self.nodes, "links": self.links, "groups": [],
                    "config": {}, "extra": {"ds": {"scale": 0.75, "offset": [60, 80]}},
                    "version": 0.4}
        validate_graph(workflow, self.api)
        return workflow, self.api


def validate_graph(w, a):
    ns = {n["id"]: n for n in w["nodes"]}
    if len(ns) != len(w["nodes"]) or len(w["links"]) != w["last_link_id"]:
        raise ConversionError("bad graph IDs")
    links = {ln[0]: ln for ln in w["links"]}
    if len(links) != len(w["links"]):
        raise ConversionError("duplicate graph link IDs")
    if set(a) != {str(i) for i in ns if ns[i]["type"] != "Note"}:
        raise ConversionError("API graph node mismatch")
    for node in ns.values():
        if node["type"] == "Note" and (node["inputs"] or node["outputs"]):
            raise ConversionError("informational Note has executable sockets")
    if not a and (not ns or any(node["type"] != "Note" for node in ns.values())):
        raise ConversionError("informational workflow must contain only Note nodes")
    for link in links.values():
        lid, source, outslot, target, inslot, typ = link
        if source not in ns or target not in ns:
            raise ConversionError("broken node link")
        try:
            src = ns[source]["outputs"][outslot]
            dst = ns[target]["inputs"][inslot]
        except IndexError:
            raise ConversionError("broken node port")
        if src["type"] != typ or dst["type"] != typ or lid not in (src["links"] or []) or dst["link"] != lid:
            raise ConversionError("inconsistent graph link")
        if a[str(target)]["inputs"].get(dst["name"]) != [str(source), outslot]:
            raise ConversionError("API link does not match canvas")



def _render(value):
    """Human-readable, unmodified recorded values (never reinterpret field units)."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)



def _profile_events(data):
    """Names of completed processing milestones as recorded in Draw Things XMP.

    Profile entries document observed events, not their exact implementation.
    Unrecognized profile structures are not grounds for assuming a second pass.
    """
    profile = data.get("profile")
    timings = profile.get("timings") if isinstance(profile, dict) else None
    if not isinstance(timings, list):
        return set()
    return {entry.get("name") for entry in timings
            if isinstance(entry, dict) and isinstance(entry.get("name"), str)}


def _hires_fix_plan(data, v2, width, height, main_denoise):
    """Plan a conservative standard-image two-pass graph; return (plan, reason).

    Require both metadata configuration and explicit recorded second-pass
    activity. Never infer a second sampler merely from stored/default settings.
    Does not guess a model architecture or treat stage2Steps as Hires Fix steps.
    """
    # The structured v2 record is authoritative when it supplies this flag;
    # a conflicting legacy field must not silently enable a second pass.
    enabled = (v2["hiresFix"] is True) if isinstance(v2.get("hiresFix"), bool) else (data.get("hires_fix") is True)
    if not enabled:
        return None, "Hires Fix not enabled in structured configuration"
    raw_w = v2.get("hiresFixWidth")
    raw_h = v2.get("hiresFixHeight")
    raw_strength = v2.get("hiresFixStrength", data.get("second_stage_strength"))
    try:
        if isinstance(raw_w, bool) or isinstance(raw_h, bool):
            raise ValueError("boolean resolution")
        first_w = int(raw_w)
        first_h = int(raw_h)
        if not (16 <= first_w < width and 16 <= first_h < height):
            return None, "first-pass dimensions are not smaller than the target dimensions"
        if first_w % 8 or first_h % 8 or width % 8 or height % 8:
            return None, "first-pass or target dimensions are not multiples of eight"
        strength = _as_float(raw_strength, "Hires Fix strength", 0, 1)
        if strength <= 0:
            return None, "Hires Fix strength is zero"
    except (ValueError, TypeError, ConversionError, OverflowError):
        return None, "Hires Fix dimensions or strength are missing/invalid"
    if main_denoise != 1.0:
        return None, "source-image denoising is used; original input is not embedded"
    events = _profile_events(data)
    necessary = {"sampling", "image_decoded", "second_pass_image_encoded", "second_pass_sampling"}
    if not necessary.issubset(events):
        return None, "recorded processing profile does not confirm a second sampling pass"
    # A different selected refiner can affect sampling in ways the basic
    # CheckpointLoaderSimple / KSampler graph cannot accurately represent.
    refiner = v2.get("refinerModel") or data.get("refiner")
    model = data.get("model") or v2.get("model")
    if refiner and refiner != model:
        return None, "a distinct refiner model is recorded; refiner behavior not reconstructed"
    if v2.get("controls"):
        return None, "control inputs are recorded and not available in the PNG"
    return {"first_width": first_w, "first_height": first_h,
            "target_width": width, "target_height": height, "strength": strength}, ""


def _meaningful_untranslated(data, v2, png_dims, *, hires_plan=None, ckpt_to_safetensors=False, changed=None):
    """Describe untranslated *recorded* settings without inferring DT internals.

    The configuration is not a record of what necessarily executed. `profile`
    event names are shown separately as recorded evidence, never as causal proof.
    """
    items = []
    if changed is None:
        changed = []

    def display_name(value, category, label):
        converted = _normalize_named_reference(value, category, ckpt_to_safetensors)
        if converted != value:
            changed.append((label, value, converted))
        return converted

    def add(title, pairs):
        items.append((title, [(key, _render(val)) for key, val in pairs]))

    if (v2.get("hiresFix") or data.get("hires_fix") is True) and hires_plan is None:
        add("Hires Fix configuration (not reconstructed)", [
            ("hiresFix", v2.get("hiresFix", data.get("hires_fix"))),
            ("hiresFixWidth", v2.get("hiresFixWidth")),
            ("hiresFixHeight", v2.get("hiresFixHeight")),
            ("hiresFixStrength", v2.get("hiresFixStrength", data.get("second_stage_strength"))),
        ])
    if hires_plan is not None:
        add("Hires Fix sampling differences (best-effort reconstruction)", [
            ("second-pass random noise", "Draw Things RNG state is not recorded; ComfyUI reuses recorded seed"),
            ("sampling equivalence", "ComfyUI's sampler and schedule may differ from Draw Things"),
            ("decode/encode", "vanilla ComfyUI VAE used; exact Draw Things processing may differ"),
        ])
    refiner = v2.get("refinerModel") or data.get("refiner")
    if refiner:
        add("Refiner configuration (not reconstructed)", [
            ("refinerModel", display_name(refiner, "refiner", "refinerModel")), ("refinerStart", v2.get("refinerStart", data.get("refiner_start"))),
            ("stage2Steps (recorded; purpose not assumed)", v2.get("stage2Steps")),
            ("stage2Guidance (recorded; purpose not assumed)", v2.get("stage2Guidance")),
            ("stage2Shift (recorded; purpose not assumed)", v2.get("stage2Shift")),
        ])
    upscaler = v2.get("upscaler") or data.get("upscaler")
    if upscaler:
        add("Upscaling configuration (not reconstructed)", [
            ("upscaler", display_name(upscaler, "upscaler", "upscaler")),
            ("upscalerScaleFactor (raw recorded value)", v2.get("upscalerScaleFactor")),
            ("stored PNG dimensions", "%d x %d" % png_dims),
        ])
    face = v2.get("faceRestoration") or data.get("face_restoration")
    if face:
        add("Face restoration configuration (not reconstructed)",
            [("faceRestoration", display_name(face, "face", "faceRestoration"))])
    if v2.get("tiledDiffusion"):
        add("Tiled diffusion configuration (not reconstructed)", [
            (key, v2.get(key)) for key in ("tiledDiffusion", "diffusionTileWidth",
                                         "diffusionTileHeight", "diffusionTileOverlap")])
    if v2.get("cfgZeroStar") or v2.get("cfgZeroInitSteps"):
        add("CFG Zero Star (not reconstructed)", [
            ("cfgZeroStar", v2.get("cfgZeroStar")),
            ("cfgZeroInitSteps", v2.get("cfgZeroInitSteps")),
        ])
    if v2.get("zeroNegativePrompt") or data.get("zero_negative_prompt"):
        add("Special negative-prompt conditioning (not reconstructed)", [
            ("zeroNegativePrompt", v2.get("zeroNegativePrompt", data.get("zero_negative_prompt"))),
        ])
    if v2.get("colorCalibration") not in (None, "none", "disabled"):
        add("Color calibration (not reconstructed)", [("colorCalibration", v2["colorCalibration"])])
    shift = data.get("shift", v2.get("shift"))
    if shift not in (None, 0, 0.0) or v2.get("resolutionDependentShift"):
        add("Model shift (no generic KSampler equivalent)", [
            ("shift", shift), ("resolutionDependentShift", v2.get("resolutionDependentShift")),
        ])
    if v2.get("guidanceEmbed") is not None or v2.get("speedUpWithGuidanceEmbed"):
        add("Embedded guidance settings (not reconstructed)", [
            ("guidanceEmbed", v2.get("guidanceEmbed")),
            ("speedUpWithGuidanceEmbed", v2.get("speedUpWithGuidanceEmbed")),
        ])
    if data.get("seed_mode") not in (None, ""):
        add("Draw Things seed mode (RNG may differ in ComfyUI)",
            [("seed_mode", data["seed_mode"])])
    if v2.get("controls"):
        add("Control configuration (not reconstructed)",
            [("controls", _normalized_controls_for_notes(v2["controls"], ckpt_to_safetensors, changed))])
    if v2.get("teaCache"):
        add("TeaCache settings (not reconstructed)", [
            (k, v2.get(k)) for k in ("teaCache", "teaCacheStart", "teaCacheEnd",
                                     "teaCacheThreshold", "teaCacheMaxSkipSteps")])
    if v2.get("clipWeight") not in (None, 1, 1.0):
        add("CLIP weight (not reconstructed)", [("clipWeight", v2["clipWeight"])])
    if v2.get("separateClipL") or v2.get("separateOpenClipG") or v2.get("separateT5"):
        add("Separate text encoders (not reconstructed)", [
            (k, v2.get(k)) for k in ("separateClipL", "separateOpenClipG", "separateT5")])
    if v2.get("batchCount") not in (None, 1):
        add("Batch count (not reconstructed)", [("batchCount", v2["batchCount"])])
    if data.get("strength", v2.get("strength", 1)) not in (None, 1, 1.0):
        add("Input image/latent not available for denoising", [
            ("strength", data.get("strength", v2.get("strength"))),
            ("limitation", "Empty latent is a best-effort graph, not the original input"),
        ])
    profile = data.get("profile")
    if isinstance(profile, dict) and isinstance(profile.get("timings"), list):
        events = [event.get("name") for event in profile["timings"]
                  if isinstance(event, dict) and isinstance(event.get("name"), str)]
        extra = [name for name in events if name in {
            "second_pass_image_encoded", "second_pass_sampling", "second_pass_image_decoded",
            "image_encoded", "image_upscaled", "face_restored"}]
        if extra:
            add("Named events found in recorded processing profile (not replayed)",
                [("profile event", name) for name in extra])
    return items


# The following helpers affect only canvas presentation. They never alter the
# Draw Things metadata, the reconstructed processing graph, or API prompt.
NOTE_LINE_LIMIT = 25  # Keep each note readable even with extensive metadata.
NOTE_LINE_WIDTH = 54  # Wrap for compact columns, without omitting information.
NOTE_MIN_WIDTH = 270
NOTE_MAX_WIDTH = 445
NOTE_BODY_CHAR_WIDTH = 7.0  # Conservative monospace text width in canvas units.
NOTE_TITLE_CHAR_WIDTH = 7.7
NOTE_SIDE_PADDING = 38
NOTE_MIN_HEIGHT = 80
NOTE_HEIGHT_BASE = 44  # Title bar and compact text-widget padding.
NOTE_LINE_HEIGHT = 15  # Estimated ComfyUI default Note textarea line pitch.
NOTE_COLUMNS = 3  # Informational-only PARTIAL workflows.
NOTE_X_SPACING = 40  # Gap between content-fitted columns, not absolute pitch.
NOTE_Y_SPACING = 26
NOTE_LEFT_GAP = 95  # Keep notes comfortably clear of checkpoint inputs.
NOTE_COLUMN_GAP = 38
NOTE_TARGET_COLUMN_HEIGHT = 950  # Balance notes independently of the functional graph height.


def _note_size(title, body):
    """Return (width, height) using the visible lines rather than fixed size.

    Title and body have different font metrics. Clamp to predictable limits,
    while _note_wrap makes the body fit the maximum text line width.
    """
    lines = body.splitlines() or [""]
    widest = max(len(line) for line in lines)
    width = max(NOTE_MIN_WIDTH,
                min(NOTE_MAX_WIDTH, round(max(
                    widest * NOTE_BODY_CHAR_WIDTH,
                    len(title) * NOTE_TITLE_CHAR_WIDTH) + NOTE_SIDE_PADDING)))
    height = max(NOTE_MIN_HEIGHT, NOTE_HEIGHT_BASE + len(lines) * NOTE_LINE_HEIGHT)
    return width, height


def _note_wrap(line):
    """Wrap long *display* lines; all source values stay verbatim in PNG XMP."""
    if len(line) <= NOTE_LINE_WIDTH:
        return [line]
    leading = len(line) - len(line.lstrip(" "))
    prefix = " " * max(leading, 3)
    # A long individual metadata value may contain no word breaks (e.g. JSON).
    return textwrap.wrap(line, width=NOTE_LINE_WIDTH,
                         subsequent_indent=prefix,
                         break_long_words=True, break_on_hyphens=False,
                         replace_whitespace=False, drop_whitespace=False) or [line]


def _note_topic(title):
    """Choose a human-readable area of the canvas without reinterpreting data."""
    if title.startswith(("Hires Fix", "Refiner configuration")):
        return "Hires Fix & Refiner"
    if title.startswith(("Upscaling", "Face restoration")):
        return "Upscaling & Restoration"
    if title.startswith(("Tiled diffusion", "Tiled VAE")):
        return "Tiling & Decoding"
    if title.startswith(("LoRA ", "Refiner-only LoRA", "All-stage LoRA", "Unmapped LoRA")):
        return "LoRA Details"
    if title.startswith("Named events found"):
        return "Processing Profile"
    if title.startswith(("CFG Zero Star", "Special negative-prompt", "Model shift",
                         "Embedded guidance", "Draw Things seed mode")):
        return "Sampling & Guidance"
    return "Other Recorded Settings"


def _notes_text(items):
    """Show recorded fields with just one blank line between sections.

    Node titles and the section headings already make the context clear;
    repeating several disclaimer lines in every Note wastes canvas space.
    Original complete metadata remains in the source XMP regardless.
    """
    lines = []
    for title, pairs in items:
        if lines:
            lines.append("")
        lines.extend(_note_wrap(title))
        for key, value in pairs:
            if value != "None":
                lines.extend(_note_wrap("  %s: %s" % (key, value)))
    return "\n".join(lines)


def _make_note_pages(items):
    """Return labeled Note texts, preserving all informational sections.

    Small workflows get a single Note. Larger ones get topical Notes. Very
    long sections (e.g. a serialized control setting) paginate instead of
    producing invisible text below the bottom of a capped Note widget.
    """
    all_text = _notes_text(items)
    if len(all_text.splitlines()) <= NOTE_LINE_LIMIT:
        return [("Additional Draw Things settings", all_text)]

    topics = {}
    for heading, pairs in items:
        topics.setdefault(_note_topic(heading), []).append((heading, pairs))
    pages = []
    intro = []
    for topic, sections in topics.items():
        # Page sections on whole field boundaries where possible. If a single
        # field is exceptionally long, repeat the heading on continued pages.
        bodies = []
        body = list(intro)
        for heading, pairs in sections:
            fields = []
            for key, value in pairs:
                if value != "None":
                    fields.extend(_note_wrap("  %s: %s" % (key, value)))
            # Never place a section heading as the last line on a page.
            remaining = list(fields)
            continuation = False
            if not remaining:
                remaining = ["  (no recorded values)"]
            while remaining:
                header = heading + (" (continued)" if continuation else "")
                heading_lines = _note_wrap(header)
                available = NOTE_LINE_LIMIT - len(body) - len(heading_lines)
                if available < 2:
                    bodies.append("\n".join(body).rstrip())
                    body = list(intro)
                    available = NOTE_LINE_LIMIT - len(body) - len(heading_lines)
                # Reserve a spacer only when it fits; content never truncates.
                taken = remaining[:available]
                remaining = remaining[len(taken):]
                body.extend(heading_lines)
                body.extend(taken)
                if len(body) < NOTE_LINE_LIMIT:
                    body.append("")
                if remaining:
                    bodies.append("\n".join(body).rstrip())
                    body = list(intro)
                    continuation = True
        if len(body) > len(intro):
            bodies.append("\n".join(body).rstrip())
        for i, note_body in enumerate(bodies, 1):
            name = "Draw Things - " + topic
            if len(bodies) > 1:
                name += " (%d/%d)" % (i, len(bodies))
            pages.append((name, note_body))
    return pages


def _add_note_grid(graph, pages, top, single_x=None):
    """Fit a Note-only PARTIAL workflow to content-based column widths."""
    if not pages:
        return
    columns = 1 if len(pages) == 1 else NOTE_COLUMNS
    column_widths = [max((_note_size(title, body)[0]
                          for j, (title, body) in enumerate(pages) if j % columns == col),
                         default=NOTE_MIN_WIDTH) for col in range(columns)]
    xs = [single_x if single_x is not None else 40]
    for col in range(1, columns):
        xs.append(xs[-1] + column_widths[col - 1] + NOTE_X_SPACING)
    for start in range(0, len(pages), columns):
        row = pages[start:start + columns]
        heights = []
        for col, (title, body) in enumerate(row):
            nid = graph.note(title, body, [xs[col], top])
            heights.append(graph.nodes[nid - 1]["size"][1])
        top += max(heights) + NOTE_Y_SPACING


def _add_notes_left_of_workflow(graph, pages):
    """Height-balance narrow Note columns to the left of the functional graph.

    Earlier versions filled a column until an arbitrary limit was reached,
    potentially leaving just one orphan Note in a second column. The number
    of columns is now chosen from the *total Note text height*, independently
    of the functional canvas height (which changes when Hires Fix is present).
    A contiguous partition minimizes column height disparities without
    dropping, reordering, or rewriting any informational text. Rightmost
    column contains the earliest topics, same reading order as before.
    """
    if not pages:
        return
    functional = [n for n in graph.nodes if n["type"] != "Note"]
    left = min(n["pos"][0] for n in functional)
    top = min(n["pos"][1] for n in functional)
    sizes = [_note_size(title, body) for title, body in pages]
    n = len(pages)
    total_height = sum(h for _, h in sizes) + NOTE_Y_SPACING * (n - 1)
    num_columns = min(n, max(1, math.ceil(total_height / NOTE_TARGET_COLUMN_HEIGHT)))

    # DP balances actual displayed Note heights; no arbitrary overflow rule or
    # large fixed width. Each column consists of consecutive Note sections.
    # Computing optimal splits avoids one almost-empty column when 7 topics
    # are close to the height cutoff. Cost is deviation from equal heights.
    from functools import lru_cache
    prefix = [0]
    for width, height in sizes:
        prefix.append(prefix[-1] + height)

    def column_height(start, end):
        return prefix[end] - prefix[start] + NOTE_Y_SPACING * (end - start - 1)

    target = (sum(height for _, height in sizes) + NOTE_Y_SPACING * (n - num_columns)) / num_columns

    @lru_cache(None)
    def fit(start, left_columns):
        if left_columns == 1:
            return (column_height(start, n) - target) ** 2, (n,)
        best = None
        # Leave at least one Note for each remaining column.
        for end in range(start + 1, n - left_columns + 2):
            tail_cost, tail_boundaries = fit(end, left_columns - 1)
            score = (column_height(start, end) - target) ** 2 + tail_cost
            candidate = score, (end,) + tail_boundaries
            if best is None or candidate < best:
                best = candidate
        return best

    boundaries = fit(0, num_columns)[1]
    right_edge = left - NOTE_LEFT_GAP
    start = 0
    for end in boundaries:
        width = max(w for w, _ in sizes[start:end])
        x = right_edge - width
        y = top
        for (title, body), (_, height) in zip(pages[start:end], sizes[start:end]):
            graph.note(title, body, [x, y])
            y += height + NOTE_Y_SPACING
        right_edge = x - NOTE_COLUMN_GAP
        start = end


def _display_normalized_partial(data, enabled):
    """For PARTIAL Note-only workflows, selectively normalize known file keys.

    This is a deep-copied display view. The parsed original and source PNG XMP
    are never changed, and non-filename configuration values stay untouched.
    """
    if not enabled:
        return data, 0
    shown = copy.deepcopy(data)
    changed = []

    def change(d, key, category):
        if isinstance(d, dict) and isinstance(d.get(key), str):
            before = d[key]
            after = _normalize_named_reference(before, category, True)
            if after != before:
                d[key] = after
                changed.append((key, before, after))

    def loras(d, key):
        if not isinstance(d, dict) or not isinstance(d.get(key), list):
            return
        for entry in d[key]:
            if isinstance(entry, dict):
                change(entry, "file", "lora")
                change(entry, "model", "lora")

    for d in (shown, shown.get("v2", {})):
        change(d, "model", "checkpoint")
        change(d, "refinerModel", "refiner")
        change(d, "refiner", "refiner")
        change(d, "upscaler", "upscaler")
        change(d, "faceRestoration", "face")
        change(d, "face_restoration", "face")
        loras(d, "loras")
        loras(d, "lora")
        if isinstance(d, dict) and "controls" in d:
            d["controls"] = _normalized_controls_for_notes(d["controls"], True, changed)
    return shown, len(changed)


def informational_workflow(data, png_dims, reason, *, ckpt_to_safetensors=False):
    """Canvas-only Notes when no generation-node topology is constructed."""
    g = Graph()
    shown, adjusted = _display_normalized_partial(data, ckpt_to_safetensors)
    raw = json.dumps(shown, ensure_ascii=True, indent=2)
    # Display the *full* recorded JSON, but split it across readable panels.
    wrapped = []
    for line in raw.splitlines():
        wrapped.extend(_note_wrap(line))
    max_body_lines = NOTE_LINE_LIMIT - 4
    segments = [wrapped[i:i + max_body_lines]
                for i in range(0, len(wrapped), max_body_lines)] or [[]]
    panels = []
    for n, segment in enumerate(segments, 1):
        title = "Draw Things PARTIAL (information only)"
        if len(segments) > 1:
            title += " (%d/%d)" % (n, len(segments))
        body = ("No executable workflow was reconstructed.\n"
                "Reason: %s\n" % reason +
                ("Filenames normalized for display; original XMP unchanged.\n\n"
                 if adjusted else "Original Draw Things XMP/JSON unchanged.\n\n") +
                "\n".join(segment))
        panels.append((title, body))
    _add_note_grid(g, panels, 80)
    w, api = g.finish()
    warnings = [reason]
    if adjusted:
        warnings.append("ComfyUI filenames: %d named reference(s) normalized in PARTIAL Notes; original XMP unchanged" % adjusted)
    return w, api, warnings

def _comfy_model_filename(recorded_filename, ckpt_to_safetensors=False, *, is_lora=False):
    """Optionally restore ComfyUI model or LoRA filenames from Draw Things names.

    A Draw Things-imported checkpoint named 'model.safetensors' may appear as
    'model_f16.ckpt', and an imported LoRA named 'name.safetensors' may appear
    as 'name_lora_f16.ckpt'. Remove the appropriate *whole suffix*, based on
    whether the reference is a LoRA or a checkpoint, rather than blindly
    replacing the extension. Plain .ckpt names retain the established fallback.
    Match only final filename suffixes, case-insensitively, preserving paths.
    Original Draw Things XMP remains unchanged; categorized Notes may be normalized.
    """
    if ckpt_to_safetensors and isinstance(recorded_filename, str):
        low = recorded_filename.lower()
        if is_lora and low.endswith("_lora_f16.ckpt") and len(recorded_filename) > len("_lora_f16.ckpt"):
            return recorded_filename[:-len("_lora_f16.ckpt")] + ".safetensors"
        if low.endswith("_f16.ckpt") and len(recorded_filename) > len("_f16.ckpt"):
            return recorded_filename[:-len("_f16.ckpt")] + ".safetensors"
        if low.endswith(".ckpt"):
            return recorded_filename[:-len(".ckpt")] + ".safetensors"
    return recorded_filename



def _normalize_named_reference(value, category, enabled):
    """Return a *display/output-only* filename under the optional filename-normalization flag.

    Not a reverse-engineering claim about the original import file. Only
    normalize a file whose type and naming convention are sufficiently known.
    Missing/uncertain formats remain untouched, for manual choice in ComfyUI.
    Existing .safetensors/.pth/.pt/.bin values are never changed.
    """
    if not enabled or not isinstance(value, str):
        return value
    low = value.lower()
    if not low.endswith(".ckpt"):
        return value
    if category in ("checkpoint", "refiner", "lora"):
        return _comfy_model_filename(value, True, is_lora=(category == "lora"))

    # The DT import convention _f16.ckpt is not exclusive to safetensors.
    # Recognized ESRGAN-style upscalers and known face restorers commonly use
    # .pth in ComfyUI. Do not pretend that every custom model was a .pth file.
    if category in ("upscaler", "face"):
        stem = value[:-len("_f16.ckpt")] if low.endswith("_f16.ckpt") else None
        if stem is None or not stem:
            return value
        filename = re.split(r"[/\\]", stem)[-1].lower()
        if category == "upscaler":
            known = ("realesrgan", "real_esrgan", "realesrnet", "esrgan", "remacri", "4x_", "2x_", "ultrasharp", "swinir", "bsrgan")
        else:
            known = ("restoreformer", "codeformer", "gfpgan")
        if filename == "remacri_original":
            # A commonly distributed original is remacri_original.pt.
            return stem + ".pt"
        if filename.startswith(known):
            return stem + ".pth"
        return value

    if category == "control":
        # Classic SD1.x ControlNet v1.1 was traditionally distributed as .pth;
        # most newer user-imported conditioning models use .safetensors. The
        # latter is a best-effort convention, not proof of original format.
        basename = re.split(r"[/\\]", value)[-1].lower()
        is_legacy_pth = (basename.startswith(("control_v11", "control_v10")) or
                         basename.startswith("control_v1_"))
        ext = ".pth" if is_legacy_pth else ".safetensors"
        if low.endswith("_f16.ckpt") and len(value) > len("_f16.ckpt"):
            return value[:-len("_f16.ckpt")] + ext
        return value[:-len(".ckpt")] + ext
    return value


def _normalized_controls_for_notes(value, enabled, changed):
    """Only rewrite explicit controls[].file fields, never input-image files.

    Control entries may have arbitrary nested options, mask filenames or image
    settings; those are not model references and must be retained verbatim.
    """
    if not isinstance(value, list):
        return value
    result = []
    for entry in value:
        if not isinstance(entry, dict):
            result.append(entry)
            continue
        out = dict(entry)
        original = entry.get("file")
        if isinstance(original, str):
            converted = _normalize_named_reference(original, "control", enabled)
            if converted != original:
                out["file"] = converted
                changed.append(("controls[].file", original, converted))
        result.append(out)
    return result


def build_workflow(data, png_dims, *, ckpt_to_safetensors=False):
    v2 = data.get("v2") if isinstance(data.get("v2"), dict) else {}
    warnings = []
    model = data.get("model") or v2.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ConversionError("Draw Things checkpoint/model filename missing")
    if not any(model.lower().endswith(ext) for ext in (".ckpt", ".safetensors", ".pt", ".pth", ".bin")):
        warnings.append("model name does not have a typical ComfyUI checkpoint extension; name retained verbatim")
    prompts = (str(data.get("c", "")), str(data.get("uc", "")))
    if not data.get("c") and "c" not in data:
        raise ConversionError("Draw Things positive prompt missing")
    size = data.get("size")
    wh = re.fullmatch(r"\s*(\d+)\s*[xX]\s*(\d+)\s*", str(size or ""))
    width = v2.get("width", int(wh.group(1)) if wh else png_dims[0])
    height = v2.get("height", int(wh.group(2)) if wh else png_dims[1])
    width = _as_int(width, "width", 16, 65536)
    height = _as_int(height, "height", 16, 65536)
    if (width, height) != png_dims:
        warnings.append("generation size %dx%d differs from stored PNG %dx%d" % (width, height, *png_dims))
    seed = _as_int(data.get("seed", v2.get("seed")), "seed", 0, 2**64 - 1)
    steps = _as_int(data.get("steps", v2.get("steps")), "steps", 1, 100000)
    cfg = _as_float(data.get("scale", v2.get("guidanceScale")), "guidance scale", 0, 1000)
    denoise = _as_float(data.get("strength", v2.get("strength", 1.0)), "strength", 0.0, 1.0)
    sampler, scheduler = sampler_mapping(data.get("sampler", ""), warnings)
    batch = _as_int(v2.get("batchSize", 1), "batch size", 1, 4096)
    if v2.get("batchCount", 1) not in (None, 1):
        warnings.append("Draw Things batch count is not reconstructed (batch size is preserved)")
    if denoise < 1:
        warnings.append("strength below 1: source image/latent is not embedded; empty-latent workflow is only an approximation")
    if data.get("shift", v2.get("shift")) not in (None, 0, 0.0):
        warnings.append("model-specific shift is not applied by the generic vanilla ComfyUI graph")
    if v2.get("resolutionDependentShift"):
        warnings.append("resolution-dependent model shift is not reconstructed")
    if v2.get("guidanceEmbed") is not None or v2.get("speedUpWithGuidanceEmbed"):
        warnings.append("model-specific embedded guidance is not mapped; KSampler CFG may differ")
    hires_plan, hires_reason = _hires_fix_plan(data, v2, width, height, denoise)
    if hires_plan:
        warnings.append("Hires Fix: reconstructed two-pass decode/bilinear-scale/encode/sampling sequence (best effort)")
        warnings.append("Hires Fix second sampler reuses recorded seed; original second-pass RNG state unavailable")
    elif v2.get("hiresFix") or data.get("hires_fix") is True:
        warnings.append("Draw Things Hires Fix not reconstructed: " + hires_reason)
    if v2.get("tiledDiffusion"):
        warnings.append("tiled diffusion is not reconstructed; original settings remain in XMP")
    if v2.get("controls"):
        warnings.append("ControlNet/control inputs are not reconstructed; source control images are unavailable")
    if v2.get("separateClipL") or v2.get("separateOpenClipG") or v2.get("separateT5"):
        warnings.append("separate text-encoder settings are not reconstructed")
    if v2.get("numFrames", 1) not in (1, 0, None) and (v2.get("causalInference") or v2.get("id") not in (None, 0)):
        warnings.append("video/multiframe architecture not represented by still-image latent workflow")
    if v2.get("cfgZeroStar") or v2.get("cfgZeroInitSteps"):
        warnings.append("CFG-zero-star settings are not reconstructed")
    if v2.get("teaCache"):
        warnings.append("TeaCache acceleration settings are not reconstructed")
    if v2.get("zeroNegativePrompt"):
        warnings.append("special zero-negative-prompt conditioning is not reconstructed")
    if v2.get("clipWeight") not in (None, 1, 1.0):
        warnings.append("Draw Things CLIP weight is not reconstructed")
    if v2.get("colorCalibration") not in (None, "none", "disabled"):
        warnings.append("Draw Things color calibration is not reconstructed")
    if data.get("seed_mode"):
        warnings.append("Draw Things seed mode may use a different RNG from ComfyUI")

    # Optional filename mapping changes generated ComfyUI metadata only.
    # The original Draw Things XMP is always preserved verbatim.
    mapped_references = 0
    output_model = _comfy_model_filename(model, ckpt_to_safetensors)
    mapped_references += (output_model != model)
    graph = Graph()
    load = graph.add("CheckpointLoaderSimple", "Draw Things Checkpoint", [-920, 140],
                     [], [("MODEL", "MODEL"), ("CLIP", "CLIP"), ("VAE", "VAE")],
                     {"ckpt_name": output_model}, [output_model])
    model_out, clip_out = load, load
    loras = v2.get("loras") if isinstance(v2.get("loras"), list) else data.get("lora", [])
    if not isinstance(loras, list):
        loras = []
    extra_lora_notes = []
    for i, entry in enumerate(loras):
        if not isinstance(entry, dict):
            warnings.append("LoRA entry %d is malformed; retained in XMP, not applied" % (i + 1))
            extra_lora_notes.append(("LoRA %d (malformed)" % (i + 1), entry))
            continue
        name = entry.get("file") or entry.get("model")
        if not isinstance(name, str) or not name:
            warnings.append("LoRA %d has no filename; retained in XMP, not applied" % (i + 1))
            extra_lora_notes.append(("LoRA %d (no filename)" % (i + 1), entry))
            continue
        if "weight" not in entry:
            warnings.append("LoRA %s has no recorded weight; not inventing one" % name)
            extra_lora_notes.append(("LoRA without weight: " + name, entry))
            continue
        try:
            strength = _as_float(entry["weight"], "LoRA weight", -1000, 1000)
        except ConversionError:
            warnings.append("LoRA %s has an invalid recorded weight; omitted from graph" % name)
            extra_lora_notes.append(("LoRA invalid weight: " + name, entry))
            continue
        # Draw Things modes describe *generation stages*, NOT model-vs-CLIP.
        # The converter does not construct a refiner stage; 'refiner' entries must
        # never be connected to the base checkpoint. Weights are passed through.
        mode = entry.get("mode")
        if mode not in ("all", "base", "refiner"):
            warnings.append("LoRA %s has unrecognized/missing stage mode %r; not applied" % (name, mode))
            extra_lora_notes.append(("Unmapped LoRA stage mode: " + name, entry))
            continue
        if mode == "refiner":
            warnings.append("refiner-only LoRA %s retained in XMP; refiner graph not reconstructed" % name)
            extra_lora_notes.append(("Refiner-only LoRA: " + name, entry))
            continue
        output_lora = _comfy_model_filename(name, ckpt_to_safetensors, is_lora=True)
        mapped_references += (output_lora != name)
        fields = {"lora_name": output_lora, "strength_model": strength, "strength_clip": strength}
        node = graph.add("LoraLoader", "Draw Things LoRA %d (%s)" % (i + 1, mode),
                         [0, 0], [("model", "MODEL"), ("clip", "CLIP")],
                         [("MODEL", "MODEL"), ("CLIP", "CLIP")], fields)
        graph.connect(model_out, "MODEL", node, "model")
        graph.connect(clip_out, "CLIP", node, "clip")
        model_out, clip_out = node, node
        if mode == "all" and v2.get("refinerModel"):
            extra_lora_notes.append(("All-stage LoRA; refiner stage not reconstructed: " + name, entry))

    skip = _as_int(v2.get("clipSkip", 1), "CLIP skip", 1, 100)
    if skip > 1:
        node = graph.add("CLIPSetLastLayer", "Draw Things CLIP Skip", [-240, 50],
                         [("clip", "CLIP")], [("CLIP", "CLIP")],
                         {"stop_at_clip_layer": -skip}, [-skip])
        graph.connect(clip_out, "CLIP", node, "clip")
        clip_out = node
        warnings.append("CLIP skip mapping is model-dependent")

    pos_node = graph.add("CLIPTextEncode", "Draw Things Positive", [-120, -270],
                         [("clip", "CLIP")], [("CONDITIONING", "CONDITIONING")],
                         {"text": prompts[0]}, [prompts[0]])
    neg_node = graph.add("CLIPTextEncode", "Draw Things Negative", [-120, -30],
                         [("clip", "CLIP")], [("CONDITIONING", "CONDITIONING")],
                         {"text": prompts[1]}, [prompts[1]])
    graph.connect(clip_out, "CLIP", pos_node, "clip")
    graph.connect(clip_out, "CLIP", neg_node, "clip")
    latent_width = hires_plan["first_width"] if hires_plan else width
    latent_height = hires_plan["first_height"] if hires_plan else height
    latent = graph.add("EmptyLatentImage", "Draw Things Latent", [-160, 250], [],
                       [("LATENT", "LATENT")],
                       {"width": latent_width, "height": latent_height, "batch_size": batch})
    sample_fields = {"seed": seed, "steps": steps, "cfg": cfg,
                     "sampler_name": sampler, "scheduler": scheduler, "denoise": denoise}
    sampler_node = graph.add("KSampler", "Draw Things First Pass" if hires_plan else "Draw Things Sampling", [280, 50],
                             [("model", "MODEL"), ("positive", "CONDITIONING"),
                              ("negative", "CONDITIONING"), ("latent_image", "LATENT")],
                             [("LATENT", "LATENT")], sample_fields,
                             [seed, "fixed", steps, cfg, sampler, scheduler, denoise])
    graph.connect(model_out, "MODEL", sampler_node, "model")
    graph.connect(pos_node, "CONDITIONING", sampler_node, "positive")
    graph.connect(neg_node, "CONDITIONING", sampler_node, "negative")
    graph.connect(latent, "LATENT", sampler_node, "latent_image")

    tiled = bool(data.get("tiled_decoding", v2.get("tiledDecoding", False)))
    if tiled:
        tw = _as_int(data.get("decoding_tile_width", v2.get("decodingTileWidth", 512)), "decoding tile width", 64, 4096)
        th = _as_int(data.get("decoding_tile_height", v2.get("decodingTileHeight", tw)), "decoding tile height", 64, 4096)
        overlap = _as_int(data.get("decoding_tile_overlap", v2.get("decodingTileOverlap", 64)), "decoding tile overlap", 0, 4096)
        if tw != th:
            warnings.append("rectangular tiled decoding approximated by square ComfyUI tiles")
        tile_size = max(tw, th)
        if tile_size % 32:
            rounded = max(64, min(4096, round(tile_size / 32) * 32))
            warnings.append("VAE tile size rounded to ComfyUI's 32-pixel step")
            tile_size = rounded
        if overlap % 32:
            overlap = max(0, min(4096, round(overlap / 32) * 32))
            warnings.append("VAE tile overlap rounded to ComfyUI's 32-pixel step")
        decode_kind = "VAEDecodeTiled"
        decode_fields = {"tile_size": tile_size, "overlap": overlap,
                         "temporal_size": 64, "temporal_overlap": 8}
        decode_widgets = [tile_size, overlap, 64, 8]
    else:
        decode_kind = "VAEDecode"
        decode_fields, decode_widgets = {}, []
    if hires_plan:
        # The published ordinary Draw Things pipeline decodes the first result,
        # resizes it with bilinear interpolation, re-encodes it, then samples.
        # Do not substitute a third-party upscaler or invent stage2Steps.
        first_decode = graph.add("VAEDecode", "Draw Things First Pass Decode", [690, 110],
                                 [("samples", "LATENT"), ("vae", "VAE")],
                                 [("IMAGE", "IMAGE")], {}, [])
        graph.connect(sampler_node, "LATENT", first_decode, "samples")
        graph.connect(load, "VAE", first_decode, "vae")
        scale_fields = {"upscale_method": "bilinear", "width": width,
                        "height": height, "crop": "center"}
        scaled = graph.add("ImageScale", "Draw Things Hires Fix Resize", [1050, 110],
                           [("image", "IMAGE")], [("IMAGE", "IMAGE")],
                           scale_fields, ["bilinear", width, height, "center"])
        graph.connect(first_decode, "IMAGE", scaled, "image")
        encoded = graph.add("VAEEncode", "Draw Things Hires Fix Encode", [1440, 110],
                            [("pixels", "IMAGE"), ("vae", "VAE")],
                            [("LATENT", "LATENT")], {}, [])
        graph.connect(scaled, "IMAGE", encoded, "pixels")
        graph.connect(load, "VAE", encoded, "vae")
        second_fields = {"seed": seed, "steps": steps, "cfg": cfg,
                         "sampler_name": sampler, "scheduler": scheduler,
                         "denoise": hires_plan["strength"]}
        second = graph.add("KSampler", "Draw Things Hires Fix Second Pass", [1830, 110],
                           [("model", "MODEL"), ("positive", "CONDITIONING"),
                            ("negative", "CONDITIONING"), ("latent_image", "LATENT")],
                           [("LATENT", "LATENT")], second_fields,
                           [seed, "fixed", steps, cfg, sampler, scheduler, hires_plan["strength"]])
        graph.connect(model_out, "MODEL", second, "model")
        graph.connect(pos_node, "CONDITIONING", second, "positive")
        graph.connect(neg_node, "CONDITIONING", second, "negative")
        graph.connect(encoded, "LATENT", second, "latent_image")
        decode = graph.add(decode_kind, "Draw Things Final Decode", [2260, 110],
                           [("samples", "LATENT"), ("vae", "VAE")],
                           [("IMAGE", "IMAGE")], decode_fields, decode_widgets)
        graph.connect(second, "LATENT", decode, "samples")
        graph.connect(load, "VAE", decode, "vae")
    else:
        decode = graph.add(decode_kind, "Draw Things Decode", [690, 110],
                           [("samples", "LATENT"), ("vae", "VAE")],
                           [("IMAGE", "IMAGE")], decode_fields, decode_widgets)
        graph.connect(sampler_node, "LATENT", decode, "samples")
        graph.connect(load, "VAE", decode, "vae")
    save = graph.add("SaveImage", "Save Image", [1030, 110],
                     [("images", "IMAGE")], [], {"filename_prefix": "DrawThings_ComfyUI"})
    graph.connect(decode, "IMAGE", save, "images")

    # Position native ComfyUI nodes in two clean sampling stages when Hires Fix
    # is documented; otherwise preserve default canvas positions.
    lora_nodes = [node for node in graph.nodes if node["type"] == "LoraLoader"]
    has_clip_skip = any(node["type"] == "CLIPSetLastLayer" for node in graph.nodes)
    prompts_x = 1080 if lora_nodes or has_clip_skip else 560
    if has_clip_skip:
        prompts_x += 390
    x_by_type = {
        "CheckpointLoaderSimple": (40, 170),
        "CLIPTextEncode": (prompts_x, -60),
        "EmptyLatentImage": (prompts_x, 590),
        "KSampler": (prompts_x + 560, 200),
        "VAEDecode": (prompts_x + 1070, 200),
        "VAEDecodeTiled": (prompts_x + 1070, 200),
        "SaveImage": (prompts_x + 1560, 200),
        "CLIPSetLastLayer": (prompts_x - 450, 150),
    }
    if hires_plan:
        # Keep the initial decoding/resize/encode path on the upper row,
        # with second-pass sampling, final decoding, and saving directly
        # below the main graph rather than far to the lower right.
        x_by_type.update({
            "VAEDecode": (prompts_x + 1025, 200),
            "VAEDecodeTiled": (prompts_x + 1025, 200),
            "ImageScale": (prompts_x + 1475, 200),
            "VAEEncode": (prompts_x + 1930, 200),
            "SaveImage": (prompts_x + 1490, 850),
        })
    prompt_index = 0
    for node in graph.nodes:
        kind = node["type"]
        if kind == "LoraLoader":
            slot = lora_nodes.index(node)
            node["pos"] = [530, 20 + slot * 300]
            node["size"] = [380, 240]
        elif kind in x_by_type:
            x, y = x_by_type[kind]
            if kind == "CLIPTextEncode":
                y += prompt_index * 320
                prompt_index += 1
            if hires_plan:
                if kind == "KSampler" and node["title"].endswith("Second Pass"):
                    x, y = prompts_x + 560, 820
                elif kind in ("VAEDecode", "VAEDecodeTiled") and node["title"].endswith("Final Decode"):
                    x, y = prompts_x + 1035, 850
            node["pos"] = [x, y]
            node["size"] = {
                "CheckpointLoaderSimple": [380, 170],
                "CLIPTextEncode": [435, 270],
                "EmptyLatentImage": [360, 205],
                "KSampler": [375, 355],
                "VAEDecode": [350, 180],
                "VAEDecodeTiled": [360, 225],
                "ImageScale": [355, 220],
                "VAEEncode": [355, 180],
                "SaveImage": [365, 180],
                "CLIPSetLastLayer": [365, 180],
            }[kind]

    note_filename_changes = []
    untranslated = _meaningful_untranslated(
        data, v2, png_dims, hires_plan=hires_plan,
        ckpt_to_safetensors=ckpt_to_safetensors, changed=note_filename_changes)
    for label, original_entry in extra_lora_notes:
        # Even a refiner-only, malformed or unresolved LoRA may
        # have an identifiable .ckpt filename. Normalize its NOTE display but
        # never apply it to the wrong generation stage or edit the source XMP.
        display_entry = original_entry
        note_label = label
        if ckpt_to_safetensors and isinstance(original_entry, dict):
            display_entry = dict(original_entry)
            for key in ("file", "model"):
                recorded = original_entry.get(key)
                if isinstance(recorded, str):
                    display = _normalize_named_reference(recorded, "lora", True)
                    if display != recorded:
                        note_filename_changes.append(("LoRA " + key, recorded, display))
                        display_entry[key] = display
                        note_label = note_label.replace(recorded, display)
        field_label = "LoRA entry (normalized filenames)" if display_entry is not original_entry else "original LoRA entry"
        untranslated.append((note_label, [(field_label, _render(display_entry))]))
    mapped_references += len(note_filename_changes)
    if tiled and (tw != th or v2.get("decodingTileOverlap") != overlap):
        untranslated.append(("Tiled VAE decoding approximation", [
            ("recorded decodingTileWidth", _render(tw)),
            ("recorded decodingTileHeight", _render(th)),
            ("recorded decodingTileOverlap", _render(data.get("decoding_tile_overlap", v2.get("decodingTileOverlap")))),
            ("ComfyUI square tile size", _render(tile_size)),
            ("ComfyUI overlap", _render(overlap)),
        ]))
    if untranslated:
        pages = _make_note_pages(untranslated)
        _add_notes_left_of_workflow(graph, pages)
        warnings.append("%d untranslated setting group(s) documented in %d ComfyUI Note(s)" %
                        (len(untranslated), len(pages)))
    if mapped_references:
        warnings.append("ComfyUI filenames: %d named .ckpt reference(s) normalized in functional nodes and/or informational Notes; output formats depend on category (.safetensors/.pth); original XMP unchanged" % mapped_references)
    w, a = graph.finish()
    return w, a, list(dict.fromkeys(warnings))


def write_chunk(dest, kind, key, content):
    payload = key.encode("latin-1") + b"\0" + content.encode("utf-8")
    if len(payload) > 2**31:
        raise ConversionError("generated metadata is too large")
    dest.write(struct.pack(">I", len(payload)))
    dest.write(kind)
    dest.write(payload)
    dest.write(struct.pack(">I", zlib.crc32(payload, zlib.crc32(kind)) & 0xffffffff))


def _copy_n(src, dst, total):
    while total:
        part = src.read(min(total, BUF))
        if not part:
            raise ConversionError("source PNG changed during conversion")
        dst.write(part)
        total -= len(part)


def build_staged_png(path, destination, scan, workflow, api, overwrite):
    # Insert before IDAT so PIL/ComfyUI finds PNG text metadata during initial open.
    inserted = False
    with open(path, "rb") as source, open(destination, "wb") as staged:
        staged.write(source.read(8))
        for position, full_len, kind, key in scan["chunks"]:
            if kind == b"IDAT" and not inserted:
                write_chunk(staged, b"tEXt", "workflow", json_text(workflow))
                write_chunk(staged, b"tEXt", "prompt", json_text(api))
                inserted = True
            if overwrite and key in ("workflow", "prompt"):
                continue
            source.seek(position)
            _copy_n(source, staged, full_len)
        staged.flush()
        os.fsync(staged.fileno())
    if not inserted:
        raise ConversionError("no PNG IDAT chunk")


# Native Windows FILETIME handling is essential: os.utime alone may not preserve
# all three Explorer dates, especially creation time.
if os.name == "nt":
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _create = k32.CreateFileW
    _create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _create.restype = wintypes.HANDLE
    _get = k32.GetFileTime
    _get.argtypes = [wintypes.HANDLE, ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME)]
    _get.restype = wintypes.BOOL
    _set = k32.SetFileTime
    _set.argtypes = [wintypes.HANDLE, ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME)]
    _set.restype = wintypes.BOOL
    _close = k32.CloseHandle
    _close.argtypes = [wintypes.HANDLE]
    _close.restype = wintypes.BOOL

    def _native_times(path, times=None):
        handle = _create(str(path), 0x180 if times is not None else 0x80,
                         7, None, 3, 0, None)
        if handle == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_last_error(), "CreateFileW failed for timestamps", str(path))
        try:
            if times is None:
                buf = [FILETIME() for _ in range(3)]
                if not _get(handle, *(ctypes.byref(x) for x in buf)):
                    raise OSError(ctypes.get_last_error(), "GetFileTime failed", str(path))
                return tuple((x.dwHighDateTime << 32) | x.dwLowDateTime for x in buf)
            buf = [FILETIME(t & 0xffffffff, t >> 32) for t in times]
            if not _set(handle, *(ctypes.byref(x) for x in buf)):
                raise OSError(ctypes.get_last_error(), "SetFileTime failed", str(path))
        finally:
            _close(handle)


def snapshot_times(path):
    if os.name == "nt":
        return _native_times(path)
    s = os.stat(path)
    return s.st_atime_ns, s.st_mtime_ns


def restore_times(path, times):
    if os.name == "nt":
        _native_times(path, times)
    else:
        os.utime(path, ns=times)


def assert_times(path, times):
    got = snapshot_times(path)
    if got != times:
        raise ConversionError("original filesystem timestamps changed: before %r, after %r" % (times, got))


def temp_name(path, purpose):
    fd, temp = tempfile.mkstemp(prefix=".dt-" + purpose + "-",
                                 suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    return Path(temp)


def copy_file(src, dst):
    with open(src, "rb") as sf, open(dst, "wb") as df:
        shutil.copyfileobj(sf, df, BUF)
        df.flush()
        os.fsync(df.fileno())


def safe_unlink(path):
    if path:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass


def identity(path):
    s = path.stat()
    return (s.st_dev, s.st_ino)


def unique_backup(path):
    for i in range(1, 1000000):
        cand = path.with_name(path.stem + ".bak" + str(i) + path.suffix)
        try:
            with cand.open("xb"):
                pass
            return cand
        except FileExistsError:
            continue
    raise ConversionError("too many existing backups")


def _datetime_from_unix_ns(epoch_ns):
    """Convert integer epoch nanoseconds to local time without float rounding."""
    seconds, nanos = divmod(int(epoch_ns), 1_000_000_000)
    try:
        return datetime.fromtimestamp(seconds).replace(microsecond=nanos // 1000)
    except (ValueError, OverflowError, OSError) as exc:
        raise ConversionError("invalid filesystem creation timestamp") from exc


def _linux_birth_ns(path):
    """Read Linux birth time using statx, if available; never substitute mtime."""
    if not sys.platform.startswith("linux"):
        return None
    class StatxTimestamp(ctypes.Structure):
        _fields_ = [("sec", ctypes.c_int64), ("nsec", ctypes.c_uint32),
                    ("reserved", ctypes.c_int32)]
    class Statx(ctypes.Structure):
        _fields_ = [
            ("mask", ctypes.c_uint32), ("blksize", ctypes.c_uint32),
            ("attributes", ctypes.c_uint64),
            ("nlink", ctypes.c_uint32), ("uid", ctypes.c_uint32),
            ("gid", ctypes.c_uint32), ("mode", ctypes.c_uint16),
            ("spare0", ctypes.c_uint16), ("ino", ctypes.c_uint64),
            ("size", ctypes.c_uint64), ("blocks", ctypes.c_uint64),
            ("attributes_mask", ctypes.c_uint64),
            ("atime", StatxTimestamp), ("btime", StatxTimestamp),
            ("ctime", StatxTimestamp), ("mtime", StatxTimestamp),
            ("rdev_major", ctypes.c_uint32), ("rdev_minor", ctypes.c_uint32),
            ("dev_major", ctypes.c_uint32), ("dev_minor", ctypes.c_uint32),
            ("mnt_id", ctypes.c_uint64), ("dio_mem_align", ctypes.c_uint32),
            ("dio_offset_align", ctypes.c_uint32),
            ("spare3", ctypes.c_uint64 * 12),
        ]
    try:
        fn = ctypes.CDLL(None, use_errno=True).statx
    except AttributeError:
        return None
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                   ctypes.c_uint, ctypes.POINTER(Statx)]
    fn.restype = ctypes.c_int
    out = Statx()
    # AT_FDCWD=-100, AT_SYMLINK_NOFOLLOW=0x100, STATX_BTIME=0x800.
    raw_path = os.fsencode(path)
    if fn(-100, raw_path, 0x100, 0x800, ctypes.byref(out)) != 0:
        return None
    if not out.mask & 0x800:
        return None
    return out.btime.sec * 1_000_000_000 + out.btime.nsec


def original_creation_datetime(path, saved_times):
    """Return the filesystem creation/birth date with microsecond precision.

    Windows uses the original native FILETIME snapshot (100-ns units).
    macOS uses st_birthtime_ns or st_birthtime from the source file.
    Linux uses statx birth time, if exposed by the filesystem.
    Date Modified and POSIX change time are NEVER used as substitutes.
    """
    if os.name == "nt":
        ticks = saved_times[0] - FILETIME_EPOCH
        if ticks <= 0:
            raise ConversionError("Date Created is unavailable or invalid")
        # FILETIME ticks = 100 nanoseconds, preserving all microsecond digits.
        return _datetime_from_unix_ns(ticks * 100), "creation"
    st = path.stat()
    birth_ns = getattr(st, "st_birthtime_ns", None)
    if birth_ns is None:
        birth = getattr(st, "st_birthtime", None)
        if birth is not None:
            birth_ns = int(round(birth * 1_000_000_000))
    if birth_ns is None:
        birth_ns = _linux_birth_ns(path)
    if birth_ns is None or birth_ns <= 0:
        raise ConversionError("Date Created is unavailable on this filesystem; refusing to use Date Modified")
    return _datetime_from_unix_ns(birth_ns), "creation"


def short_filename_plan(path, saved_times, reserved_targets=None):
    """Plan DrawThings_YYYYMMDD_HHMMSS_ffffff[_N].png with no overwrites.

    The original filename's sequence/seed and image fingerprint are ignored.
    Already-shortened v1.00 filenames are never renumbered on later runs.
    Other supported naming schemes are converted to this scheme.
    """
    if SHORT_NAME_RE.fullmatch(path.stem):
        return path, "already named"
    when, _ = original_creation_datetime(path, saved_times)
    base = "DrawThings_" + when.strftime("%Y%m%d_%H%M%S_%f")
    reserved_targets = reserved_targets if reserved_targets is not None else set()
    for index in range(1_000_000):
        name = base + ("_%d" % index if index else "") + ".png"
        dest = path.with_name(name)
        if str(dest).casefold() in reserved_targets:
            continue
        if dest == path or not dest.exists():
            return dest, "creation"
    raise ConversionError("too many short filename collisions")


def rename_no_replace(source, destination):
    """Preserve identity and never silently overwrite another file.

    Windows os.rename refuses existing destinations. POSIX os.rename replaces
    destinations, so use exclusive hard-link creation followed by unlink.
    """
    if source == destination:
        return
    if os.name == "nt":
        os.rename(source, destination)  # Windows rejects occupied destinations.
    else:
        os.link(source, destination)    # Atomic, exclusive destination creation.
        os.unlink(source)


def existing_graph_classification(scan):
    try:
        for raw in scan["texts"].get("prompt", []):
            return "FULL" if json.loads(raw) else "PARTIAL"
    except (ValueError, TypeError):
        pass
    return "FULL"  # Existing graph is present, precise label not recorded.


def convert(path, options, reserved_targets=None):
    if path.is_symlink():
        return "skip", "symlink skipped", []
    if path.suffix.lower() != ".png":
        return "skip", "not a PNG", []
    before = snapshot_times(path)
    try:
        inode = identity(path)
        scan = scan_png(path)
        has_comfy = any(key in ("workflow", "prompt") for _, _, _, key in scan["chunks"])
        if has_comfy and not options.overwrite and not options.short_filenames:
            restore_times(path, before)
            return "skip", "already has ComfyUI metadata (use --overwrite)", []
        try:
            info = drawthings_json(scan)
        except ConversionError as exc:
            if str(exc).startswith("no Draw Things JSON"):
                restore_times(path, before)
                return "skip", "NONE; no Draw Things generation metadata", []
            raise
        short_target = path
        warnings = []
        if options.short_filenames:
            short_target, time_source = short_filename_plan(path, before, reserved_targets)
        rename_only = bool(has_comfy and not options.overwrite)
        if rename_only and short_target == path:
            restore_times(path, before)
            return "skip", "already converted; short filename unchanged", warnings
        if rename_only:
            classification = existing_graph_classification(scan)
        else:
            try:
                wf, api, graph_warnings = build_workflow(
                    info, scan["dims"], ckpt_to_safetensors=options.ckpt_to_safetensors)
            except ConversionError as exc:
                wf, api, graph_warnings = informational_workflow(
                    info, scan["dims"], str(exc), ckpt_to_safetensors=options.ckpt_to_safetensors)
            warnings.extend(graph_warnings)
            classification = "FULL" if api else "PARTIAL"
        action = "rename only" if rename_only else "convert"
        if options.dry_run:
            restore_times(path, before)
            if options.short_filenames and reserved_targets is not None:
                reserved_targets.add(str(short_target).casefold())
            suffix = "; would rename to " + short_target.name if short_target != path else ""
            return "dry-run", classification + "; would " + action + suffix, warnings
    except Exception:
        # Original access times are retained even on unsupported/invalid input.
        restore_times(path, before)
        raise
    stage = None
    rollback = None
    persistent = None
    wrote = False
    renamed = False
    current = path
    try:
        if not rename_only:
            stage = temp_name(path, "stage")
            build_staged_png(path, stage, scan, wf, api, options.overwrite)
            test = scan_png(stage)
            if test["idat_sha256"] != scan["idat_sha256"] or test["xmp"] != scan["xmp"]:
                raise ConversionError("staged PNG changed image bytes or Draw Things XMP")
            for key in ("workflow", "prompt"):
                if len(test["texts"].get(key, [])) != 1:
                    raise ConversionError("staged PNG missing unique " + key)
            if json.loads(test["texts"]["workflow"][0]) != wf or json.loads(test["texts"]["prompt"][0]) != api:
                raise ConversionError("staged ComfyUI metadata differs from generated graph")
        if options.backup:
            # With shortening, make backups using the SHORT basename. That
            # avoids Windows filename-length errors on long original names.
            persistent = unique_backup(short_target if short_target != path else path)
            copy_file(path, persistent)
            shutil.copystat(path, persistent)
            rollback = persistent
        elif not rename_only:
            rollback = temp_name(path, "rollback")
            copy_file(path, rollback)
        if not rename_only:
            # Retain original inode and native Windows creation timestamp.
            with open(path, "r+b") as output, open(stage, "rb") as source:
                wrote = True
                output.seek(0)
                shutil.copyfileobj(source, output, BUF)
                output.truncate()
                output.flush()
                os.fsync(output.fileno())
            if options.verify:
                after = scan_png(path)
                if (after["idat_sha256"] != scan["idat_sha256"] or
                        after["xmp"] != scan["xmp"] or
                        json.loads(after["texts"]["workflow"][0]) != wf or
                        json.loads(after["texts"]["prompt"][0]) != api):
                    raise ConversionError("on-disk post-write verification failed")
        if short_target != path:
            rename_no_replace(path, short_target)
            current = short_target
            renamed = True
        if options.verify and rename_only:
            after = scan_png(current)
            if (after["idat_sha256"] != scan["idat_sha256"] or
                    after["xmp"] != scan["xmp"] or
                    after["texts"] != scan["texts"]):
                raise ConversionError("rename-only verification failed")
        restore_times(current, before)
        assert_times(current, before)
        if identity(current) != inode:
            raise ConversionError("original file identity changed")
        if options.short_filenames and reserved_targets is not None:
            reserved_targets.add(str(short_target).casefold())
        msg = classification + ("; renamed" if rename_only else "; converted")
        if renamed:
            msg += "; renamed to " + short_target.name
        return "renamed" if rename_only else "converted", msg, warnings
    except Exception as exc:
        recovery = ""
        if renamed:
            try:
                rename_no_replace(current, path)
                current = path
            except OSError as err:
                recovery += "; RENAME ROLLBACK FAILED: " + str(err)
        if wrote and rollback and Path(rollback).exists():
            try:
                with open(current, "r+b") as output, open(rollback, "rb") as source:
                    shutil.copyfileobj(source, output, BUF)
                    output.truncate()
                    output.flush()
                    os.fsync(output.fileno())
            except OSError as err:
                recovery += "; ROLLBACK FAILED: " + str(err)
        try:
            restore_times(current, before)
        except OSError as err:
            recovery += "; TIMESTAMP RESTORE FAILED: " + str(err)
        if persistent is not None and not wrote and not renamed:
            safe_unlink(persistent)
        return "error", str(exc) + recovery, []
    finally:
        safe_unlink(stage)
        if rollback and rollback != persistent:
            safe_unlink(rollback)


def eligible_paths(target, recursive):
    if target.is_symlink():
        raise ConversionError("symlink input is not processed")
    if target.is_file():
        if target.suffix.lower() != ".png":
            raise ConversionError("input file must be .png")
        return [target]
    if not target.is_dir():
        raise ConversionError("path not found: " + str(target))
    iterator = target.rglob("*") if recursive else target.iterdir()
    paths = [p for p in iterator if p.is_file() and not p.is_symlink() and
             p.suffix.lower() == ".png" and not BAK_RE.search(p.name)]
    return sorted(paths, key=lambda p: str(p).lower())


def print_progress(done, total):
    if not total:
        return
    width = 24
    blocks = int(width * done / total)
    bar = "#" * blocks + "-" * (width - blocks)
    print("\r[%s] %d/%d" % (bar, done, total), end="\n" if done == total else "", flush=True)


def main(argv=None):
    p = argparse.ArgumentParser(description="Draw Things -> ComfyUI PNG metadata converter, version 1.00 (retains XMP, pixels and file dates).")
    p.add_argument("path", nargs="?", default=".", help="PNG file or directory; default: current directory")
    p.add_argument("--recursive", "-r", action="store_true", help="also search subdirectories")
    p.add_argument("--backup", action="store_true", help="keep numbered .bak1.png, .bak2.png copies")
    p.add_argument("--dry-run", action="store_true", help="analyze and report; never modify files")
    p.add_argument("--overwrite", action="store_true", help="replace existing ComfyUI workflow/prompt; leave Draw Things XMP unchanged")
    p.add_argument("--ckpt-to-safetensors", action="store_true",
                   help="Normalize generated ComfyUI references and Notes by category: checkpoints/LoRAs -> .safetensors, recognized upscalers/face restorers -> .pth, conditioning models -> .safetensors/.pth; preserve original XMP")
    p.add_argument("--short-filenames", action="store_true",
                   help="optionally rename PNGs to DrawThings_YYYYMMDD_HHMMSS_ffffff.png using Date Created only; collisions get _1, _2, etc.; existing older-format names are updated without --overwrite")
    p.add_argument("--verify", action="store_true", help="verify written PNG and metadata against originals")
    p.add_argument("--report", metavar="CSV", help="write CSV report with per-file status and warnings")
    p.add_argument("--verbose", "-v", action="store_true", help="list every processed/skipped PNG with warnings")
    p.add_argument("--quiet", action="store_true", help="suppress ordinary console output")
    p.add_argument("--no-progress", action="store_true", help="disable progress bar")
    p.add_argument("--version", action="version", version="%(prog)s " + VERSION)
    args = p.parse_args(argv)
    if args.quiet and args.verbose:
        p.error("--quiet and --verbose are mutually exclusive")
    try:
        target = Path(args.path).expanduser().absolute()
        paths = eligible_paths(target, args.recursive)
    except (ConversionError, OSError) as e:
        p.error(str(e))
    rows = []
    counts = {"converted": 0, "renamed": 0, "dry-run": 0, "skip": 0, "error": 0}
    # Reserve planned destinations so --dry-run shows the same automatic
    # collision suffixes as a real batch run, without touching any files.
    reserved_targets = set()
    for i, file in enumerate(paths, 1):
        try:
            status, message, warnings = convert(file, args, reserved_targets)
        except Exception as exc:
            status, message, warnings = "error", str(exc), []
        counts[status] += 1
        classification = "NONE" if message.startswith("NONE;") else (
            "FULL" if message.startswith("FULL;") else (
            "PARTIAL" if message.startswith("PARTIAL;") else ""))
        rows.append((str(file), status, classification, message, " | ".join(warnings)))
        if not args.quiet and (args.verbose or status == "error"):
            print("\n[%s] %s: %s" % (status.upper(), file.name, message))
            if args.verbose:
                for warn in warnings:
                    print("  NOTE: " + warn)
        if not args.quiet and not args.no_progress and not args.verbose:
            print_progress(i, len(paths))
    if args.report:
        try:
            report = Path(args.report).expanduser()
            with report.open("w", encoding="utf-8-sig", newline="") as f:
                out = csv.writer(f)
                out.writerow(["file", "status", "classification", "message", "warnings"])
                out.writerows(rows)
        except OSError as e:
            print("Cannot write CSV report: " + str(e), file=sys.stderr)
            return 2
    if not args.quiet:
        print("Total %d PNGs: %d converted, %d renamed-only, %d dry-run, %d skipped, %d errors" %
              (len(paths), counts["converted"], counts["renamed"], counts["dry-run"], counts["skip"], counts["error"]))
        classified = {"FULL": 0, "PARTIAL": 0, "NONE": 0}
        for _path, _status, label, _message, _warnings in rows:
            if label in classified:
                classified[label] += 1
        print("Metadata: %d FULL, %d PARTIAL, %d NONE" %
              (classified["FULL"], classified["PARTIAL"], classified["NONE"]))
        if not paths:
            print("No eligible PNGs found (non-PNG files and backups are ignored).")
    return 1 if counts["error"] else 0





# --- Draw Things SQLite project export engine ---
# Includes image, workflow, and video export support.
# Bundled with the locked Draw Things -> ComfyUI v1.00 conversion engine.

import argparse as _ct_argparse
import base64 as _ct_base64
from datetime import datetime as _ct_datetime, timezone as _ct_timezone
import os as _ct_os
from pathlib import Path as _ct_Path
import sqlite3 as _ct_sqlite3
import struct as _ct_struct
import sys as _ct_sys
import ssl as _ct_ssl
import xml.sax.saxutils as _ct_xml_escape
import zlib as _ct_zlib
import time as _ct_time
import shutil as _ct_shutil
import math as _ct_math
import re as _ct_re

CT_VERSION = 'v1.01'
CT_MAX_DIMENSION = 32768
CT_SAFE_TOTAL_PIXELS = 180_000_000

# The indexes and defaults below follow Draw Things' published tensor_history.fbs.
# Field positions matter: changing a schema index silently corrupts conversions.
HISTORY_FIELDS = '''
lineage:long
logical_time:long
start_width:ushort
start_height:ushort
seed:uint
steps:uint
guidance_scale:float
strength:float
model:string
tensor_id:long
mask_id:long
wall_clock:long
text_edits:long=-1
text_lineage:long=-1
batch_size:uint=1
sampler:ubyte
hires_fix:bool=false
hires_fix_start_width:ushort
hires_fix_start_height:ushort
hires_fix_strength:float=0.7
upscaler:string
scale_factor:ushort=1
depth_map_id:long
generated:bool=true
image_guidance_scale:float=1.5
seed_mode:ubyte
clip_skip:uint=1
controls:[Control]
scribble_id:long
pose_id:long
loras:[LoRA]
color_palette_id:long
mask_blur:float
custom_id:long
face_restoration:string
decode_with_attention:bool=true
hires_fix_decode_with_attention:bool=true
clip_weight:float=1
negative_prompt_for_image_prior:bool=true
image_prior_steps:uint=5
data_stored:int
preview_id:long
content_offset_x:int
content_offset_y:int
scale_factor_by_120:int=120
refiner_model:string
original_image_height:uint
original_image_width:uint
crop_top:int
crop_left:int
target_image_height:uint
target_image_width:uint
aesthetic_score:float
negative_aesthetic_score:float
zero_negative_prompt:bool=false
refiner_start:float
negative_original_image_height:uint
negative_original_image_width:uint
shuffle_data_stored:int
fps_id:uint=5
motion_bucket_id:uint=127
cond_aug:float=0.02
start_frame_cfg:float=1.0
num_frames:uint=14
mask_blur_outset:int
sharpness:float
shift:float=1.0
stage_2_steps:uint=10
stage_2_cfg:float=1.0
stage_2_shift:float=1.0
tiled_decoding:bool=false
decoding_tile_width:ushort=10
decoding_tile_height:ushort=10
decoding_tile_overlap:ushort=2
stochastic_sampling_gamma:float=0.3
preserve_original_after_inpaint:bool=true
tiled_diffusion:bool=false
diffusion_tile_width:ushort=16
diffusion_tile_height:ushort=16
diffusion_tile_overlap:ushort=2
upscaler_scale_factor:ubyte
script_session_id:ulong
t5_text_encoder:bool=true
separate_clip_l:bool=false
clip_l_text:string
separate_open_clip_g:bool=false
open_clip_g_text:string
speed_up_with_guidance_embed:bool=true
guidance_embed:float=3.5
resolution_dependent_shift:bool=true
profile_data:[ubyte]
tea_cache_start:int=5
tea_cache_end:int=-1
tea_cache_threshold:float=0.06
tea_cache:bool=false
separate_t5:bool=false
t5_text:string
tea_cache_max_skip_steps:int=3
text_prompt:string
negative_text_prompt:string
clip_id:long=-1
index_in_a_clip:int
causal_inference_enabled:bool=false
causal_inference:int=3
causal_inference_pad:int
cfg_zero_star:bool=false
cfg_zero_init_steps:int
generation_time:double
reason:int
compression_artifacts:ubyte
compression_artifacts_quality:float=43.1
audio:bool=false
color_calibration:ubyte
expand_prompt_to_json:bool=false
shift_for_audio:float=3.0
uses_sol_attention:bool=false
sol_attention_start:int=2
sol_attention_tau:float=0.5
'''.strip()

LO_RA_FIELDS = 'file:string\nweight:float=0.6\nmode:ubyte'.splitlines()
CONTROL_FIELDS = '''file:string
weight:float=1
guidance_start:float
guidance_end:float=1
no_prompt:bool=false
global_average_pooling:bool=true
down_sampling_rate:float=1
control_mode:ubyte
target_blocks:[string]
input_override:ubyte'''.splitlines()


def _ct_parse_fields(lines):
    f = {}
    for idx, line in enumerate(lines):
        if '=' in line:
            left, default = line.strip().split('=', 1)
        else:
            left, default = line.strip(), None
        name, typ = left.split(':', 1)
        f[name] = (idx, typ, default)
    return f


_CT_HISTORY_SPEC = _ct_parse_fields(HISTORY_FIELDS.splitlines())
_CT_LORA_SPEC = _ct_parse_fields(LO_RA_FIELDS)
_CT_CONTROL_SPEC = _ct_parse_fields(CONTROL_FIELDS)

_CT_TYPES = {
    'bool': ('?', False), 'byte': ('b', 0), 'ubyte': ('B', 0),
    'short': ('h', 0), 'ushort': ('H', 0), 'int': ('i', 0),
    'uint': ('I', 0), 'long': ('q', 0), 'ulong': ('Q', 0),
    'float': ('f', 0.0), 'double': ('d', 0.0),
}


class CTError(Exception):
    pass


class CTFlatTable:
    """Strict read-only FlatBuffers table reader, including vectors of tables."""
    def __init__(self, buf, absolute=None):
        self.buf = buf
        self.pos = _ct_struct.unpack_from('<I', buf, 0)[0] if absolute is None else absolute
        if not (0 <= self.pos < len(buf) - 4):
            raise CTError('invalid FlatBuffers table location')
        self.vtable = self.pos - _ct_struct.unpack_from('<i', buf, self.pos)[0]
        if self.vtable < 0 or self.vtable + 4 > len(buf):
            raise CTError('invalid FlatBuffers vtable')
        self.vlength = _ct_struct.unpack_from('<H', buf, self.vtable)[0]
        if self.vtable + self.vlength > len(buf):
            raise CTError('invalid FlatBuffers vtable size')

    def _offset(self, index):
        where = self.vtable + 4 + index * 2
        if where + 2 > self.vtable + self.vlength:
            return None
        relative = _ct_struct.unpack_from('<H', self.buf, where)[0]
        return self.pos + relative if relative else None

    def scalar(self, index, fmt, default=0):
        off = self._offset(index)
        return default if off is None else _ct_struct.unpack_from('<' + fmt, self.buf, off)[0]

    def _indirect(self, index):
        at = self._offset(index)
        return None if at is None else at + _ct_struct.unpack_from('<I', self.buf, at)[0]

    def string(self, index):
        at = self._indirect(index)
        if at is None:
            return None
        count = _ct_struct.unpack_from('<I', self.buf, at)[0]
        if count > len(self.buf) - at - 4:
            raise CTError('invalid FlatBuffers string length')
        return self.buf[at+4:at+4+count].decode('utf-8')

    def vector(self, index):
        at = self._indirect(index)
        if at is None:
            return None
        count = _ct_struct.unpack_from('<I', self.buf, at)[0]
        if count > len(self.buf) - at - 4:
            raise CTError('invalid FlatBuffers vector length')
        return at + 4, count

    def get(self, field_name, spec=_CT_HISTORY_SPEC):
        idx, typ, default_text = spec[field_name]
        if typ == 'string':
            return self.string(idx)
        if typ == '[ubyte]':
            v = self.vector(idx)
            return None if v is None else self.buf[v[0]:v[0]+v[1]]
        if typ == '[string]':
            v = self.vector(idx)
            if v is None:
                return []
            at, n = v
            out = []
            for i in range(n):
                pos = at+i*4
                dest = pos + _ct_struct.unpack_from('<I', self.buf, pos)[0]
                size = _ct_struct.unpack_from('<I', self.buf, dest)[0]
                out.append(self.buf[dest+4:dest+4+size].decode('utf-8'))
            return out
        if typ in ('[LoRA]', '[Control]'):
            spec2 = _CT_LORA_SPEC if typ == '[LoRA]' else _CT_CONTROL_SPEC
            v = self.vector(idx)
            if v is None:
                return []
            at, n = v
            if n > 10000:
                raise CTError('unreasonably large nested vector')
            out = []
            for i in range(n):
                pos = at + 4*i
                node = CTFlatTable(self.buf, pos + _ct_struct.unpack_from('<I', self.buf, pos)[0])
                out.append({name: node.get(name, spec2) for name in spec2})
            return out
        fmt, default = _CT_TYPES[typ]
        if default_text is not None:
            if typ == 'bool':
                default = default_text == 'true'
            elif typ in ('float', 'double'):
                default = float(default_text)
            else:
                default = int(default_text)
        return self.scalar(idx, fmt, default)

    def dump(self):
        return {name: self.get(name) for name in _CT_HISTORY_SPEC}


_CT_SAMPLERS = (
    'DPM++ 2M Karras', 'Euler Ancestral', 'DDIM', 'PLMS', 'DPM++ SDE Karras',
    'UniPC', 'LCM', 'Euler A Substep', 'DPM++ SDE Substep', 'TCD',
    'Euler A Trailing', 'DPM++ SDE Trailing', 'DPM++ 2M AYS', 'Euler A AYS',
    'DPM++ SDE AYS', 'DPM++ 2M Trailing', 'DDIM Trailing', 'UniPC Trailing',
    'UniPC AYS', 'TCD Trailing',
)
_CT_SEED_MODES = ('Legacy', 'Torch CPU Compatible', 'Scale Alike', 'NVIDIA GPU Compatible')
_CT_COMPRESSIONS = ('disabled', 'h264', 'h265', 'jpeg')
_CT_LORA_MODES = ('all', 'base', 'refiner')
_CT_COLOR_CAL = ('none', 'lab')
_CT_INPUT_TYPES = ('unspecified', 'custom', 'depth', 'canny', 'scribble', 'pose', 'normalbae', 'color', 'lineart', 'softedge', 'seg', 'inpaint', 'ip2p', 'shuffle', 'mlsd', 'tile', 'blur', 'lowquality', 'gray')
_CT_CONTROL_MODES = ('balanced', 'prompt', 'control')


def _ct_enum(labels, n, fallback):
    return labels[n] if isinstance(n, int) and 0 <= n < len(labels) else fallback


def _ct_camel(name):
    words = name.split('_')
    return words[0] + ''.join(s.capitalize() for s in words[1:])


def _ct_history_metadata(h, prompt, negative, *, recovered_prompt=False):
    """Build DT-compatible config from raw history record, not DTM export metadata.

    No unavailable field is guessed. Changes in the upstream schema must be
    validated with native DT exports before expanding this mapping.
    """
    get = h.__getitem__
    width, height = int(get('start_width'))*64, int(get('start_height'))*64
    if not (width and height):
        raise CTError('image-history generation dimensions unavailable')
    v2 = {}
    # Fields where simple snake_case -> camelCase agrees with DT's serialized XMP.
    names = '''aesthetic_score batch_size causal_inference causal_inference_pad
    cfg_zero_init_steps cfg_zero_star clip_l_text clip_skip clip_weight crop_left crop_top
    guidance_embed guidance_scale hires_fix hires_fix_strength image_guidance_scale
    image_prior_steps mask_blur mask_blur_outset model negative_aesthetic_score
    negative_original_image_height negative_original_image_width negative_prompt_for_image_prior
    num_frames original_image_height original_image_width preserve_original_after_inpaint
    refiner_start resolution_dependent_shift sampler seed seed_mode separate_clip_l
    separate_open_clip_g separate_t5 sharpness shift speed_up_with_guidance_embed
    steps stochastic_sampling_gamma strength t5_text_encoder target_image_height
    target_image_width tea_cache tea_cache_end tea_cache_max_skip_steps tea_cache_start
    tea_cache_threshold tiled_decoding tiled_diffusion upscaler upscaler_scale_factor
    zero_negative_prompt color_calibration expand_prompt_to_json shift_for_audio
    uses_sol_attention sol_attention_start sol_attention_tau fps_id motion_bucket_id
    cond_aug start_frame_cfg refiner_model compression_artifacts compression_artifacts_quality'''.split()
    for name in names:
        value = get(name)
        if value is None:
            continue
        if name == 'fps_id': name = 'fps'
        if name == 'motion_bucket_id': name = 'motionScale'
        if name == 'cond_aug': name = 'guidingFrameNoise'
        if name == 'start_frame_cfg': name = 'startFrameGuidance'
        if name == 'compression_artifacts':
            value = _ct_enum(_CT_COMPRESSIONS, value, str(value))
        if name == 'color_calibration':
            value = _ct_enum(_CT_COLOR_CAL, value, str(value))
        v2[_ct_camel(name)] = value
    v2['width'] = width
    v2['height'] = height
    v2['batchCount'] = 1
    v2['id'] = 0
    v2['hiresFixWidth'] = int(get('hires_fix_start_width')) * 64
    v2['hiresFixHeight'] = int(get('hires_fix_start_height')) * 64
    # In the database these tiling fields represent 64-pixel units.
    for base in ('decoding_tile_width', 'decoding_tile_height', 'decoding_tile_overlap',
                 'diffusion_tile_width', 'diffusion_tile_height', 'diffusion_tile_overlap'):
        v2[_ct_camel(base)] = int(get(base)) * 64
    v2['stage2Steps'] = get('stage_2_steps')
    v2['stage2Guidance'] = get('stage_2_cfg')
    v2['stage2Shift'] = get('stage_2_shift')
    for name in ('upscaler','face_restoration','refiner_model','clip_l_text','open_clip_g_text','t5_text'):
        if get(name) is not None:
            v2[_ct_camel(name)] = get(name)
    # Only write a nonzero state if recording explicitly says causal inference
    # enabled. Do not translate an absent/unused v2 field into a video warning.
    if not get('causal_inference_enabled'):
        v2['causalInference'] = 0
    loras = []
    for item in get('loras'):
        if not item.get('file'):
            continue
        loras.append({'file': item['file'], 'weight': item['weight'],
                      'mode': _ct_enum(_CT_LORA_MODES, item['mode'], str(item['mode']))})
    v2['loras'] = loras
    # Native DT v2 JSON serializes float32 settings to about seven decimal
    # places. Preserve raw precision in the retained source FlatBuffers blob.
    for entry in v2['loras']:
        entry['weight'] = round(entry['weight'], 7)
    for key in ('hiresFixStrength', 'refinerStart', 'shift', 'strength', 'teaCacheThreshold'):
        if key in v2:
            v2[key] = round(v2[key], 7)
    controls = []
    for item in get('controls'):
        controls.append({
            'file': item['file'], 'weight': item['weight'],
            'guidanceStart': item['guidance_start'], 'guidanceEnd': item['guidance_end'],
            'noPrompt': item['no_prompt'], 'globalAveragePooling': item['global_average_pooling'],
            'downSamplingRate': item['down_sampling_rate'],
            'controlImportance': _ct_enum(_CT_CONTROL_MODES, item['control_mode'], str(item['control_mode'])),
            'targetBlocks': item['target_blocks'],
            'inputOverride': _ct_enum(_CT_INPUT_TYPES, item['input_override'], str(item['input_override'])),
        })
    v2['controls'] = controls
    if get('upscaler'):
        v2['upscaler'] = get('upscaler')
    if get('face_restoration'):
        v2['faceRestoration'] = get('face_restoration')
    if get('refiner_model'):
        v2['refinerModel'] = get('refiner_model')
    raw_profile = get('profile_data')
    try:
        profile = json.loads(raw_profile.decode('utf-8')) if raw_profile else None
    except (ValueError, UnicodeError) as exc:
        raise CTError('corrupt processing profile in history') from exc
    sampler_index = get('sampler')
    sampler = _ct_enum(_CT_SAMPLERS, sampler_index, 'Unknown Draw Things sampler ' + str(sampler_index))
    seed_mode = _ct_enum(_CT_SEED_MODES, get('seed_mode'), 'Unknown seed mode')
    result = {
        'c': prompt.rstrip(), 'uc': negative.rstrip(), 'model': get('model'), 'size': f'{width}x{height}',
        'steps': get('steps'), 'seed': get('seed'), 'scale': get('guidance_scale'),
        'strength': get('strength'), 'sampler': sampler, 'seed_mode': seed_mode,
        'v2': v2,
    }
    if profile is not None:
        result['profile'] = profile
    if get('hires_fix'):
        result['hires_fix'] = True
        result['first_stage_size'] = f"{v2['hiresFixWidth']}x{v2['hiresFixHeight']}"
        result['second_stage_strength'] = get('hires_fix_strength')
    if get('upscaler'):
        result['upscaler'] = get('upscaler')
    if get('refiner_model'):
        result['refiner'] = get('refiner_model')
        result['refiner_start'] = get('refiner_start')
    if get('tiled_decoding'):
        result['tiled_decoding'] = True
        result['decoding_tile_width'] = v2['decodingTileWidth']
        result['decoding_tile_height'] = v2['decodingTileHeight']
        result['decoding_tile_overlap'] = v2['decodingTileOverlap']
    if get('tiled_diffusion'):
        result['tiled_diffusion'] = True
    return result


def _ct_text_first_edit(conn, lineage, logical):
    """Recover a legacy first prompt only when the text-history first edit is
    unambiguous. No speculative replay of later text branches."""
    if logical != 1:
        return None
    row = conn.execute('SELECT p FROM texthistorynode WHERE __pk0=? ORDER BY __pk1 LIMIT 1', (lineage,)).fetchone()
    if row is None:
        return None
    node = CTFlatTable(row[0]); vector = node.vector(5)
    if vector is None or vector[1] == 0:
        return None
    at = vector[0]
    first = CTFlatTable(row[0], at + _ct_struct.unpack_from('<I', row[0], at)[0])
    if first.scalar(0, 'b') != 0:  # not positive text
        return None
    range_at = first._offset(1)
    if range_at is None:
        return None
    start, length = _ct_struct.unpack_from('<ii', row[0], range_at)
    if start != 0 or length != 0:  # not the initial insertion
        return None
    return first.string(2)


def _ct_missing_prompt_recovery(conn, record):
    if record['text_prompt'] is not None:
        # Native DT's PNG exporter strips spaces at the end of prompt strings.
        # Retain the exact history text separately in the raw FlatBuffer chunk.
        return record['text_prompt'].rstrip(' '), False
    text_lin = record['text_lineage']
    text_edits = record['text_edits']
    if text_lin < 0 or text_edits < 0:
        return '', False
    text = _ct_text_first_edit(conn, text_lin, text_edits)
    if text is not None:
        return text.rstrip(' '), True
    return '', False


def _ct_history_timestamp_us(raw):
    raw = int(raw)
    # Some old history records use seconds rather than microseconds. Reject
    # implausible dates rather than inventing a chronology.
    if 946684800 <= raw < 10_000_000_000:  # 2000 through 2286 as seconds
        return raw * 1_000_000, 'legacy-seconds'
    if 946684800000000 <= raw <= 20_000_000_000_000_000:
        return raw, 'microseconds'
    raise CTError(f'invalid history wall_clock value: {raw}')


def _ct_dt_from_epoch_us(us, utc=False):
    # Integer microseconds are authoritative; floating point must never be
    # used to format the microsecond component.
    whole_seconds, rem_us = divmod(int(us), 1_000_000)
    dt = (_ct_datetime.fromtimestamp(whole_seconds, tz=_ct_timezone.utc) if utc
          else _ct_datetime.fromtimestamp(whole_seconds))
    return dt.replace(microsecond=rem_us)


def _ct_png_chunk(file, kind, content):
    file.write(_ct_struct.pack('>I', len(content)))
    file.write(kind)
    file.write(content)
    file.write(_ct_struct.pack('>I', _ct_zlib.crc32(content, _ct_zlib.crc32(kind)) & 0xffffffff))


def _ct_png_itxt(keyword, value):
    # Uncompressed international text; valid PNG iTXt structure.
    return keyword.encode('latin-1') + b'\x00\x00\x00\x00\x00' + value.encode('utf-8')


def _ct_xmp(metadata):
    payload = json.dumps(metadata, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    # EXIF UserComment structure compatible with v1.00 source-XMP parser.
    # This is reconstructed database metadata, NOT Draw Things original XMP.
    return ('<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
            '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
            '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            '<rdf:Description xmlns:exif="http://ns.adobe.com/exif/1.0/">'
            '<exif:UserComment>' + _ct_xml_escape.escape(payload) + '</exif:UserComment>'
            '</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>')


def _ct_filter_scanline(current, previous, bpp, mode, np):
    """Encode one PNG scanline; PNG predictors operate on BYTES, not uint16s.

    Adaptive mode minimizes the PNG-standard sum of signed filtered byte
    magnitudes. The original unfiltered bytes are used as the next prior row.
    The filter byte and filtered data are fully reversible by any PNG decoder.
    """
    if mode not in ('adaptive', 'none'):
        raise CTError('unsupported PNG scanline filter: ' + str(mode))
    raw = np.frombuffer(current, dtype=np.uint8)
    if mode == 'none':
        return b'\x00' + current
    up = np.frombuffer(previous, dtype=np.uint8) if previous is not None else np.zeros_like(raw)
    if len(up) != len(raw):
        raise CTError('inconsistent PNG scanline width')
    left = np.zeros_like(raw)
    left[bpp:] = raw[:-bpp]
    upper_left = np.zeros_like(raw)
    upper_left[bpp:] = up[:-bpp]

    best_filter = 0
    best_data = raw
    def consider(kind, delta):
        nonlocal best_filter, best_data, best_score
        # PNG recommends minimizing the sum of abs(signed residual bytes).
        score = int(np.abs(delta.view(np.int8).astype(np.int16)).sum(dtype=np.int64))
        if score < best_score:
            best_score = score
            best_filter = kind
            best_data = delta

    best_score = int(np.abs(raw.view(np.int8).astype(np.int16)).sum(dtype=np.int64))
    consider(1, raw - left)  # Sub
    consider(2, raw - up)    # Up
    avg_predictor = ((left.astype(np.uint16) + up.astype(np.uint16)) >> 1).astype(np.uint8)
    consider(3, raw - avg_predictor)  # Average; PNG mandates floor division

    # Paeth: nearest of left, up and upper-left, ties prefer left then up.
    a = left.astype(np.int16)
    b = up.astype(np.int16)
    c = upper_left.astype(np.int16)
    predictor = a + b - c
    da = np.abs(predictor - a)
    db = np.abs(predictor - b)
    dc = np.abs(predictor - c)
    paeth = np.where((da <= db) & (da <= dc), a,
                     np.where(db <= dc, b, c)).astype(np.uint8)
    consider(4, raw - paeth)
    return bytes((best_filter,)) + best_data.tobytes()


def _ct_write_rgb_png(path, pixels, json_metadata, workflow, api,
                      original_fb, history_info, bit_depth=16,
                      png_filter="adaptive", compression_level=9, output_fd=None,
                      clip_info=None):
    """Write lossless 8- or 16-bit RGB PNG from decoded FPZIP floats.

    Source tensor RGB values are normalized to [-1, 1]. 8-bit and 16-bit
    encoding both round to the closest output value; they never downconvert
    a previously exported image, and neither path resamples image geometry.
    """
    import numpy as np
    if bit_depth not in (8, 16):
        raise CTError('unsupported PNG bit depth: ' + str(bit_depth))
    if png_filter not in ('adaptive', 'none'):
        raise CTError('unsupported PNG filter: ' + str(png_filter))
    if not isinstance(compression_level, int) or not 0 <= compression_level <= 9:
        raise CTError('PNG compression level must be 0 through 9')
    rgb = np.asarray(pixels, dtype=np.float32)
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise CTError('expected an H x W x 3 tensor')
    height, width, _ = rgb.shape
    if not (0 < height <= CT_MAX_DIMENSION and 0 < width <= CT_MAX_DIMENSION):
        raise CTError('unreasonable image dimensions')
    if width*height > CT_SAFE_TOTAL_PIXELS:
        raise CTError('image too large for safe export')
    # A file writer may use an exclusively reserved staging descriptor. Ownership of
    # that descriptor transfers to fdopen, which closes it on every path.
    with (_ct_os.fdopen(output_fd, 'wb') if output_fd is not None else open(path, 'xb')) as out:
        out.write(PNG_SIG)
        _ct_png_chunk(out, b'IHDR', _ct_struct.pack('>IIBBBBB', width, height, bit_depth, 2, 0, 0, 0))
        xmp = _ct_xmp(json_metadata)
        _ct_png_chunk(out, b'iTXt', _ct_png_itxt('XML:com.adobe.xmp', xmp))
        _ct_png_chunk(out, b'tEXt', b'workflow\0' + json_text(workflow).encode('ascii'))
        _ct_png_chunk(out, b'tEXt', b'prompt\0' + json_text(api).encode('ascii'))
        _ct_png_chunk(out, b'iTXt', _ct_png_itxt('ComfyThings:provenance', json.dumps(history_info, ensure_ascii=True, separators=(',', ':'))))
        _ct_png_chunk(out, b'iTXt', _ct_png_itxt('ComfyThings:history_flatbuffers_b64', _ct_base64.b64encode(original_fb).decode('ascii')))
        if clip_info is not None:
            _ct_png_chunk(out, b'iTXt', _ct_png_itxt('ComfyThings:clip_flatbuffers_b64',
                          _ct_base64.b64encode(clip_info['raw']).decode('ascii')))
        compressor = _ct_zlib.compressobj(level=compression_level)
        pending = bytearray()
        previous = None
        bpp = (bit_depth // 8) * 3
        def push(data):
            pending.extend(data)
            while len(pending) >= 1024*1024:
                _ct_png_chunk(out, b'IDAT', bytes(pending[:1024*1024]))
                del pending[:1024*1024]
        for row in rgb:
            # Use floating-point intermediate so quantization is not biased by
            # the FP32 multiply and handles 8-bit and 16-bit symmetrically.
            scaled = (np.clip(row, -1, 1).astype(np.float64)*0.5 + 0.5)
            if bit_depth == 8:
                packed = np.rint(scaled*255.0).astype(np.uint8, copy=False)
            else:
                packed = np.rint(scaled*65535.0).astype('>u2', copy=False)
            raw = packed.tobytes()
            encoded_row = _ct_filter_scanline(raw, previous, bpp, png_filter, np)
            push(compressor.compress(encoded_row))
            previous = raw
        push(compressor.flush())
        if pending:
            _ct_png_chunk(out, b'IDAT', bytes(pending))
        _ct_png_chunk(out, b'IEND', b'')
        out.flush()
        _ct_os.fsync(out.fileno())


def _ct_provenance_in_file(path):
    """Read early provenance, even when later PNG data is truncated (recovery path)."""
    try:
        with path.open('rb') as file:
            if file.read(8) != PNG_SIG:
                return None
            for _ in range(80):
                header = file.read(8)
                if len(header) != 8:
                    return None
                count, kind = _ct_struct.unpack('>I4s', header)
                if kind in (b'IDAT', b'IEND') or count > 16*1024*1024:
                    return None
                payload = file.read(count)
                crc = file.read(4)
                if len(payload) != count or len(crc) != 4:
                    return None
                if kind == b'iTXt' and payload.startswith(b'ComfyThings:provenance\x00'):
                    keyword, value = _text_value(kind, payload)
                    if keyword == 'ComfyThings:provenance' and value:
                        parsed = json.loads(value)
                        return parsed if isinstance(parsed, dict) else None
    except (OSError, ValueError, _ct_struct.error, ConversionError):
        pass
    return None


def _ct_choose_path(dest, stem, record_key, claimed):
    """Find this record's original filename, never commandeering unknown files."""
    for idx in range(1_000_000):
        name = stem + ('' if idx == 0 else '_' + str(idx)) + '.png'
        candidate = dest / name
        if candidate in claimed:
            continue
        if not candidate.exists():
            claimed.add(candidate)
            return candidate, False
        meta = _ct_provenance_in_file(candidate)
        if meta is not None and all(meta.get(k) == v for k, v in record_key.items()):
            claimed.add(candidate)
            return candidate, True
    raise CTError('excessive filename collisions for ' + stem)


def _ct_verify_idat_stream(path, width, height, bit_depth):
    """Stream-validate compressed pixels: zlib EOF, row sizes, filter bytes.

    scan_png validates CRCs first. This additionally detects a PNG with valid
    chunk CRCs/IEND but a truncated or malformed compressed pixel stream.
    It doesn't materialize all decoded pixels in memory.
    """
    stride = width * 3 * (bit_depth // 8) + 1
    remaining_row = stride
    rows = 0
    inflater = _ct_zlib.decompressobj()
    ended = False

    def consume(decoded):
        nonlocal remaining_row, rows
        offset = 0
        while offset < len(decoded):
            if rows >= height:
                raise CTError('PNG decompressed pixel stream exceeds expected dimensions')
            if remaining_row == stride and decoded[offset] > 4:
                raise CTError('invalid PNG scanline filter type')
            take = min(remaining_row, len(decoded) - offset)
            remaining_row -= take
            offset += take
            if remaining_row == 0:
                rows += 1
                remaining_row = stride

    with path.open('rb') as stream:
        if stream.read(8) != PNG_SIG:
            raise CTError('invalid PNG signature')
        while True:
            head = stream.read(8)
            if len(head) != 8:
                raise CTError('PNG chunk missing or truncated')
            count, kind = _ct_struct.unpack('>I4s', head)
            if kind == b'IDAT':
                while count:
                    part = stream.read(min(count, 65536))
                    if not part:
                        raise CTError('truncated PNG IDAT')
                    count -= len(part)
                    if ended:
                        raise CTError('trailing data after PNG zlib stream')
                    while part:
                        decoded = inflater.decompress(part, stride * 4)
                        consume(decoded)
                        part = inflater.unconsumed_tail
                        if inflater.eof:
                            if inflater.unused_data or part:
                                raise CTError('trailing data after PNG zlib stream')
                            ended = True
                            break
            else:
                stream.seek(count, 1)
            if len(stream.read(4)) != 4:
                raise CTError('truncated PNG CRC')
            if kind == b'IEND':
                break
    if not ended or rows != height or remaining_row != stride:
        raise CTError('incomplete PNG image data or invalid zlib stream')


def _ct_validate_export(path, expected, dims, depth, metadata, workflow, api,
                        *, deep_pixels=True, clip_info=None):
    """Validate published/staged output before treating it as complete."""
    scan = scan_png(path)  # all PNG chunk CRCs, final IEND, dimensions
    if scan['dims'] != dims:
        raise CTError('saved PNG dimensions mismatch')
    with path.open('rb') as file:
        file.seek(24)
        actual = tuple(file.read(2))
    if actual != (depth, 2):
        raise CTError(f'PNG is not {depth}-bit RGB')
    prov = (scan['texts'].get('ComfyThings:provenance') or [])
    if len(prov) != 1:
        raise CTError('missing or repeated ComfyThings provenance')
    stored = json.loads(prov[0])
    if not isinstance(stored, dict) or any(stored.get(k) != v for k, v in expected.items()):
        raise CTError('ComfyThings provenance differs from this export request')
    if drawthings_json(scan) != metadata:
        raise CTError('reconstructed Draw Things metadata mismatch')
    if len(scan['texts'].get('workflow', [])) != 1 or len(scan['texts'].get('prompt', [])) != 1:
        raise CTError('missing or repeated ComfyUI workflow/prompt')
    if json.loads(scan['texts']['workflow'][0]) != workflow or json.loads(scan['texts']['prompt'][0]) != api:
        raise CTError('ComfyUI workflow/prompt mismatch')
    if not scan['texts'].get('ComfyThings:history_flatbuffers_b64'):
        raise CTError('missing source history record')
    if clip_info is not None:
        raw = scan['texts'].get('ComfyThings:clip_flatbuffers_b64') or []
        if len(raw) != 1 or raw[0] != _ct_base64.b64encode(clip_info['raw']).decode('ascii'):
            raise CTError('missing/mismatched original video clip FlatBuffers record')
    if deep_pixels:
        _ct_verify_idat_stream(path, *dims, depth)
    return scan



def _ct_is_workflow_metadata_refresh(path, previous, expected, dims, depth,
                                     metadata, clip_info, history_blob):
    """Distinguish an intact, out-of-date CT workflow from a damaged PNG.

    Only labels a replacement as a metadata update if the original image's
    source record, full pixel stream, and original workflow all verify.
    Anything unverifiable remains a conservative validation failure.
    """
    try:
        if (not isinstance(previous, dict) or previous == expected or
                previous.get('workflow_fingerprint') == expected.get('workflow_fingerprint')):
            return False
        scan = scan_png(path)  # checks PNG CRC, dimensions and IEND
        if scan['dims'] != dims:
            return False
        with path.open('rb') as stream:
            stream.seek(24)
            if tuple(stream.read(2)) != (depth, 2):
                return False
        if drawthings_json(scan) != metadata:
            return False
        prov = scan['texts'].get('ComfyThings:provenance') or []
        if len(prov) != 1 or json.loads(prov[0]) != previous:
            return False
        raw_history = scan['texts'].get('ComfyThings:history_flatbuffers_b64') or []
        if raw_history != [_ct_base64.b64encode(history_blob).decode('ascii')]:
            return False
        if clip_info is not None:
            raw_clip = scan['texts'].get('ComfyThings:clip_flatbuffers_b64') or []
            if raw_clip != [_ct_base64.b64encode(clip_info['raw']).decode('ascii')]:
                return False
        graphs = scan['texts'].get('workflow') or []
        prompts = scan['texts'].get('prompt') or []
        if len(graphs) != 1 or len(prompts) != 1:
            return False
        old_workflow = json.loads(graphs[0])
        old_api = json.loads(prompts[0])
        validate_graph(old_workflow, old_api)
        fingerprint = hashlib.sha256((json_text(old_workflow) + '\0' +
                                      json_text(old_api)).encode('utf-8')).hexdigest()
        if fingerprint != previous.get('workflow_fingerprint'):
            return False
        # The PNG header, source lineage, workflow and image pixels must all
        # be intact before the reason can be described as metadata-only.
        _ct_verify_idat_stream(path, *dims, depth)
        return True
    except (Exception):
        return False


def _ct_check_settings(old, new):
    keys = ('ckpt_to_safetensors', 'filename_timezone', 'image_encoding',
            'png_filter', 'png_compression_level')
    differences = [f'{key}: existing={old.get(key)!r}, requested={new.get(key)!r}'
                   for key in keys if old.get(key) != new.get(key)]
    # Prevent one workflow layout from being silently mistaken for a different
    # architecture-aware graph. Re-export into a new directory
    # instead of describing a legitimate workflow upgrade as corrupted PNG.
    upgrading_video_graph = (old.get('clip_id') == new.get('clip_id') and
        old.get('frame_index') == 0 and new.get('frame_index') == 0 and
        old.get('content_type') in ('video_frame', 'video_workflow') and
        new.get('content_type') in ('video_frame', 'video_workflow'))
    old_mode = old.get('workflow_policy', 'unspecified')
    new_mode = new.get('workflow_policy', 'unspecified')
    if old_mode != new_mode and not upgrading_video_graph:
        differences.append('workflow policy: existing=%r, requested=%r' % (old_mode, new_mode))
    if (old.get('workflow_fingerprint') or new.get('workflow_fingerprint')) and (
            old.get('workflow_fingerprint') != new.get('workflow_fingerprint')) and not upgrading_video_graph:
        differences.append('workflow definition changed (catalog/graph may differ)')
    if differences:
        raise CTError('existing export has different settings (' +
                      '; '.join(differences) +
                      '). Resume with the original settings or use another output directory.')



def _ct_resume_previous_civitai_export(candidate, previous, requested, dims,
                                      bit_depth, metadata, clip_info=None):
    """Reuse *only* a fully verified existing PNG when live Civitai identification
    is unavailable. Never replace an earlier confirmed family with a generic
    fallback merely because a later network request failed.

    This does not cache external metadata in a sidecar or accept changed graphs:
    the earlier workflow is verified against its stored hash, the unchanged
    export settings and source metadata, and the complete PNG/pixel stream.
    """
    if (previous.get('model_identification') != 'confirmed' or
            previous.get('model_family_source') != 'Civitai' or
            not previous.get('workflow_fingerprint')):
        return False
    try:
        identical_settings=dict(requested)
        identical_settings['workflow_fingerprint']=previous['workflow_fingerprint']
        _ct_check_settings(previous,identical_settings)
        scan=scan_png(candidate)
        texts=scan.get('texts') or {}
        workflow_texts=texts.get('workflow') or []
        api_texts=texts.get('prompt') or []
        if len(workflow_texts)!=1 or len(api_texts)!=1:
            return False
        workflow=json.loads(workflow_texts[0])
        api=json.loads(api_texts[0])
        expected_hash=hashlib.sha256((json_text(workflow)+'\0'+json_text(api)).encode('utf-8')).hexdigest()
        if expected_hash!=previous['workflow_fingerprint']:
            return False
        validate_graph(workflow,api)
        _ct_validate_export(candidate,previous,dims,bit_depth,metadata,
                            workflow,api,deep_pixels=True,clip_info=clip_info)
        return True
    except (OSError,ValueError,TypeError,KeyError,IndexError,CTError,ConversionError):
        return False


def _ct_output_lock(dest):
    """Hold a nonblocking output lock during export and remove it afterward."""
    from contextlib import contextmanager

    @contextmanager
    def locked():
        path = dest / '.comfythings.lock'
        acquired = False
        try:
            with path.open('a+b') as lock:
                try:
                    if _ct_os.name == 'nt':
                        import msvcrt
                        lock.seek(0)
                        if lock.read(1) == b'':
                            lock.seek(0)
                            lock.write(b'\x00')
                            lock.flush()
                        lock.seek(0)
                        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except (OSError, BlockingIOError) as exc:
                    raise CTError('another ComfyThings export is using this output directory: ' + str(dest)) from exc
                acquired = True
                try:
                    yield
                finally:
                    if _ct_os.name == 'nt':
                        lock.seek(0)
                        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        finally:
            # On Windows the handle must be closed before unlinking.
            # Do not remove a lock owned by another process.
            if acquired:
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    print('    WARNING: could not remove lock file %s: %s'
                          % (path, exc), file=_ct_sys.stderr)
    return locked()


def _ct_cleanup_abandoned_staging(dest):
    """Remove abandoned staging files. Hold the exporter lock first."""
    removed = 0
    for path in dest.glob('.comfythings-*.ctpart'):
        try:
            if path.is_file() and not path.is_symlink():
                path.unlink()
                removed += 1
        except OSError as exc:
            print('    WARNING: could not clear abandoned staging file %s: %s'
                  % (path.name, exc), file=_ct_sys.stderr)
    return removed


def _ct_publish_temp(temp_path, destination, repair=False):
    """Publish fully verified bytes atomically. Never overwrite a new export.

    Hard-link publication is atomic and fails if another file owns the name.
    Repair replaces only an already-identified CT export, under our lock.
    """
    if repair:
        if not destination.exists():
            raise CTError('damaged export disappeared before repair; refusing replacement')
        _ct_os.replace(temp_path, destination)
    else:
        _ct_os.link(temp_path, destination)  # exclusive, never overwrites
        temp_path.unlink()
    if _ct_os.name != 'nt':
        # Sync directory metadata where supported. Some macOS filesystems
        # reject directory fsync; a completed atomic publication is still good.
        try:
            fd = _ct_os.open(str(destination.parent), _ct_os.O_RDONLY)
            try:
                _ct_os.fsync(fd)
            finally:
                _ct_os.close(fd)
        except OSError:
            pass


def _ct_collect_history(conn, progress=None):
    """Isolate broken history entries so other generations can still export."""
    results, errors = [], []
    scanned = 0
    for lineage, logical, blob in conn.execute(
            'SELECT __pk0,__pk1,p FROM tensorhistorynode ORDER BY __pk0,__pk1'):
        scanned += 1
        if progress is not None and scanned % 256 == 0:
            progress.update(scanned)
        try:
            tbl = CTFlatTable(blob)
            if not tbl.get('generated') or tbl.get('reason') not in (0, 1):
                continue
            h = tbl.dump()
            if int(h['seed']) < 0 or not h['model']:
                raise CTError('generated-image entry has invalid model or seed')
            unix_us, ts_kind = _ct_history_timestamp_us(h['wall_clock'])
            results.append((unix_us, lineage, logical, ts_kind, blob, h))
        except Exception as exc:
            errors.append((lineage, logical, str(exc)))
    if progress is not None:
        progress.update(scanned)
    results.sort(key=lambda r: (r[0], r[1], r[2]))
    return results, errors


def _ct_read_clips(conn, progress=None):
    """Decode clip.fbs records from Draw Things' clip table, never infer FPS.

    Frame history fps_id is not actual playback FPS; the clip table is.
    The complete raw clip FlatBuffer is also preserved in every frame PNG.
    """
    present = {r[0] for r in conn.execute('SELECT name FROM sqlite_master WHERE type="table"')}
    if 'clip' not in present:
        return {}
    found = {}
    scanned=0
    for key, raw in conn.execute('SELECT __pk0,p FROM clip ORDER BY __pk0'):
        scanned += 1
        if progress is not None and scanned % 32 == 0:
            progress.update(scanned)
        if key in found:
            raise CTError('duplicate Draw Things clip ID: %s' % key)
        table = CTFlatTable(raw)
        clip_id = table.scalar(0, 'q', 0)
        count = table.scalar(1, 'i', 0)
        fps = table.scalar(2, 'd', 0.0)
        width = table.scalar(3, 'i', 0)
        height = table.scalar(4, 'i', 0)
        audio_id = table.scalar(5, 'q', 0)
        if (clip_id != key or not 0 < count <= 1_000_000 or
                not _ct_math.isfinite(fps) or not 0 < fps < 10_000 or
                not 0 < width <= CT_MAX_DIMENSION or
                not 0 < height <= CT_MAX_DIMENSION or
                width * height > CT_SAFE_TOTAL_PIXELS):
            raise CTError('invalid Draw Things clip record for clip ID %s' % key)
        found[key] = {'id': key, 'count': count, 'fps': fps,
                      'width': width, 'height': height, 'audio_id': audio_id,
                      'raw': raw}
    if progress is not None:
        progress.update(scanned)
    return found


def _ct_preflight_video(records, clips):
    """Validate clip membership; collect frame ordering without SQLite assumptions."""
    groups = {}
    for row in records:
        raw_id = row[5].get('clip_id', -1)
        if raw_id == -1:
            continue
        if type(raw_id) is not int or raw_id < 0 or raw_id not in clips:
            raise CTError('generated frame references invalid/missing clip ID %r' % raw_id)
        index = row[5].get('index_in_a_clip')
        if type(index) is not int or not 0 <= index < clips[raw_id]['count']:
            raise CTError('clip %s has invalid frame index %r' % (raw_id, index))
        groups.setdefault(raw_id, []).append(row)
    warnings = []
    for cid, rows in groups.items():
        seen = set()
        for row in rows:
            idx = row[5]['index_in_a_clip']
            if idx in seen:
                raise CTError('clip %s contains duplicate frame index %s' % (cid, idx))
            seen.add(idx)
        if len(seen) != clips[cid]['count']:
            missing = sorted(set(range(clips[cid]['count'])) - seen)
            warnings.append('clip %s: %s of %s frames present, missing zero-based indices: %s'
                            % (cid, len(seen), clips[cid]['count'],
                               ', '.join(map(str, missing[:12])) +
                               (' ...' if len(missing) > 12 else '')))
        rows.sort(key=lambda row: row[5]['index_in_a_clip'])
        # A generation's num_frames setting does not have to equal the number
        # of frames persisted in its parent clip. Report it once per clip,
        # not once per frame (which can spam thousands of warnings).
        differing = {}
        for row in rows:
            gen_count = row[5].get('num_frames')
            if type(gen_count) is int and gen_count != clips[cid]['count']:
                differing[gen_count] = differing.get(gen_count, 0) + 1
        if differing:
            setting_summary = ', '.join('%s (%s frame histories)' % (n, count)
                                        for n, count in sorted(differing.items()))
            warnings.append('clip %s: stored clip length=%s; historical generation numFrames=%s; '
                            'using stored clip count and original frame indexes for export'
                            % (cid, clips[cid]['count'], setting_summary))
    # A clip can have a database record even when no generated frames survive.
    for cid in set(clips) - set(groups):
        warnings.append('clip %s has no eligible generated frames' % cid)
    return groups, warnings


def _ct_is_ltx23(meta, resolution):
    spec=(resolution or {}).get('spec') or {}
    return spec.get('version') == 'ltx2_3' and (resolution or {}).get('status') == 'confirmed'


def _ct_ltx23_workflow(meta, clip, resolution):
    """Native-node LTX-2.3 T2V reconstruction, structural, not pixel-exact.

    Uses node names and ports from Comfy-Org's *official* LTX-2.3 T2V
    blueprint. Original DT .ckpt asset strings and official DT text encoder
    filenames remain unchanged; no Comfy-formatted weights are fabricated.
    Unlike the official 2-stage blueprint this is a one-pass, editable
    baseline pending in-app ComfyUI validation and original I2V source media.
    """
    spec=resolution.get('spec') or {}
    encoder=spec.get('text_encoder')
    model=str(meta.get('model') or '')
    g=Graph()
    warnings=[
        'LTX-2.3 native-node single-pass baseline reconstructed from available history; not an exact replay.',
        'Original Draw Things .ckpt/quantized files may not load in ComfyUI. Select compatible ComfyUI model assets manually.',
        'Original T2V versus I2V/other conditioning source cannot be established from the recovered frame alone. No generated output frame is substituted as input.',
        'Official ComfyUI LTX-2.3 blueprint uses extra stages/conditioning that may not be present in this historical metadata. Single-pass graph requires review.',
    ]
    if not encoder:
        # No text encoder name available; never fabricate one. Keep source
        # provenance and an honest informational-only workflow instead.
        w,a,n=_ct_video_workflow(meta,clip,0,resolution=None,reconstruct=False)
        return w,a,n+warnings+['No text encoder filename from official DT catalog.']
    body='\n'.join([
        'ORIGINAL DRAW THINGS VIDEO — LTX-2.3',
        'Model: '+model,
        'Family: LTX-2.3 (official exact-filename catalog)',
        'DT text encoder: '+str(encoder),
        'Seed: '+str(meta.get('seed')),
        'Steps: '+str(meta.get('steps')),
        'Original sampler: '+str(meta.get('sampler')),
        'Guidance: '+str(meta.get('scale')),
        'Stored frames: '+str(clip['count']),
        'Recorded generation numFrames: '+str(meta.get('numFrames','not recorded')),
        'FPS from clip: '+str(clip['fps']),
        'Dimensions: %s x %s' % (clip['width'],clip['height']),
        '', 'WORKFLOW LIMITATIONS', *warnings,
        '', 'Full generation metadata and native FlatBuffers retained in this PNG.',
    ])
    g.note('LTX-2.3 VIDEO — SOURCE & LIMITATIONS',body,[-1320,0])
    g.note('ORIGINAL NEGATIVE PROMPT',str(meta.get('uc') or '(empty)'),[-1320,740])
    # Official native ComfyUI loader names; original DT names unchanged.
    ckpt=g.add('CheckpointLoaderSimple','Draw Things source model',[0,0],
               [], [('MODEL','MODEL'),('CLIP','CLIP'),('VAE','VAE')],{'ckpt_name':model})
    text=g.add('LTXAVTextEncoderLoader','LTX-2.3 text encoder',[0,320],[],
               [('CLIP','CLIP')],{'text_encoder':encoder,'ckpt_name':model,'device':'default'})
    positive=g.add('CLIPTextEncode','Positive prompt',[450,80],
                   [('clip','CLIP')],[('CONDITIONING','CONDITIONING')],{'text':meta.get('c') or ''})
    negative=g.add('CLIPTextEncode','Negative prompt',[450,400],
                   [('clip','CLIP')],[('CONDITIONING','CONDITIONING')],{'text':meta.get('uc') or ''})
    g.connect(text,'CLIP',positive,'clip');g.connect(text,'CLIP',negative,'clip')
    conditioned=g.add('LTXVConditioning','Recorded clip frame rate',[890,200],
              [('positive','CONDITIONING'),('negative','CONDITIONING')],
              [('positive','CONDITIONING'),('negative','CONDITIONING')],{'frame_rate':float(clip['fps'])})
    g.connect(positive,'CONDITIONING',conditioned,'positive')
    g.connect(negative,'CONDITIONING',conditioned,'negative')
    video=g.add('EmptyLTXVLatentVideo','Original video dimensions',[890,580],[],
                [('LATENT','LATENT')],{'width':clip['width'],'height':clip['height'],
                                      'length':clip['count'],'batch_size':1})
    audio_vae=g.add('LTXVAudioVAELoader','LTX audio VAE',[0,590],[],
                    [('Audio VAE','VAE')],{'ckpt_name':model})
    audio_lat=g.add('LTXVEmptyLatentAudio','LTX audio latent',[890,910],
                    [('audio_vae','VAE')],[('LATENT','LATENT')],
                    {'frames_number':clip['count'],'frame_rate':float(clip['fps']), 'batch_size':1})
    g.connect(audio_vae,'Audio VAE',audio_lat,'audio_vae')
    join=g.add('LTXVConcatAVLatent','Join video and audio latents',[1350,600],
               [('video_latent','LATENT'),('audio_latent','LATENT')],[('latent','LATENT')],{})
    g.connect(video,'LATENT',join,'video_latent');g.connect(audio_lat,'LATENT',join,'audio_latent')
    seed=int(meta.get('seed') or 0)
    steps=int(meta.get('steps') or 20)
    cfg=float(meta.get('scale') or 1.0)
    noise=g.add('RandomNoise','Recorded seed',[1350,40],[],[('NOISE','NOISE')],{'noise_seed':seed})
    try:
        sampler,schedule=sampler_mapping(str(meta.get('sampler') or ''),[])
    except (ValueError,ConversionError):
        sampler,schedule='euler','normal'
        warnings.append('Sampler has no confirmed ComfyUI counterpart; provisional Euler/normal used.')
    sampler_node=g.add('KSamplerSelect','Best-effort sampling method',[1350,260],[],
                       [('SAMPLER','SAMPLER')],{'sampler_name':sampler})
    guider=g.add('CFGGuider','Recovered guidance',[1850,0],
          [('model','MODEL'),('positive','CONDITIONING'),('negative','CONDITIONING')],
          [('GUIDER','GUIDER')],{'cfg':cfg})
    g.connect(ckpt,'MODEL',guider,'model')
    g.connect(conditioned,'positive',guider,'positive');g.connect(conditioned,'negative',guider,'negative')
    sigma=g.add('BasicScheduler','Provisional diffusion schedule',[1850,330],
                [('model','MODEL')],[('SIGMAS','SIGMAS')],
                {'scheduler':schedule,'steps':steps,'denoise':1.0})
    g.connect(ckpt,'MODEL',sigma,'model')
    samples=g.add('SamplerCustomAdvanced','LTX-2.3 single-pass sampler',[2320,200],
          [('noise','NOISE'),('guider','GUIDER'),('sampler','SAMPLER'),('sigmas','SIGMAS'),('latent_image','LATENT')],
          [('output','LATENT'),('denoised_output','LATENT')],{})
    for n,sn,dn in ((noise,'NOISE','noise'),(guider,'GUIDER','guider'),
                    (sampler_node,'SAMPLER','sampler'),(sigma,'SIGMAS','sigmas'),
                    (join,'latent','latent_image')):
        g.connect(n,sn,samples,dn)
    split=g.add('LTXVSeparateAVLatent','Separate generated A/V',[2790,200],
                [('av_latent','LATENT')],[('video_latent','LATENT'),('audio_latent','LATENT')],{})
    g.connect(samples,'output',split,'av_latent')
    dec=g.add('VAEDecode','Decode video frames',[3220,100],
             [('samples','LATENT'),('vae','VAE')],[('IMAGE','IMAGE')],{})
    g.connect(split,'video_latent',dec,'samples');g.connect(ckpt,'VAE',dec,'vae')
    aud=g.add('LTXVAudioVAEDecode','Decode synchronized audio',[3220,440],
             [('samples','LATENT'),('audio_vae','VAE')],[('Audio','AUDIO')],{})
    g.connect(split,'audio_latent',aud,'samples');g.connect(audio_vae,'Audio VAE',aud,'audio_vae')
    creation=g.add('CreateVideo','Create video with original FPS',[3670,150],
                [('images','IMAGE'),('audio','AUDIO')],[('VIDEO','VIDEO')],
                {'fps':float(clip['fps'])})
    g.connect(dec,'IMAGE',creation,'images');g.connect(aud,'Audio',creation,'audio')
    save=g.add('SaveVideo','Save generated video',[4100,150],
               [('video','VIDEO')],[('video','VIDEO')],
               {'filename_prefix':'video/DrawThings_LTX23','format':'mp4','codec':'auto'})
    g.connect(creation,'VIDEO',save,'video')
    w,a=g.finish()
    return w,a,warnings


def _ct_video_workflow(meta, clip, frame_index, *, resolution=None, reconstruct=False):
    """Native LTX-2.3 baseline where identified; otherwise informational Notes.

    Original generation metadata is preserved in DT XMP and FlatBuffers.
    Unsupported video families must not be presented as reconstructed graphs.
    """
    if reconstruct and frame_index == 0 and _ct_is_ltx23(meta, resolution):
        return _ct_ltx23_workflow(meta, clip, resolution)
    def wrap(value):
        return '\n'.join(part for line in str(value).splitlines() or ['']
                         for part in _note_wrap(line))

    g = Graph()
    clip_note = (
        'Draw Things exported video frame (still PNG)\n'
        'Clip ID: %s\nFrame: %s of %s (source zero-based index: %s)\n'
        'Frame rate: %s FPS\nDimensions: %s x %s\nAudio tensor reference: %s\n'
        'Audio: clip-named WAV is available when standalone audio export is explicitly enabled' % (
            clip['id'], frame_index + 1, clip['count'], frame_index,
            clip['fps'], clip['width'], clip['height'], clip['audio_id']))
    generation_frames = meta.get('numFrames')
    if (type(generation_frames) is int and generation_frames != clip['count']):
        clip_note += ('\nHistorical generation numFrames: %s; stored clip length: %s. '
                      'These settings differ; frame positions use the stored clip length.'
                      % (generation_frames, clip['count']))
    g.note('VIDEO FRAME — CLIP DETAILS', wrap(clip_note), (0, 0))
    g.note('SOURCE GENERATION CONFIGURATION', wrap(
        'Model: %s\nSeed: %s\nSteps: %s\nSampler: %s\nGuidance: %s\n'
        'All parameters: Draw Things XMP and original history FlatBuffer in this PNG' % (
            meta.get('model', ''), meta.get('seed'), meta.get('steps'),
            meta.get('sampler'), meta.get('scale'))), (470, 0))
    g.note('POSITIVE PROMPT', wrap(meta.get('c', '')), (0, 410))
    g.note('NEGATIVE PROMPT', wrap(meta.get('uc', '')), (470, 410))
    g.note('WORKFLOW STATUS — READ ONLY', wrap(
        'The original video diffusion sampling graph was not reconstructed for this frame.\n'
        'Please review conversion details for accuracy before use.\n'
        'Model-specific video architectures require model-specific ComfyUI nodes.\n'
        'This PNG includes the original Draw Things history and clip FlatBuffers,\n'
        'plus exact clip FPS / frame position in ComfyThings provenance.\n'
        'Frame history fps_id is NOT the original clip playback FPS.'), (940, 0))
    wf, api = g.finish()
    return wf, api, []


def _ct_video_directory_owner(folder, dbname, clip_id):
    """Recognize a previously exported clip from PNG *or* audio provenance.

    A video-only WAV can predate the frames on resumed runs. Its embedded
    CTID chunk is an independent, reliable ownership marker. Empty folders,
    unknown WAVs and unrelated PNGs never prove ownership.
    """
    has_audio = False
    # Current sidecar WAVs use clip names; previous layouts used
    # audio.wav. Both formats are valid ownership proof on a resumed export.
    for wav in folder.glob('*.wav'):
        if wav.is_symlink() or not wav.is_file():
            return None
        owner = _ct_wav_owner(wav)
        if (not isinstance(owner, dict) or
                owner.get('database_name') != dbname or
                owner.get('clip_id') != clip_id or
                owner.get('source') != 'ComfyThings Draw Things clip audio'):
            return None
        has_audio = True
    frames = []
    for path in folder.iterdir():
        if path.suffix.lower() != '.png':
            continue
        if path.is_symlink() or not path.is_file():
            return None
        prov = _ct_provenance_in_file(path)
        if (not isinstance(prov, dict) or
                prov.get('database_name') != dbname or
                prov.get('clip_id') != clip_id or
                prov.get('content_type') not in ('video_frame','video_workflow')):
            return None
        frames.append(path)
    has_mp4 = False
    mp4 = folder / 'video.mp4'
    if mp4.exists() or mp4.is_symlink():
        if mp4.is_symlink() or not mp4.is_file():
            return None
        owner = _ct_mp4_owner(mp4)
        if (not isinstance(owner, dict) or
                owner.get('database_name') != dbname or
                owner.get('clip_id') != clip_id or
                owner.get('source') != 'ComfyThings Draw Things clip MP4'):
            return None
        has_mp4 = True
    if not has_audio and not frames and not has_mp4:
        return None
    return {'audio': has_audio, 'frames': frames, 'mp4': has_mp4}


def _ct_choose_video_directory(root, clip, base_stem, dbname, claimed):
    """Find the clip's owned canonical directory and split-export siblings.

    Older versions put WAV-only original folders alongside _1 folders with
    frames *and another WAV*. Multiple matching WAV ownership markers are
    expected in that case, not a reason to abort the entire export. Prefer
    the owned original unsuffixed directory and reconcile extra WAVs only
    after checking their contents are identical.
    """
    cid = clip['id']
    eligible = []
    for candidate in root.glob(base_stem + '*'):
        suffix = candidate.name[len(base_stem):]
        if suffix != '' and not _ct_re.fullmatch(r'_[1-9]\d*', suffix):
            continue
        if not candidate.is_dir() or candidate.is_symlink() or candidate in claimed:
            continue
        marker = _ct_video_directory_owner(candidate, dbname, cid)
        if marker is not None:
            eligible.append((candidate, marker))
    if eligible:
        # All eligible directories prove exact DB/clip ownership. Deterministic
        # choice ensures that a resumed run never starts using a new folder.
        # The original (no numeric suffix) always wins when available.
        def rank(item):
            name = item[0].name
            suffix = name[len(base_stem):]
            return (0 if not suffix else int(suffix[1:]), name)
        eligible.sort(key=rank)
        primary = eligible[0][0]
        splits = [path for path, _ in eligible[1:]]
        claimed.update(path for path, _ in eligible)
        return primary, splits
    for idx in range(1_000_000):
        name = base_stem + ('' if idx == 0 else '_' + str(idx))
        path = root / name
        if path in claimed or path.exists() or path.is_symlink():
            continue
        claimed.add(path)
        return path, []
    raise CTError('too many video directory collisions for ' + base_stem)


def _ct_migrate_old_workflow_first_frame(videos_root, target_dir, dbname, clip_id,
                                         first_history, *, dry_run=False):
    """Move a verified first-frame PNG to <clip>/<clip>_workflow.png.

    Never mistake an unrelated or corrupted file for an owned export. The
    source PNG's old note-only workflow gets upgraded on the next normal PNG
    export; that metadata change is not a reason to re-decode other frames.
    Ownership includes full source-history lineage, not just the filename.
    """
    old_root=videos_root / 'first_frames'
    if not old_root.is_dir() or old_root.is_symlink():
        return 'none'
    unix_us,lineage,logical,_,_,h=first_history
    expected={'database_name':dbname, 'clip_id':clip_id,
              'frame_index':0,'lineage':lineage,'logical_time':logical,
              'wall_clock_raw':h['wall_clock'],'seed':h['seed']}
    # Compare metadata rather than relying on localized timestamp formatting.
    matches=[]
    for src in old_root.glob('*.png'):
        if src.is_symlink() or not src.is_file():
            continue
        owner=_ct_provenance_in_file(src)
        if owner and owner.get('content_type') in ('video_frame','video_workflow') and all(
                owner.get(k)==v for k,v in expected.items()):
            scan=scan_png(src)  # validate PNG chunks and source file identity
            if scan['dims'] != (h.get('width',scan['dims'][0]), h.get('height',scan['dims'][1])):
                raise CTError('legacy first-frame dimensions mismatch: '+str(src))
            matches.append(src)
    if not matches:
        return 'none'
    if len(matches)>1:
        raise CTError('multiple legacy first frames claim clip %s; manual inspection required' % clip_id)
    src=matches[0]
    dst=target_dir / (target_dir.name+'_workflow.png')
    if dst.exists() or dst.is_symlink():
        owner=_ct_provenance_in_file(dst) if dst.is_file() and not dst.is_symlink() else None
        if owner is None or not all(owner.get(k)==v for k,v in expected.items()):
            raise CTError('unrelated destination blocks workflow migration: '+str(dst))
        # Leave source intact if different: may encode more source-side metadata.
        if not _ct_files_identical(src,dst):
            raise CTError('two nonidentical owned workflow PNGs; refusing to discard: '+str(src))
        if not dry_run:
            src.unlink()
        result='removed duplicate'
    else:
        if not dry_run:
            target_dir.mkdir(parents=True,exist_ok=True)
            _ct_os.link(src,dst)
            if not _ct_files_identical(src,dst):
                raise CTError('legacy workflow migration changed PNG bytes: '+str(src))
            src.unlink()
        result='migrated'
    if not dry_run:
        try:
            old_root.rmdir()
        except OSError:
            pass
    return result


def _ct_normalize_legacy_video_audio(folder, dbname, clip_id, *, dry_run=False):
    """Rename only an owned legacy audio.wav to <clip-folder>.wav.

    Called on every export mode, including MP4-only, after old video folders
    are relocated/merged into output_dir/videos/. Source audio remains
    byte-identical and keeps its original timestamps. An interrupted hard-link
    rename is recoverable; conflicting or unrelated WAVs are never replaced.
    """
    legacy = folder / 'audio.wav'
    named = folder / (folder.name + '.wav')
    if not (legacy.exists() or legacy.is_symlink()):
        return 'unchanged'
    if legacy == named or legacy.is_symlink() or not legacy.is_file():
        raise CTError('clip %s: invalid legacy audio.wav; refusing rename' % clip_id)
    owner = _ct_wav_owner(legacy)
    if not (isinstance(owner, dict) and
            owner.get('source') == 'ComfyThings Draw Things clip audio' and
            owner.get('database_name') == dbname and
            owner.get('clip_id') == clip_id):
        raise CTError('clip %s: legacy audio.wav provenance does not match this clip; '
                      'refusing rename' % clip_id)
    if named.exists() or named.is_symlink():
        if (named.is_symlink() or not named.is_file() or
                _ct_wav_owner(named) != owner or
                not _ct_files_identical(legacy, named)):
            raise CTError('clip %s: named audio WAV conflicts with legacy audio.wav; '
                          'refusing automatic replacement' % clip_id)
        if not dry_run:
            legacy.unlink()  # duplicate after interrupted link+unlink
        return 'would-remove-duplicate' if dry_run else 'removed-duplicate'
    if dry_run:
        return 'would-rename'
    _ct_os.link(legacy, named)  # exclusive, same-filesystem, retains timestamps
    if not _ct_files_identical(legacy, named):
        raise CTError('clip %s: migrated WAV content changed; keeping both files' % clip_id)
    legacy.unlink()
    return 'renamed'


def _ct_files_identical(first, second):
    """Compare whole files in constant memory; do not rely on timestamps."""
    try:
        if (not first.is_file() or not second.is_file() or
                first.is_symlink() or second.is_symlink() or
                first.stat().st_size != second.stat().st_size):
            return False
        with first.open('rb') as left, second.open('rb') as right:
            while True:
                a = left.read(1024 * 1024)
                b = right.read(1024 * 1024)
                if a != b:
                    return False
                if not a:
                    return True
    except OSError:
        return False


def _ct_reunite_video_frames(primary, splits, dbname, clip_id, *, dry_run=False):
    """Reunite verified clip frames and audio from historic _1 directories.

    An older layout sometimes wrote a second audio.wav to the _1 folder. Only identical
    WAVs can be removed automatically. Preflight ALL sources and targets
    before any mutation. A different WAV or conflicting frame aborts safely.
    Use hard links followed by unlink for idempotent, non-overwriting moves.
    """
    migrations = []
    redundant_pngs = []
    redundant_wavs = []
    audio_move = None
    primary_marker = _ct_video_directory_owner(primary, dbname, clip_id)
    if primary_marker is None:
        raise CTError('canonical video directory ownership changed: ' + str(primary))
    primary_wavs = sorted(primary.glob('*.wav')) if primary_marker['audio'] else []
    if len(primary_wavs) > 1:
        if not _ct_files_identical(primary_wavs[0],primary_wavs[1]):
            raise CTError('clip %s: multiple nonidentical WAVs in canonical folder' % clip_id)
    audio_reference = primary_wavs[0] if primary_wavs else None

    for split in splits:
        marker = _ct_video_directory_owner(split, dbname, clip_id)
        if marker is None:
            raise CTError('split video directory ownership changed: ' + str(split))
        if marker.get('mp4'):
            raise CTError('clip %s: an additional owned MP4 exists in %s; '
                          'manual inspection required before folder reconciliation' %
                          (clip_id, split))
        if marker['audio']:
            for src_wav in sorted(split.glob('*.wav')):
                if audio_reference is None:
                    audio_reference = src_wav
                    audio_move = (src_wav, primary / (primary.name + '.wav'))
                else:
                    if not _ct_files_identical(audio_reference, src_wav):
                        raise CTError('clip %s: duplicate WAVs differ between %s and %s; '
                                      'no files moved or removed' %
                                      (clip_id, audio_reference.parent.name, split.name))
                    redundant_wavs.append(src_wav)
        for src in marker['frames']:
            dst = primary / src.name
            previous = _ct_provenance_in_file(src)
            if previous is None:
                raise CTError('split PNG provenance disappeared: ' + str(src))
            if dst.exists() or dst.is_symlink():
                current = (_ct_provenance_in_file(dst)
                           if dst.is_file() and not dst.is_symlink() else None)
                if current != previous:
                    raise CTError('conflicting destination PNG; refusing to merge: ' + str(dst))
                if not _ct_files_identical(src, dst):
                    raise CTError('same-provenance PNG files differ; refusing to discard: ' + str(src))
                redundant_pngs.append(src)
            else:
                migrations.append((src, dst))

    if dry_run:
        return len(migrations), len(redundant_pngs), len(redundant_wavs)
    for src, dst in ([audio_move] if audio_move else []) + migrations:
        # Exclusive link prevents overwriting an existing destination, even
        # if its contents have changed since preflight. Both live on same
        # filesystem by virtue of sharing a parent output directory.
        _ct_os.link(src, dst)
        src.unlink()
    # Reread before removing redundant files, guarding against modifications
    # after preflight. This also repairs a previous interrupted link+unlink.
    for src in redundant_pngs:
        dst = primary / src.name
        if not _ct_files_identical(src, dst):
            raise CTError('duplicate PNG changed during reconciliation: ' + str(src))
        src.unlink()
    for src in redundant_wavs:
        dst = (audio_move[1] if audio_move is not None else audio_reference)
        if not _ct_files_identical(src, dst):
            raise CTError('duplicate WAV changed during reconciliation: ' + str(src))
        src.unlink()
    for split in splits:
        try:
            split.rmdir()  # no deletion if unknown files remain
        except OSError:
            pass
    return len(migrations), len(redundant_pngs), len(redundant_wavs)


def _ct_windows_filetime_from_unix_us(unix_us):
    """Convert integer Unix microseconds to native Windows 100-ns FILETIME.

    Calculate entirely with integers (no float epoch precision loss). A
    FILETIME is measured from 1601-01-01 UTC, not local wall-clock time.
    """
    value = FILETIME_EPOCH + int(unix_us) * 10
    if not 0 <= value < (1 << 64):
        raise CTError('source timestamp outside native Windows FILETIME range')
    return value


def _ct_set_created_time(path, unix_us, *, set_birthtime):
    # All three Windows Explorer dates (Created, Modified, Accessed) must
    # follow the original DT generation history. os.utime only sets the latter
    # two on Windows. File times are set on the temporary file *before* atomic
    # publication so hard-link/repair operations retain source Created date.
    if _ct_os.name == 'nt':
        filetime = _ct_windows_filetime_from_unix_us(unix_us)
        _native_times(path, (filetime, filetime, filetime))
        return
    # On macOS, Date Created is set by default; use --no-set-birthtime to opt out.
    ns = int(unix_us)*1000
    _ct_os.utime(path, ns=(ns, ns))
    if set_birthtime and _ct_sys.platform == 'darwin':
        # macOS setattrlist expects a bare struct timespec for a write, NOT
        # getattrlist's length-prefixed attribute-return buffer. The function
        # is supported by APFS and HFS+; other mounted filesystems may reject it.
        import ctypes
        class AttrList(ctypes.Structure):
            _fields_ = [('bitmapcount', ctypes.c_ushort), ('reserved', ctypes.c_ushort),
                        ('commonattr', ctypes.c_uint), ('volattr', ctypes.c_uint),
                        ('dirattr', ctypes.c_uint), ('fileattr', ctypes.c_uint),
                        ('forkattr', ctypes.c_uint)]
        class TimeSpec(ctypes.Structure):
            _fields_ = [('tv_sec', ctypes.c_long), ('tv_nsec', ctypes.c_long)]
        attrs = AttrList(5, 0, 0x00000200, 0, 0, 0, 0)
        sec, micro = divmod(int(unix_us), 1_000_000)
        when = TimeSpec(sec, micro*1000)
        libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
        libc.setattrlist.argtypes = (ctypes.c_char_p, ctypes.POINTER(AttrList),
                                     ctypes.c_void_p, ctypes.c_size_t, ctypes.c_ulong)
        libc.setattrlist.restype = ctypes.c_int
        rv = libc.setattrlist(_ct_os.fsencode(path), ctypes.byref(attrs),
                             ctypes.byref(when), ctypes.sizeof(when), 0)
        if rv != 0:
            raise OSError(ctypes.get_errno(), 'macOS filesystem refused to set Date Created', str(path))
        # Finder/Unix stat can have small rounding differences when exposed
        # through floating-point st_birthtime on older Python versions.
        created = path.stat()
        if hasattr(created, 'st_birthtime_ns'):
            actual = created.st_birthtime_ns
            if abs(actual-ns) > 1000:
                raise OSError('macOS birthtime verification mismatch: ' + str(path))


def _ct_read_only_connection(db, *, check_integrity=True):
    # URI mode=ro: never modify project, WAL or schema; original files must
    # be copied while Draw Things is closed for a coherent WAL snapshot.
    db = _ct_Path(db).resolve()
    if not db.is_file():
        raise CTError('database not found: ' + str(db))
    # pathlib encodes Windows drive letters, spaces, '#', '?' and Unicode
    # correctly as a file URI. A hand-built 'file:C:/...?' is ambiguous on
    # Windows and can misread valid filenames containing URI metacharacters.
    c = _ct_sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)
    try:
        if check_integrity:
            answer = c.execute('PRAGMA quick_check').fetchone()
            if answer is None or answer[0] != 'ok':
                raise CTError('SQLite quick_check failed: ' + str(answer))
        tables = {x[0] for x in c.execute('SELECT name FROM sqlite_master WHERE type="table"')}
        if not {'tensorhistorynode', 'tensordata', 'tensors', 'texthistorynode'} <= tables:
            raise CTError('unsupported Draw Things database schema')
    except Exception:
        c.close()
        raise
    return c


def _ct_iter_records(conn):
    for lineage, logical, blob in conn.execute('SELECT __pk0,__pk1,p FROM tensorhistorynode ORDER BY __pk0,__pk1'):
        tbl = CTFlatTable(blob)
        if not tbl.get('generated') or tbl.get('reason') not in (0, 1):
            continue
        record = tbl.dump()
        if int(record['seed']) < 0 or not record['model']:
            continue
        yield (lineage, logical, blob, record)


def _ct_tensor_index(conn, progress=None):
    """Index source images, marking ambiguous/invalid tensor links as errors.

    Multiple tensors for the same history entry must never be resolved by
    silently taking whichever SQLite row happened to come last.
    """
    idx = {}
    for scanned, (lin, logical, slot, blob) in enumerate(
            conn.execute('SELECT __pk0,__pk1,__pk2,p FROM tensordata'), 1):
        if progress is not None and scanned % 256 == 0:
            progress.update(scanned)
        key = (lin, logical)
        if key in idx:
            idx[key] = CTError('ambiguous image tensor references for lineage=%s logical_time=%s'
                               % key)
            continue
        try:
            item = CTFlatTable(blob)
            tensor_id = item.scalar(8, 'q', 0)
            width = item.scalar(5, 'i', 0)
            height = item.scalar(6, 'i', 0)
            if (not tensor_id or not 0 < width <= CT_MAX_DIMENSION or
                    not 0 < height <= CT_MAX_DIMENSION or
                    width * height > CT_SAFE_TOTAL_PIXELS):
                raise CTError('invalid dimensions or image tensor ID')
            idx[key] = (tensor_id, width, height)
        except Exception as exc:
            idx[key] = CTError('bad image tensor for lineage=%s logical_time=%s: %s'
                               % (lin, logical, exc))
    if progress is not None:
        progress.update(scanned if idx else 0)
    return idx



def _ct_confirm_later_nongenerated_empty_slot_references(
        conn, references, target_key, tensor_id, dimensions, target_history):
    """Confirm all other uses are later, non-generated history markers.

    Conservative association rules cannot select a new output solely when it is
    referenced elsewhere. Draw Things may record a generated output in later
    non-generation history entries (reason=4), with the same missing-ID slot-0
    marker plus the image in slot 1. These records are not competing outputs.

    This fallback never accepts a competing generated history, an earlier
    reference, unknown reason, altered dimensions, extra valid tensor, or
    missing raw history. It cannot establish semantics of arbitrary images;
    it applies only to the narrowly defined structural pattern described above.
    """
    others = references - {target_key}
    if not others:
        return False
    try:
        target_us, _ = _ct_history_timestamp_us(target_history['wall_clock'])
    except (KeyError, ValueError, TypeError, CTError, OverflowError):
        return False
    for other_key in others:
        try:
            rec = conn.execute(
                'SELECT p FROM tensorhistorynode WHERE __pk0=? AND __pk1=?',
                other_key).fetchall()
            if len(rec) != 1:
                return False
            other = CTFlatTable(rec[0][0])
            h = other.dump()
            other_us, _ = _ct_history_timestamp_us(h['wall_clock'])
            if not (h['generated'] is False and h['reason'] == 4
                    and h['clip_id'] == -1 and h['tensor_id'] == 0
                    and h['mask_id'] == 0 and other_us > target_us
                    and (h['original_image_width'], h['original_image_height']) == dimensions
                    and (h['target_image_width'], h['target_image_height']) == dimensions):
                return False
            raw_rows = conn.execute(
                'SELECT __pk2,p FROM tensordata WHERE __pk0=? AND __pk1=?',
                other_key).fetchall()
            if len(raw_rows) != 2 or {row[0] for row in raw_rows} != {0, 1}:
                return False
            slots = {slot: CTFlatTable(blob) for slot, blob in raw_rows}
            empty, image = slots[0], slots[1]
            if not (empty._offset(8) is None and int(empty.scalar(8, 'q', 0)) == 0
                    and (int(empty.scalar(5, 'i', 0)), int(empty.scalar(6, 'i', 0))) == dimensions
                    and image._offset(8) is not None
                    and int(image.scalar(8, 'q', 0)) == tensor_id
                    and (int(image.scalar(5, 'i', 0)), int(image.scalar(6, 'i', 0))) == dimensions):
                return False
        except Exception:
            return False
    return True


def _ct_confirm_strict_append_successor(conn, key, rows, target, references):
    """Optional, narrowly constrained still-output provenance fallback.

    A generated still may have three or more simultaneously present image tensors,
    all carried forward to the immediately following generation.
    Reference counts alone cannot distinguish provenance. In some image-editing sequences, the final slot in the earlier
    history matched that generation's thumbnail, and an immediate successor
    appended a distinct output slot.

    This rule requires a 3+-tensor contiguous, uniform-dimension history
    retained byte-for-byte (by ID and metadata) as the prefix of an immediate
    successor's strictly one-longer sequence.  It never selects candidates in
    unrelated histories, 2-slot histories, rearranged histories, videos,
    histories with source/masks, or when essential evidence is missing.

    The source JPEG is *not* decoded for automated visual recognition; it is
    a historic provenance marker only. The feature remains OPT-IN and
    reports each newly eligible history.  Do not claim that
    a thumbnail's existence proves which pixels it depicts.
    """
    if not (3 <= len(rows) <= 8 and all(row is not None and row[4] for row in rows)):
        return None
    ordered = sorted(rows, key=lambda r: r[0])
    n = len(ordered)
    if [r[0] for r in ordered] != list(range(n)):
        return None
    ids = [r[1] for r in ordered]
    if len(set(ids)) != n:
        return None
    dims = (ordered[-1][2], ordered[-1][3])
    if any((r[2], r[3]) != dims for r in ordered):
        return None
    if any(r[5] for r in ordered):
        return None
    if not (target.get('clip_id') == -1 and target.get('reason') == 1 and
            target.get('tensor_id') == 0 and target.get('mask_id') == 0 and
            target.get('batch_size', 1) == 1 and
            (target.get('target_image_width'), target.get('target_image_height')) == dims and
            (target.get('original_image_width'), target.get('original_image_height')) == dims and
            target.get('data_stored') == n and
            int(target.get('preview_id') or 0) > 0 and
            int(target.get('text_edits') or 0) > 0 and
            int(target.get('text_lineage') or 0) >= 0):
        return None
    successor_key = (key[0], key[1] + 1)
    # Every old tensor is confined to these two histories; another history
    # could indicate that the order is not a simple editing sequence.
    if any(references.get(tid, set()) != {key, successor_key} for tid in ids):
        return None
    try:
        next_raw = conn.execute(
            'SELECT p FROM tensorhistorynode WHERE __pk0=? AND __pk1=?',
            successor_key).fetchall()
        if len(next_raw) != 1:
            return None
        following = CTFlatTable(next_raw[0][0]).dump()
        if not (following.get('generated') is True and
                following.get('clip_id') == -1 and
                following.get('reason') == target.get('reason') and
                following.get('model') == target.get('model') and
                following.get('text_edits') == target.get('text_edits') and
                following.get('text_lineage') == target.get('text_lineage') and
                following.get('tensor_id') == 0 and
                following.get('mask_id') == 0 and
                following.get('batch_size', 1) == 1 and
                (following.get('original_image_width'), following.get('original_image_height')) == dims and
                (following.get('target_image_width'), following.get('target_image_height')) == dims and
                following.get('data_stored') == n + 1 and
                int(following.get('preview_id') or 0) > 0 and
                following.get('preview_id') != target.get('preview_id') and
                following.get('seed') != target.get('seed')):
            return None
        prev_us, _ = _ct_history_timestamp_us(target['wall_clock'])
        next_us, _ = _ct_history_timestamp_us(following['wall_clock'])
        if next_us <= prev_us:
            return None
        # Require a visible editing-state transition in the source history;
        # unchanged framing alone is insufficient provenance.
        if (target.get('content_offset_x'), target.get('content_offset_y'),
                target.get('scale_factor_by_120')) == (
                    following.get('content_offset_x'), following.get('content_offset_y'),
                    following.get('scale_factor_by_120')):
            return None
        next_rows = conn.execute(
            'SELECT __pk2,p FROM tensordata WHERE __pk0=? AND __pk1=? ORDER BY __pk2',
            successor_key).fetchall()
        if len(next_rows) != n + 1 or [r[0] for r in next_rows] != list(range(n+1)):
            return None
        next_slots = []
        for slot, blob in next_rows:
            tab = CTFlatTable(blob)
            tid = int(tab.scalar(8, 'q', 0))
            wh = (int(tab.scalar(5, 'i', 0)), int(tab.scalar(6, 'i', 0)))
            if tid <= 0 or wh != dims or tab._offset(8) is None:
                return None
            next_slots.append((tid, wh))
        if next_slots[:n] != [(r[1], dims) for r in ordered]:
            return None
        added_id = next_slots[-1][0]
        if added_id in ids:
            return None
        # Both original DT thumbnails must still exist.  Existence is *not*
        # a pixel-level image match; confirm the exported result independently.
        for preview_id in (target['preview_id'], following['preview_id']):
            if conn.execute('SELECT 1 FROM thumbnailhistorynode WHERE __pk0=?',
                    (preview_id,)).fetchone() is None:
                return None
        # Indexed BLOB existence, no FPZIP decode and no unindexed full scan.
        if not conn.execute('SELECT length(data) FROM tensors WHERE name=?',
                ('tensor_history_' + str(added_id),)).fetchone()[0] > 0:
            return None
        return ordered[-1]
    except (ValueError, KeyError, TypeError, IndexError, OverflowError,
            _ct_sqlite3.Error, CTError, AttributeError):
        return None


def _ct_resolve_still_tensor_references(conn, index, histories, progress=None, *,
                                        allow_empty_slot=True, placeholder_resolved=None,
                                        shared_placeholder_resolved=None,
                                        allow_editing_append=False, editing_append_resolved=None):
    """Conservatively resolve Draw Things still-image source/output tensors.

    A generation can retain previous/source images alongside its new output.
    Rule A: one globally single-history output candidate in a higher
    slot; ALL lower-slot inputs are reused in other histories.

    Rule B: a generated output can itself be reused in *later* histories.
    It is eligible only when EXACTLY ONE candidate is first seen at the target
    history, all its other references are strictly later histories within the
    same lineage, each competing lower-slot tensor existed before the target
    history, and each source is referenced more often than the output. This
    is checked over the full metadata corpus, not inferred from slot alone.
    If candidates tie or no earlier source exists, leave the record ambiguous.

    Optional Rule C considers ONLY an unresolved 3+-slot history that is
    an exact retained prefix of the immediately next append-one generation,
    with strong matching history/source metadata. It remains OFF by default as a conservative release policy. Never overrides established rules or
    recovered empty-slot choices. No blanket last-slot selection.

    Neither rule guesses pixels, stretches tensors, changes FPZIP values, or
    alters the already resolved single-tensor/video mappings.

    Metadata-only scan: tensordata.p is small FlatBuffers metadata; never read
    tensors.data. Payload existence/length is checked only for finalists via
    indexed SQLite lookups. Guard against unindexed 54GB BLOB table scans.
    """
    # Generated histories in the original list are authoritative for expected
    # dimensions. The strict two-slot fallback and later non-generated
    # reference variant require independent structural corroboration.
    # Do not broaden to arbitrary malformed metadata or slot-order guessing.
    history_by_key = {(lin, logical): h for _, lin, logical, _, _, h in histories}
    targets = {
        (lin, logical) for _, lin, logical, _, _, h in histories
        if h.get('clip_id') == -1 and isinstance(index.get((lin, logical)), CTError)
        and str(index[(lin, logical)]).startswith('ambiguous image tensor references')
    }
    if not targets:
        if progress is not None:
            progress.update(0)
        return {}
    # Count distinct history references, not duplicated slot rows.
    references = {}
    candidates = {key: [] for key in targets}
    scanned = 0
    for lin, logical, slot, blob in conn.execute(
            'SELECT __pk0,__pk1,__pk2,p FROM tensordata'):
        scanned += 1
        key = (lin, logical)
        try:
            data = CTFlatTable(blob)
            tid = int(data.scalar(8, 'q', 0))
            width = int(data.scalar(5, 'i', 0))
            height = int(data.scalar(6, 'i', 0))
            valid = (tid > 0 and 0 < width <= CT_MAX_DIMENSION
                     and 0 < height <= CT_MAX_DIMENSION
                     and width * height <= CT_SAFE_TOTAL_PIXELS)
            if valid:
                references.setdefault(tid, set()).add(key)
            if key in targets:
                # Preserve which fields are present, not just their numeric
                # default values. Draw Things encodes empty placeholders as
                # tensor-ID field absent (there is no tensor ID zero BLOB).
                no_id_field = (data._offset(8) is None if
                               allow_empty_slot and hasattr(data, '_offset') else False)
                candidates[key].append((slot, tid, width, height, valid, no_id_field))
        except Exception:
            if key in targets:
                candidates[key].append(None)
    # This target table is normally indexed by name. Without a usable index,
    # refuse costly per-candidate access (and don't misreport a source tensor).
    try:
        plan = ' '.join(str(row[3]).upper() for row in conn.execute(
            'EXPLAIN QUERY PLAN SELECT length(data) FROM tensors WHERE name=?',
            ('tensor_history_0',)))
        indexed = 'SEARCH ' in plan and 'SCAN ' not in plan
    except Exception:
        indexed = False
    resolved = {}
    for i, key in enumerate(sorted(targets), 1):
        if progress is not None and (i % 25 == 0 or i == len(targets)):
            progress.update(i)
        rows = candidates[key]
        if not indexed or len(rows) < 2:
            continue
        # Guarded two-record fallback (enabled by default).
        # Slot 0 has an absent tensor ID; slot 1 is the sole valid tensor with
        # the expected dimensions and an indexed payload. Cross-history reuse
        # is accepted ONLY if every other reference is a later non-generated
        # reason-4 marker retaining precisely this empty-ID/slot-1 pattern.
        # Previously resolved associations are not changed.
        if allow_empty_slot and len(rows) == 2 and all(row is not None for row in rows):
            by_slot = {row[0]: row for row in rows}
            empty, candidate = by_slot.get(0), by_slot.get(1)
            h = history_by_key.get(key) or {}
            if (len(by_slot) == 2 and empty is not None and candidate is not None
                    and empty[1] == 0 and empty[5] and not empty[4]
                    and candidate[4] and candidate[1] > 0 and not candidate[5]
                    and (empty[2], empty[3]) == (candidate[2], candidate[3])
                    and h.get('reason') == 1 and h.get('mask_id') == 0
                    # HISTORY_FIELDS names the source tensor 'tensor_id', not
                    # 'source_tensor_id'. The latter is only a report label.
                    and h.get('tensor_id') == 0
                    and (candidate[2], candidate[3]) == (
                        h.get('target_image_width'), h.get('target_image_height'))
                    and (candidate[2], candidate[3]) == (
                        h.get('original_image_width'), h.get('original_image_height'))):
                ref_keys = references.get(candidate[1], set())
                shared_non_generated = (ref_keys != {key}
                    and key in ref_keys and _ct_confirm_later_nongenerated_empty_slot_references(
                        conn, ref_keys, key, candidate[1],
                        (candidate[2], candidate[3]), h))
                if not (ref_keys == {key} or shared_non_generated):
                    continue
                try:
                    payload = conn.execute(
                        'SELECT length(data) FROM tensors WHERE name=?',
                        ('tensor_history_' + str(candidate[1]),)).fetchone()
                except Exception:
                    payload = None
                if payload and isinstance(payload[0], int) and payload[0] > 0:
                    result = (candidate[1], candidate[2], candidate[3])
                    if isinstance(index.get(key), CTError) and str(index[key]).startswith(
                            'ambiguous image tensor references'):
                        index[key] = result
                        resolved[key] = result
                        if placeholder_resolved is not None:
                            placeholder_resolved.add(key)
                        if shared_non_generated and shared_placeholder_resolved is not None:
                            shared_placeholder_resolved.add(key)
                        continue
        if any(row is None or not row[4] for row in rows):
            continue
        # Distinct candidate IDs, consistent metadata, and unambiguous slots.
        ids = {row[1] for row in rows}
        if len(ids) != len(rows) or len({row[0] for row in rows}) != len(rows):
            continue
        novel = [row for row in rows if len(references.get(row[1], ())) == 1]
        reused = [row for row in rows if len(references.get(row[1], ())) >= 2]
        chosen = None
        editing_chosen = False
        if len(novel) == 1 and len(reused) == len(rows) - 1:
            # Unique-output rule: a new output has never been carried into
            # another history, unlike the lower-slot source/reference tensors.
            candidate = novel[0]
            if all(isinstance(row[0], int) and candidate[0] > row[0]
                   for row in reused):
                chosen = candidate
        if chosen is None and len(rows) == 2:
            # Later-reuse rule: a genuine output can be fed into subsequent
            # generations. The first reference is the target; all others
            # MUST be strictly later in the SAME lineage. The competing
            # source must already have appeared before this history.
            # Require its reference count to exceed the output's as an
            # additional corroboration, not as the sole selection criterion.
            eligible = []
            for candidate in rows:
                others = [row for row in rows if row is not candidate]
                if len(others) != 1:
                    continue
                source = others[0]
                if (not isinstance(candidate[0], int) or
                        not isinstance(source[0], int) or
                        candidate[0] <= source[0]):
                    continue
                out_refs = references.get(candidate[1], set())
                source_refs = references.get(source[1], set())
                if (len(out_refs) < 2 or len(source_refs) <= len(out_refs) or
                        key not in out_refs or
                        not all(lin == key[0] and logical > key[1]
                                for lin, logical in out_refs if (lin, logical) != key) or
                        not any(logical < key[1] for _, logical in source_refs)):
                    continue
                eligible.append(candidate)
            if len(eligible) == 1:
                chosen = eligible[0]
        if chosen is None and allow_editing_append:
            h = history_by_key.get(key) or {}
            append_choice = _ct_confirm_strict_append_successor(
                conn, key, rows, h, references)
            if append_choice is not None:
                chosen = append_choice
                editing_chosen = True
        if chosen is None:
            continue
        tid = chosen[1]
        try:
            length = conn.execute(
                'SELECT length(data) FROM tensors WHERE name=?',
                ('tensor_history_' + str(tid),)).fetchone()
        except Exception:
            continue
        if not length or not isinstance(length[0], int) or length[0] <= 0:
            continue
        resolution = (tid, chosen[2], chosen[3])
        # Only replace the expected ambiguity; never overwrite a known image
        # tensor, a video tensor, or another type of error.
        if isinstance(index.get(key), CTError) and str(index[key]).startswith(
                'ambiguous image tensor references'):
            index[key] = resolution
            resolved[key] = resolution
            if editing_chosen and editing_append_resolved is not None:
                editing_append_resolved.add(key)
    return resolved

def _ct_resolve_video_tensor_streams(conn, index, clip_groups, clips, progress=None):
    """Select generated frames from unambiguous, clip-wide tensor *streams*.

    Draw Things may retain one or more I2V source/conditioning images alongside
    each generated frame. All have the same history key, and multiple inputs
    may even have the same dimensions as the generated video (e.g. two 1280x768
    tensors in each history). Ambiguous associations require extra evidence.

    A reliable distinguishing signal is a consistent slot across the COMPLETE
    clip, containing a *different* valid tensor at each frame while competing
    slots contain the same, repeated reference image. We require that pattern;
    no positional 'last slot wins', highest-resolution or arbitrary tie-break.

    Only clips with currently ambiguous associations are rechecked. Existing
    mapping remains unchanged for standalone images and other clips. Resolved
    tuples replace errors in the shared index so workflow/frame PNG generation,
    MP4 fingerprinting and raw-video encoding all use exactly the same tensors.

    This is read-only: SQL only requests tiny FlatBuffer metadata and blob
    length, NEVER tensor payloads. The actual FPZIP stream is decoded later.
    """
    repaired = {}
    scanned_frames=0
    for cid, rows in clip_groups.items():
        if progress is not None:
            progress.update(scanned_frames)
        scanned_frames += len(rows)
        if not any(isinstance(index.get((r[1], r[2])), Exception) for r in rows):
            continue
        clip = clips[cid]
        dimensions = (clip['width'], clip['height'])
        slots_by_frame = []
        payload_cache = {}
        # A clip's frames are already sorted by original index_in_a_clip.
        # Refuse to infer a cross-frame stream on incomplete clip histories.
        if len(rows) != clip['count']:
            continue
        for row in rows:
            lin, logical = row[1:3]
            available = {}
            for slot, blob in conn.execute(
                    'SELECT __pk2,p FROM tensordata WHERE __pk0=? AND __pk1=? '
                    'ORDER BY __pk2', (lin, logical)):
                try:
                    data = CTFlatTable(blob)
                    tid = data.scalar(8, 'q', 0)
                    width = data.scalar(5, 'i', 0)
                    height = data.scalar(6, 'i', 0)
                    if (not tid or (width, height) != dimensions or
                            width > CT_MAX_DIMENSION or height > CT_MAX_DIMENSION or
                            width * height > CT_SAFE_TOTAL_PIXELS):
                        continue
                    if tid not in payload_cache:
                        # length(data) reads SQLite's BLOB length without
                        # transferring potentially multi-megabyte FPZIP bytes.
                        payload_cache[tid] = conn.execute(
                            'SELECT length(data) FROM tensors WHERE name=?',
                            ('tensor_history_' + str(tid),)).fetchone()
                    if (payload_cache[tid] is None or
                            not payload_cache[tid][0] or payload_cache[tid][0] <= 0):
                        continue
                    # A repeated __pk2 slot would make association ambiguous.
                    if slot in available:
                        available[slot] = None
                    else:
                        available[slot] = tid
                except Exception:
                    # Malformed candidate cannot be used as a generated frame.
                    continue
            slots_by_frame.append(available)
            if progress is not None and len(slots_by_frame) % 64 == 0:
                progress.update(scanned_frames - len(rows) + len(slots_by_frame))
        if not slots_by_frame:
            continue
        # A valid stream must occupy one stable slot in *every* frame.
        shared = set(slots_by_frame[0])
        for frame in slots_by_frame[1:]:
            shared.intersection_update(frame)
        streams = {}
        for slot in shared:
            ids = tuple(frame[slot] for frame in slots_by_frame)
            if all(tid is not None for tid in ids):
                streams[slot] = ids
        if not streams:
            continue
        # Coalesce exact duplicate sequences appearing under different slots.
        # The frame image bytes will be identical; slot position is irrelevant.
        distinct_streams = list(dict.fromkeys(streams.values()))
        if len(distinct_streams) == 1:
            chosen = distinct_streams[0]
        else:
            # Across multi-frame clips the generated images have distinct
            # source tensor IDs; carried-over I2V sources are constant. Only
            # select when exactly one stream has a unique ID in EVERY frame
            # and ALL competing streams are fixed source images. Two moving
            # streams could both be legitimate videos: fail, never guess.
            moving = [ids for ids in distinct_streams
                      if len(set(ids)) == len(rows) and len(rows) > 1]
            nonmoving = [ids for ids in distinct_streams
                         if len(set(ids)) != len(rows)]
            if (len(moving) != 1 or len(nonmoving) != len(distinct_streams) - 1
                    or any(len(set(ids)) != 1 for ids in nonmoving)):
                continue
            chosen = moving[0]
        # A selected multi-frame video must actually vary by frame.  A lone
        # same-size candidate could still be a fixed input/reference tensor,
        # even when other recorded tensors have different dimensions.
        # Reject rather than accidentally encode one source image repeatedly.
        if len(rows) > 1 and len(set(chosen)) != len(rows):
            continue
        for row, tensor_id in zip(rows, chosen):
            key = (row[1], row[2])
            old = index.get(key)
            # Preserve confirmed single-reference records, if any; never
            # rewrite them with a conflicting stream assignment.
            if old is not None and not isinstance(old, Exception) and old != (
                    tensor_id, dimensions[0], dimensions[1]):
                break
        else:
            count = 0
            for row, tensor_id in zip(rows, chosen):
                key = (row[1], row[2])
                if isinstance(index.get(key), Exception):
                    count += 1
                index[key] = (tensor_id, *dimensions)
            repaired[cid] = count
    return repaired


def _ct_decode_pixels(conn, tensor_id, width, height):
    try:
        import fpzip
        import numpy as np
    except ImportError as exc:
        raise CTError('image extraction requires: python3 -m pip install numpy fpzip') from exc
    if width > CT_MAX_DIMENSION or height > CT_MAX_DIMENSION or width*height > CT_SAFE_TOTAL_PIXELS:
        raise CTError('unreasonable tensor image size')
    row = conn.execute('SELECT data FROM tensors WHERE name=?', ('tensor_history_'+str(tensor_id),)).fetchone()
    if row is None:
        raise CTError('image tensor not found: ' + str(tensor_id))
    try:
        decompressed = fpzip.decompress(row[0], order='C')
    except Exception as exc:
        raise CTError('FPZIP decode failed for tensor ' + str(tensor_id) + ': ' + str(exc)) from exc
    arr = np.asarray(decompressed, dtype=np.float32)
    if arr.size != width * height * 3:
        raise CTError(f'tensor shape mismatch for id {tensor_id}: {arr.shape}, expected {width}x{height}x3')
    arr = arr.reshape((height, width, 3))
    if not np.isfinite(arr).all():
        raise CTError('image contains non-finite FPZIP values')
    return arr


def _ct_explain_note(note):
    """Improve console explanation; do not change the frozen converter graph."""
    match = re.fullmatch(r'generation size (\d+x\d+) differs from stored PNG (\d+x\d+)', note)
    if match:
        return ('generation-stage resolution ' + match.group(1) +
                '; final stored image resolution ' + match.group(2) +
                ' (full stored resolution exported; downstream processing may explain difference)')
    return note




_CT_BANNER = '░░░░▒▒▒▒▓▓▓▓████ ℂ𝕠𝕞𝕗𝕪𝕋𝕙𝕚𝕟𝕘𝕤 ████▓▓▓▓▒▒▒▒░░░░'


def _ct_console_header(version):
    """Show the Unicode title in a capable interactive terminal.

    Fall back to the versioned ASCII label for logs, redirected output,
    and terminals that cannot encode the title. NO_COLOR disables bold
    but does not remove the visible Unicode banner.
    """
    label = f'ComfyThings {version}'
    out = _ct_sys.stdout
    if not out.isatty() or _ct_os.environ.get('TERM', '') == 'dumb':
        return label
    try:
        _CT_BANNER.encode(out.encoding or 'utf-8', 'strict')
    except (UnicodeError, LookupError):
        return label
    if 'NO_COLOR' in _ct_os.environ:
        return _CT_BANNER
    if _ct_os.name == 'nt' and not (
        _ct_os.environ.get('WT_SESSION') or
        _ct_os.environ.get('ANSICON') or
        _ct_os.environ.get('ConEmuANSI', '').upper() == 'ON' or
        _ct_os.environ.get('TERM', '').startswith(('xterm', 'vt100'))
    ):
        return _CT_BANNER
    return f'\x1b[1m{_CT_BANNER}\x1b[0m'


class _CTProgress:
    """One-line, dependency-free, terminal-adaptive export progress.

    Interactive TTY: a colored Unicode bar where supported, ASCII otherwise;
    log/redirected output: no control characters or artificial progress lines.
    Export errors and recovery warnings remain visible. Informational
    workflow notices are printed only with --verbose, but retained in PNGs.
    """

    def __init__(self, total, *, enabled=True, planned=False, show_detail=False, animate=False):
        self.show_detail = show_detail
        self.total = max(0, total)
        self.done = 0
        self.saved = 0
        self.reused = 0
        self.errors = 0
        self.planned = planned
        self.started = _ct_time.monotonic()
        self.stream = _ct_sys.stdout
        self.active = bool(enabled and self.stream.isatty() and
                           _ct_os.environ.get('TERM', '') != 'dumb')
        self.color = bool(self.active and
                          _ct_os.environ.get('TERM', '') not in ('', 'dumb') and
                          not _ct_os.environ.get('NO_COLOR'))
        if _ct_os.name == 'nt':
            # Legacy Windows consoles can redraw with carriage returns, but
            # ANSI escape codes are not reliably supported without VT mode.
            # Windows Terminal, ConEmu and ANSICON announce ANSI capability.
            self.color = bool(self.color and (
                _ct_os.environ.get('WT_SESSION') or
                _ct_os.environ.get('ANSICON') or
                _ct_os.environ.get('ConEmuANSI', '').upper() == 'ON' or
                _ct_os.environ.get('TERM', '').startswith(('xterm', 'vt100'))))
        try:
            '█▓▒░·'.encode(self.stream.encoding or 'utf-8', 'strict')
            self.unicode = True
        except (UnicodeError, LookupError):
            self.unicode = False
        self._last_width = 0
        self._visible = False
        # Display-only thread: no SQLite, FPZIP, PNG or FFmpeg operations.
        # Share a lock with the caller's progress writes to avoid corrupting
        # terminal redraws during multi-minute image/video operations.
        import threading
        self._draw_lock = threading.RLock()
        self._anim_stop = threading.Event()
        self._anim_thread = None
        self._anim_enabled = bool(animate and self.active)
        self._anim_frame = 0
        self._last_detail = ''
        if self._anim_enabled:
            self._anim_thread = threading.Thread(
                target=self._animate_progress, daemon=True, name='CT-progress-spinner')
            self._anim_thread.start()

    def _animate_progress(self):
        while not self._anim_stop.wait(0.18):
            with self._draw_lock:
                if self._visible and self._anim_enabled and self.done < self.total:
                    self._anim_frame += 1
                    self._show_locked(self._last_detail)

    def _stop_animation(self):
        thread = self._anim_thread
        if thread is not None:
            self._anim_stop.set()
            thread.join()
            self._anim_thread = None
            self._anim_enabled = False

    @staticmethod
    def _elapsed(seconds):
        count = int(max(0, seconds))
        h, remainder = divmod(count, 3600)
        m, sec = divmod(remainder, 60)
        return f'{h:d}:{m:02d}:{sec:02d}' if h else f'{m:02d}:{sec:02d}'

    def _line(self, detail):
        """Compact human-facing progress; diagnostic counts are reported separately."""
        columns = max(38, _ct_shutil.get_terminal_size(fallback=(100, 24)).columns)
        spinner = ''
        if self._anim_enabled and self.done < self.total:
            frames = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏' if self.unicode else '|/-\\'
            spinner = ' ' + frames[self._anim_frame % len(frames)]
        # Reserve width for the added indicator without removing any existing
        # percent, count, detail, status or completed text.
        available = columns - 1 - len(spinner)
        ratio = self.done / self.total if self.total else 1.0
        percent = int(ratio * 100)
        summary = f' {percent:>3}%'
        if self.done == self.total:
            summary += '  Complete'
        # Count selected PNG histories (still images AND individual video frames).
        # Keep this on the same live terminal line as the percentage.
        summary += f' ({self.done}/{self.total})'
        # Reserve room for a short video/phase status on the SAME updating line.
        status = ''
        if self.show_detail and detail:
            room = max(0, available - 17 - len(summary))
            if room >= 14:
                status = ' | ' + str(detail)[:room - 3]
        max_bar = 34 if columns >= 70 else 24
        # No bracket-shaped endcaps; the bar stands on its own.
        bar_width = max(1, min(max_bar, available - len(summary) - len(status) - 1))
        filled = max(0, min(bar_width, int(ratio * bar_width)))
        if self.unicode:
            if filled == bar_width or filled < 4:
                head = '█' * filled
            else:
                # The filled portion ends in a three-cell fade.
                head = '█' * (filled - 3) + '▓▒░'
            tail = '·' * (bar_width - filled)
        else:
            # Encoding-safe fallback for legacy terminal streams.
            head = '#' * filled
            if 0 < filled < bar_width:
                head = head[:-1] + '>'
            tail = '.' * (bar_width - filled)
        bar = head + tail
        if self.color:
            hue = ('\x1b[38;5;208m' if self.errors else
                   '\x1b[38;5;82m' if self.done == self.total else
                   '\x1b[38;5;45m')
            bar = (hue + head + '\x1b[0m\x1b[2m' + tail + '\x1b[0m')
        line = ' ' + bar + summary + status + spinner
        return line, 1 + bar_width + len(summary) + len(status) + len(spinner)

    def _clear_locked(self):
        if not self.active or not self._visible:
            return
        self.stream.write('\r' + ' ' * self._last_width + '\r')
        self.stream.flush()
        self._visible = False

    def clear(self):
        with self._draw_lock:
            self._clear_locked()

    def _show_locked(self, detail):
        line, visible_width = self._line(detail)
        self.stream.write('\r' + line + ' ' * max(0, self._last_width-visible_width))
        self.stream.flush()
        self._last_width = visible_width
        self._visible = True

    def show(self, detail=''):
        if not self.active:
            return
        with self._draw_lock:
            self._last_detail = detail
            self._show_locked(detail)

    def cancel(self):
        """Stop activity without implying progress reached 100%."""
        self._stop_animation()
        self.clear()

    def message(self, text, *, error=False):
        # Clear the progress display so permanent diagnostics aren't overdrawn.
        self.clear()
        print(text, file=_ct_sys.stderr if error else _ct_sys.stdout)

    def advance(self, kind, detail=''):
        self.done += 1
        if kind == 'saved':
            self.saved += 1
        elif kind == 'reused':
            self.reused += 1
        elif kind == 'error':
            self.errors += 1
        self.show(detail)

    def finish(self):
        self._stop_animation()
        if self.active:
            with self._draw_lock:
                self._show_locked('Complete')
                self.stream.write('\n')
                self.stream.flush()
                self._visible = False



class _CTActivity:
    """Elapsed-time activity indicator, without a fictional percentage.

    A separate display-only thread keeps the terminal responsive when SQLite
    executes a single long-running C statement (such as PRAGMA quick_check).
    The worker never touches the SQLite connection. No non-TTY animation.
    """
    def __init__(self, label, *, enabled=True):
        import threading
        self.label = label
        self.stream = _ct_sys.stdout
        self.enabled = bool(enabled and self.stream.isatty() and
                            _ct_os.environ.get('TERM', '') != 'dumb')
        self.started = _ct_time.monotonic()
        self.event = threading.Event()
        self.thread = None
        self.width = 0
        if self.enabled:
            self.thread = threading.Thread(target=self._animate, daemon=True)
            self.thread.start()
        else:
            print('    ' + label + '...', flush=True)

    def _animate(self):
        chars = '|/-\\' if _ct_os.name == 'nt' else '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
        i = 0
        while not self.event.is_set():
            terminal = _ct_shutil.get_terminal_size(fallback=(100, 24)).columns
            duration = _CTProgress._elapsed(_ct_time.monotonic()-self.started)
            suffix = f' {chars[i % len(chars)]} {duration} elapsed'
            available = max(10, terminal - 2 - len(suffix))
            line = '    ' + self.label[:available] + suffix
            self.stream.write('\r' + line + ' ' * max(0, self.width-len(line)))
            self.stream.flush()
            self.width = len(line)
            i += 1
            self.event.wait(0.18)

    def stop(self):
        if self.thread is not None:
            self.event.set()
            self.thread.join()
            self.stream.write('\r' + ' ' * self.width + '\r')
            self.stream.flush()
            self.thread = None


class _CTPreparation:
    """Phase-specific preparation progress; disables animation for logs/verbose."""
    def __init__(self, *, enabled=True):
        self.enabled = enabled
        self.activity = None
        self.bar = None
        self.phase = ''
        self.count = 0

    def begin(self, name, *, total=None):
        self.finish()
        self.phase = name
        if total is None:
            self.activity = _CTActivity(name, enabled=self.enabled)
        else:
            print('    ' + name + '...', flush=True)
            self.bar = _CTProgress(total, enabled=self.enabled)
            self.bar.show()

    def update(self, value):
        if self.bar is not None:
            self.bar.done = min(self.bar.total, value)
            self.bar.show()

    def finish(self):
        # One blank line between completed preparation phases. Keep the phase
        # label and its live progress bar adjacent; never write spacing while a
        # spinner or bar is still active (which would corrupt TTY redraws).
        had_phase = self.activity is not None or self.bar is not None
        if self.activity is not None:
            elapsed = _CTProgress._elapsed(_ct_time.monotonic()-self.activity.started)
            self.activity.stop()
            if self.activity.enabled:
                print(f'    {self.phase}: complete ({elapsed})', flush=True)
            self.activity = None
        if self.bar is not None:
            self.bar.done = self.bar.total
            self.bar.finish()
            self.bar = None
        if had_phase:
            print(flush=True)

    def stop(self):
        # Emergency cleanup without displaying a fictitious 100%.
        if self.activity:
            self.activity.stop()
            self.activity = None
        if self.bar:
            self.bar.clear()
            self.bar = None


class _CTVideoStatus:
    """One terminal line for clip checking, FFmpeg frames, and verification."""
    def __init__(self, *, enabled=True):
        self.enabled = bool(enabled and _ct_sys.stdout.isatty() and
                            _ct_os.environ.get('TERM', '') != 'dumb')
        self.activity = None
        self.bar = None
        self.description = ''

    def begin(self, cid, ordinal, total, fmt):
        if not self.enabled:
            return
        self.stop()
        self.description = f'Clip {cid} ({ordinal}/{total})'
        self.stage(f'Checking {fmt.upper()}')

    def stage(self, state):
        if not self.enabled:
            return
        if self.bar is not None:
            self.bar.cancel()
            self.bar = None
        if self.activity is not None:
            self.activity.stop()
        self.activity = _CTActivity(f'{self.description} | {state}',enabled=self.enabled)

    def encoding(self, total, fmt):
        if not self.enabled:
            return
        if self.activity is not None:
            self.activity.stop()
            self.activity = None
        self.bar = _CTProgress(total,enabled=self.enabled,show_detail=True,animate=True)
        self.bar.show(f'{self.description} | Encoding {fmt.upper()}')

    @property
    def done(self):
        return self.bar.done if self.bar else 0

    @done.setter
    def done(self, value):
        if self.bar is not None:
            self.bar.done = value

    def show(self, detail=''):
        if self.bar is not None:
            self.bar.show(detail)

    def clear(self):
        if self.bar is not None:
            self.bar.clear()

    def stop(self):
        if self.activity is not None:
            self.activity.stop()
            self.activity = None
        if self.bar is not None:
            self.bar.cancel()
            self.bar = None


def _ct_compact_clips(ids):
    """Compress exact clip numbers to e.g. '6-9, 13, 18-26'."""
    ordered = sorted(set(ids))
    spans=[]
    for cid in ordered:
        if spans and cid == spans[-1][-1]+1:
            spans[-1].append(cid)
        else:
            spans.append([cid])
    return ', '.join(str(x[0]) if len(x)==1 else f'{x[0]}-{x[-1]}' for x in spans)


def _ct_diagnostic_report(video_warnings, history_errors, export_errors, *, verbose=False):
    """Consolidate nonfatal diagnostics while retaining exact details in -v."""
    from collections import defaultdict
    counts=defaultdict(list)
    remaining=[]
    for warning in video_warnings:
        match=_ct_re.fullmatch(
            r'clip (\d+): stored clip length=(\d+); historical generation '
            r'numFrames=(\d+) \((\d+) frame histories\); '
            r'using stored clip count and original frame indexes for export',warning)
        if match:
            cid,stored,requested,histories=map(int, match.groups())
            counts[(stored,requested,histories)].append(cid)
        else:
            remaining.append(warning)
    if not (counts or remaining or history_errors or export_errors):
        return
    print('\nDiagnostics:')
    if counts:
        import textwrap
        print(f'    Video frame-count discrepancies: {sum(map(len,counts.values()))} clip(s)')
        columns=max(50,_ct_shutil.get_terminal_size(fallback=(100,24)).columns)
        for (stored,requested,histories), ids in sorted(counts.items()):
            message=(f'      {stored} stored / {requested} requested: '
                     f'{len(ids)} clip(s) ({_ct_compact_clips(ids)})')
            print(textwrap.fill(message,width=columns-1,
                                subsequent_indent='          ',break_long_words=False))
        print('      Action: Using stored frame counts and original frame indexes.')
    for item in remaining:
        print('    WARNING: ' + item)
    if history_errors:
        print(f'    ERROR: {len(history_errors)} invalid generation history record(s)')
        if verbose:
            for lin,logical,reason in history_errors:
                print(f'      lineage={lin} logical_time={logical}: {reason}')
    if export_errors:
        print(f'    ERROR: {len(export_errors)} export failure(s)')
        ambiguous=[]
        other=[]
        for label,problem in export_errors:
            matched=_ct_re.fullmatch(
                r'ambiguous image tensor references for lineage=(\d+) logical_time=(\d+)',
                problem)
            if matched:
                ambiguous.append((int(matched[1]),int(matched[2]),label))
            else:
                other.append((label,problem))
        if ambiguous:
            print(f'      Ambiguous image tensor references: {len(ambiguous)}')
            print('        Affected lineages: ' + _ct_compact_clips(lin for lin,_,_ in ambiguous))
            if verbose:
                for lin,logical,label in ambiguous:
                    print(f'        {label}: lineage={lin} logical_time={logical}')
            else:
                print('        Individual lineage/times: use --verbose')
        # Show non-ambiguity failures even in normal mode; cap lengthy lists.
        for label,problem in other[:20 if not verbose else len(other)]:
            print(f'      {label}: {problem}')
        if not verbose and len(other)>20:
            print(f'      ... {len(other)-20} more; use --verbose for details')
    if verbose:
        for warning in video_warnings:
            print('    DETAIL: ' + warning)


from collections import Counter

# --- Architecture-aware still-image workflow metadata ---
# Exact records from Draw Things developer-managed ModelZoo.swift and
# drawthingsai/community-models definitions (verified 2026-10-18).
# No external model or LoRA registry files are required.
# This is deliberately a *small* authoritative embedded catalog, not a
# guessing engine. Entries refer to DT file identifiers, NOT ComfyUI filenames.
DT_BUILTIN_EXACT = {}

def _ct_builtin_model(filename, family, text_encoder=None, autoencoder=None, clip_encoder=None):
    record = {'file': filename, 'version': family,
              '_origin': 'Draw Things developer-published exact model specification'}
    if text_encoder: record['text_encoder'] = text_encoder
    if autoencoder: record['autoencoder'] = autoencoder
    if clip_encoder: record['clip_encoder'] = clip_encoder
    if filename.casefold() in DT_BUILTIN_EXACT:
        raise ValueError('duplicate built-in model identifier: ' + filename)
    DT_BUILTIN_EXACT[filename.casefold()] = record

for _dt_suffix in ('q8p', 'i8x', 'q6p', 'bf16_q8p', 'bf16_i8x', 'bf16_q6p'):
    _ct_builtin_model('qwen_image_2512_' + _dt_suffix + '.ckpt', 'qwen_image',
                      'qwen_2.5_vl_7b_q8p.ckpt', 'qwen_image_vae_f16.ckpt')
for _dt_name in ('z_image_turbo_1.0_q8p.ckpt', 'z_image_turbo_1.0_i8x.ckpt',
                 'z_image_turbo_1.0_q6p.ckpt', 'z_image_1.0_q8p.ckpt',
                 'z_image_1.0_i8x.ckpt', 'z_image_1.0_q6p.ckpt'):
    _ct_builtin_model(_dt_name, 'z_image', 'qwen_3_vl_4b_instruct_q8p.ckpt',
                      'flux_1_vae_f16.ckpt')
for _dt_name in ('flux_1_dev_q8p.ckpt', 'flux_1_dev_q5p.ckpt'):
    _ct_builtin_model(_dt_name, 'flux1', 't5_xxl_encoder_q6p.ckpt',
                      'flux_1_vae_f16.ckpt', 'clip_vit_l14_f16.ckpt')
for _dt_name in ('flux_2_klein_4b_q8p.ckpt', 'flux_2_klein_4b_i8x.ckpt',
                 'flux_2_klein_4b_q6p.ckpt'):
    # FLUX.2 Klein 4B has its own Qwen3/flux2 text encoder and 128-channel latent.
    _ct_builtin_model(_dt_name, 'flux2_4b', 'qwen_3_4b_q8p.ckpt',
                      'flux_2_vae_f16.ckpt')
del _dt_suffix, _dt_name


# --- Embedded Draw Things model catalog ---
# Build-time tool populates the source rows below from official Swift, curated,
# and uncurated DT registries. This *development snapshot* is intentionally
# incomplete until the build tool has run successfully against all three feeds.
_CT_DT_CATALOG_ROWS_JSON = r'''[{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1001176_jibmixflux_fp8v4canvasgalore_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1002647_pixelwave_flux1schnell03_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1004235_artsydream_v2fp8_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1004320_artsydream_v2fp16_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1006775_iniversemixsfwnsfw_f1drealguofengfp8v20_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1023471_artsydream_v3fp8_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1023964_artsydream_v3fp16_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1024165_ratatoskranimalcreature_v594fp8dev1hybrid_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1031531_iniversemixsfwnsfw_f1drealnsfwguofengv2_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1035832_c4pacitor_av2beta_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1045901_copaxtimeless_fluxrealfast_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1048219_demoncoresfwnsfw_fluxv13_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1050076_fluxmania_i_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1052470_acornisspinningflux_aisf11h8stpchinfx_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1054490_flux1_v10fp8_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1055211_agflux_v21fp8_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1068773_genovaapex_s_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1070930_artsydream_v4fp8_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1071391_artsydream_v4fp16_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1085190_copaxtimeless_xplus2basetrain_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1086868_c4pacitor_sv2alpha_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1087807_redcraftcadsupdatedjun1_revealnsfw_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1093373_redcraftcadsupdatedjun1_rasch1forge_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1095703_ultrarealfinetune_v10_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1095910_agfluxfillnsfwfp8_agfluxfillnsfwv17fp8_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1103485_flux1_v10fp8schnell_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1108227_illustrationjuanerghibli_v10_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1111281_genovaapex_real_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1115605_redcraftcadsupdatedjun1_rasch2_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1123235_c4pacitor_dv2_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1126942_fluxedupfluxnsfw_21_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1134093_copaxtimeless_xplusmix3_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1136364_artsydream_v5fp8_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1136806_artsydream_v5fp16_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1138590_redbluefantasy_fluxv10_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1148473_copaxtimeless_xplusmix3dark_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1159932_colossusprojectflux_v42aio_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1164498_ultrarealfinetune_v20_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1171942_redcraftcadsupdatedjun1_revealultrav35_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1178183_centerfoldflux_v35fp8e54steps_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1181877_project0_v10fp8unetonly_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1182699_fluxedupfluxnsfw_22_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1186008_rayflux_v10_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1187144_8stepscrearthyperflux_v30hyperdevfp8unet_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1193229_jibmixflux_v7pixelheavenbeta_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1201074_redcraftcadsupdatedjun1_turborevealhotfix21_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1209969_genovaapex_plus_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1213097_artsydream_v6fp8_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1213649_artsydream_v6fp16_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1215918_illustrationjuanerghibli_v20_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1217501_copaxtimeless_xplusmix4_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1219311_flux1_v20fp8_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1224657_edgeofreality_v30_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1244250_agflux_v3fp8_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1247362_majicflus_v10_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1249092_project0_v20fp16_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1249359_moxiefusionflux_v141s_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1249496_project0_v20fp8_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1249737_jibmixflux_v72pixelheaven_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1254943_hyperfluxdedistilled_hyper16stepsfp8_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1255083_hyperfluxdedistilled_hyper8stepsfp8_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1255371_moxiefusionflux_v141d_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1288366_nepotism_xdit_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1292596_moxiefusionflux_v15s_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1298942_acornisspinningflux_aisfhyper8stepv15_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1305680_fluxedupfluxnsfw_24_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1308101_originbyn0utis_fluxfp8v1_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1308180_artsyvibe_v1fp8_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1308807_artsyvibe_v1fp16_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1315145_8stepscrearthyperflux_v40hyperdevfp8unet_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1315432_redcraftcadsupdatedjun1_newreveal_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1319871_flux1_v30fp8_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1320268_project0_v30artfp8_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1326315_nepotism_xidit_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1329004_genovaapex_1618experiment_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1338565_copaxtimeless_xpluscartoon_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1344905_redcraftcadsupdatedjun1_rasch3rushreveal_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1355240_jibmixflux_v78cleartextfocus_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1357212_demoncoresfwnsfw_v25helheimprojectaio_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1374658_acornisspinningflux_aisfluxv15_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1389019_jibmixflux_v8accentueightnow_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1413133_ultrarealfinetune_v4_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1442202_flux1_v30papfp8_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1443922_colossusprojectflux_v50aio_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1445047_redcraftcadsupdatedjun1_f1fillnsfw_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1450688_acornisspinningflux_aisfdedistilledv15_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1451754_project0_real1smfp16_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1453489_moxieflux1ds_v16d_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1454015_ultrarealfinetune_v1_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1462430_colossusprojectflux_v50fp8unet_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1463804_fluxmania_iv_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1466679_fluxedupfluxnsfw_25_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1473409_moxiefusionflux_v16s_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1495910_colossusprojectflux_v44aio_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1505959_msfluxsfwnsfwv3_v3_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1507835_colossusprojectflux_v50aiobehemoth_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1519030_flux1eastfp8_dev_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1519446_flux1eastfp8_fill_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1519790_flux1eastfp8_canny_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1519801_flux1eastfp8_depth_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1539776_fluxmania_v_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1550408_redcraftcadsupdatedjun1_realreveal5_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1576605_redcraftcadsupdatedjun1_reveal5sfwultra_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1591370_project0_real1smv2fp16_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1596255_project0_real1smv2fp8_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1637227_ratatoskranimalcreature_v667fp16_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1637484_ratatoskranimalcreature_v667fp8_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1656743_acornisspinningflux_aisfv169_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1677057_edgeofreality_32beyondreality_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1693152_8stepscrearthyperflux_creartultimate_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1700674_c4pacitor_dv3alpha_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1700814_fluxedupfluxnsfw_40devfp8_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1703060_colossusprojectflux_v90aio_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1703341_realdream_flux1v1_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1718411_moxiefusionflux_17d_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1742386_moxiefusionflux_v17s_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1745151_redcraftcadsupdatedjun1_redediticedit11_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1755367_jibmixflux_v85consisteight_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1769925_fluxmania_legacy_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1785042_rayflux_v30aio_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1802008_moxieflux1ds_v17s_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1823302_fluxedupfluxnsfw_41_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1831456_genovaapex_genovav2_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1840540_artsyvibe_v2fp8_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1842885_artsyvibe_v2fp16_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1845222_centerfoldflux_v40fp8e5m2_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1846322_colossusprojectflux_v10aiofp8_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1848909_colossusprojectflux_v10unetonlyfp8_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1849802_redcraftcadsupdatedjun1_redomniflux1dreamo_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1851709_colossusprojectflux_v10behemothaiofp16_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1855312_colossusprojectflux_v10unetonlyfp16_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1860608_colossusprojectflux_v10bfp8aiobobfp8_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1868950_gonzalomoxlfluxpony_v03fluxityfp8_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1875661_project0_real1smv3fp16_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1878051_project0_real1smv3fp8aio_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"1883195_gonzalomoxlfluxpony_v10fluxityfp8_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"2dn_clip_vit_l14_f16.ckpt","file":"2dn_f16.ckpt","text_encoder":"2dn_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"742904_thearamintaexperiment_flux1a1_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"752959_flux1devasian_v10fp16_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"765642_colorfulasiangirlflux_beta_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"778112_copaxtimeless_xplus1_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"783956_copaxtimeless_xplusunrealfast_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"791130_xehentaianimeflux_03_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"793143_ratatoskranimalcreature_v29_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"795206_fenrisxlflux_fenrisfluxv1fp8_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"796103_jibmixflux_fp16v1_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"801556_copaxtimeless_xplusposes_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"806846_projectgaiaflux1d_v10unet_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"807911_xehentaianimeflux_04_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"808159_bluepencilflux1_v010_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"826571_pixelwave_flux1dev02_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"835047_ratatoskranimalcreature_v40fp8_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"835744_lyhanimeflux_delete_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"845592_c4pacitor_dv1_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"862533_whiteflux1_v10_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"862930_8stepscrearthyperflux_v24hyperdevfp8unet_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"864441_agflux_v10fp8_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"866874_myhumanflux_12train_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"867256_bluepencilflux1_v021_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"869391_stoiqonewrealityfluxsd35_f1dalpha_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"872820_lahmysteriousflux_fluxalpha_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"873136_ratatoskranimalcreature_v41_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"873546_moxiefusionflux_v1328step_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"874291_moxiefusionflux_v132_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"876813_moxiefusionflux_v1318step_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"878315_sdvn11ghibliflux_fp8hyper_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"881836_realflux10b_10bcompactschnell_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"882554_realflux10b_10btransformer_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"883958_moxieflux1ds_v138step_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"884843_agflux_schnellv10fp8_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"885693_myhumanflux_myh11_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"886474_isenganmixflux_vb_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"894590_copaxtimeless_stylemix1fluxdevfp16_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"897489_stoiqoafroditefluxxl_f1dalpha_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"897535_copaxtimeless_stylemix1fp8_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"905329_moxieflux1ds_flux1d8step_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"905879_moxieflux1ds_flux1s8step_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"907183_copaxtimeless_xplus2_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"913024_moxiefusionflux_v1338step_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"915279_realflux10b_10btransformerdev_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"918242_jibmixflux_fp8v2electric_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"930403_8stepscrearthyperflux_v26hyperdevfp8unet_f16.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"930403_8stepscrearthyperflux_v26hyperdevfp8unet_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"931988_colossusprojectflux_v10allinonefp8_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"932231_moxiefusionflux_v1348step_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"940696_colossusprojectflux_v10fp16unetonly_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"943051_moxiefusionflux_v134_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"947686_fluxedupfluxnsfw_10_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"949363_c4pacitor_dv11_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"951641_xueronecyantencolor_fluxv10_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"954181_c4pacitor_sv11_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"954640_copaxtimeless_xplusmix1_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"959302_fluxrealistic_fluxrealisticsamayv2_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"962412_copaxtimeless_xplusmix1fp8_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"966095_iniversemixsfwnsfw_flux1dnsfwfp8v10_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"971579_colossusprojectflux_v20allinoneexp_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"973626_iniversemixsfwnsfw_flux1dnsfwfp16v12_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"974825_artsydream_v1fp16_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"974932_artsydream_v1fp8_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"975743_copaxtimeless_stylemix2_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"978066_c4pacitor_av2alpha_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"979329_stoiqonewrealityfluxsd35_f1dalphatwo_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"980131_atomixfluxunet_v10_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"982160_copaxtimeless_xplusmix2_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"985269_moxiefusionflux_v135_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"986682_moxieflux1ds_v14_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"987485_acornisspinningflux_aisfluxdedistilled_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"988886_verusvision10b_10btransformer_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"989443_myhumanflux_myh13_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"991687_myhumanflux_myh13fp8_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"992642_pixelwave_flux1dev03_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"996001_colossusprojectflux_21dedistilledaioexp_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"agga_s_utopia__illustrious_sdxl_clip_vit_l14_f16.ckpt","file":"agga_s_utopia__illustrious_sdxl_f16.ckpt","text_encoder":"agga_s_utopia__illustrious_sdxl_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","file":"aloeveras_simpmaker_3k1_f16.ckpt","version":"v1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"amanatsu_illustrious_clip_vit_l14_f16.ckpt","file":"amanatsu_illustrious_f16.ckpt","text_encoder":"amanatsu_illustrious_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","file":"analog_v1_f16.ckpt","text_encoder":"analog_v1_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"anillustrious_clip_vit_l14_f16.ckpt","file":"anillustrious_f16.ckpt","text_encoder":"anillustrious_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"qwen_image_vae_f16.ckpt","clip_encoder":"anima_aesthetic_1.1_f16.ckpt","file":"anima_aesthetic_1.1_f16.ckpt","text_encoder":"qwen_3_0.6b_q8p.ckpt","version":"cosmos2_5_2b"},{"_origin":"Draw Things curated models.json","autoencoder":"qwen_image_vae_f16.ckpt","clip_encoder":"anima_aesthetic_1.1_i8x.ckpt","file":"anima_aesthetic_1.1_i8x.ckpt","text_encoder":"qwen_3_0.6b_q8p.ckpt","version":"cosmos2_5_2b"},{"_origin":"Draw Things curated models.json","autoencoder":"qwen_image_vae_f16.ckpt","clip_encoder":"anima_aesthetic_1.1_q8p.ckpt","file":"anima_aesthetic_1.1_q8p.ckpt","text_encoder":"qwen_3_0.6b_q8p.ckpt","version":"cosmos2_5_2b"},{"_origin":"Draw Things curated models.json","autoencoder":"qwen_image_vae_f16.ckpt","clip_encoder":"anima_base_1.0_f16.ckpt","file":"anima_base_1.0_f16.ckpt","text_encoder":"qwen_3_0.6b_q8p.ckpt","version":"cosmos2_5_2b"},{"_origin":"Draw Things curated models.json","autoencoder":"qwen_image_vae_f16.ckpt","clip_encoder":"anima_base_1.0_i8x.ckpt","file":"anima_base_1.0_i8x.ckpt","text_encoder":"qwen_3_0.6b_q8p.ckpt","version":"cosmos2_5_2b"},{"_origin":"Draw Things curated models.json","autoencoder":"qwen_image_vae_f16.ckpt","clip_encoder":"anima_base_1.0_q8p.ckpt","file":"anima_base_1.0_q8p.ckpt","text_encoder":"qwen_3_0.6b_q8p.ckpt","version":"cosmos2_5_2b"},{"_origin":"Draw Things curated models.json","autoencoder":"qwen_image_vae_f16.ckpt","clip_encoder":"anima_turbo_1.0_f16.ckpt","file":"anima_turbo_1.0_f16.ckpt","text_encoder":"qwen_3_0.6b_q8p.ckpt","version":"cosmos2_5_2b"},{"_origin":"Draw Things curated models.json","autoencoder":"qwen_image_vae_f16.ckpt","clip_encoder":"anima_turbo_1.0_i8x.ckpt","file":"anima_turbo_1.0_i8x.ckpt","text_encoder":"qwen_3_0.6b_q8p.ckpt","version":"cosmos2_5_2b"},{"_origin":"Draw Things curated models.json","autoencoder":"qwen_image_vae_f16.ckpt","clip_encoder":"anima_turbo_1.0_q8p.ckpt","file":"anima_turbo_1.0_q8p.ckpt","text_encoder":"qwen_3_0.6b_q8p.ckpt","version":"cosmos2_5_2b"},{"_origin":"Draw Things curated models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"animagine_xl_v3.1_clip_vit_l14_f16.ckpt","file":"animagine_xl_v3.1_f16.ckpt","text_encoder":"animagine_xl_v3.1_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"animagine_xl_v3.1_clip_vit_l14_f16.ckpt","file":"animagine_xl_v3.1_q6p_q8p.ckpt","text_encoder":"animagine_xl_v3.1_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","clip_encoder":"open_clip_vit_h14_visual_proj_f16.ckpt","file":"animatelcm_svd_xt_v1.1_f16.ckpt","text_encoder":"open_clip_vit_h14_vision_model_f16.ckpt","version":"svd_i2v"},{"_origin":"Draw Things curated models.json","clip_encoder":"open_clip_vit_h14_visual_proj_f16.ckpt","file":"animatelcm_svd_xt_v1.1_q6p_q8p.ckpt","text_encoder":"open_clip_vit_h14_vision_model_f16.ckpt","version":"svd_i2v"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"animij_clip_vit_l14_f16.ckpt","file":"animij_f16.ckpt","text_encoder":"animij_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"wan_v2.1_video_vae_f16.ckpt","clip_encoder":"open_clip_xlm_roberta_large_vit_h14_f16.ckpt","file":"anisora_v2_i2v_wan_2.1_14b_q6p_svd.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan_v2_1_14b"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"wan_v2.1_video_vae_f16.ckpt","clip_encoder":"open_clip_xlm_roberta_large_vit_h14_f16.ckpt","file":"anisora_v2_i2v_wan_2.1_14b_q8p.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan_v2_1_14b"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"wan_v2.1_video_vae_f16.ckpt","clip_encoder":"open_clip_xlm_roberta_large_vit_h14_f16.ckpt","file":"anisora_v3.1_i2v_wan_2.1_14b_q6p_svd.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan_v2_1_14b"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"wan_v2.1_video_vae_f16.ckpt","clip_encoder":"open_clip_xlm_roberta_large_vit_h14_f16.ckpt","file":"anisora_v3.1_i2v_wan_2.1_14b_q8p.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan_v2_1_14b"},{"_origin":"Draw Things curated models.json","autoencoder":"wan_v2.1_video_vae_f16.ckpt","file":"anisora_v3.2_i2v_wan_2.2_a14b_hne_q6p_svd.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan_v2_1_14b"},{"_origin":"Draw Things curated models.json","autoencoder":"wan_v2.1_video_vae_f16.ckpt","file":"anisora_v3.2_i2v_wan_2.2_a14b_hne_q8p.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan_v2_1_14b"},{"_origin":"Draw Things curated models.json","autoencoder":"wan_v2.1_video_vae_f16.ckpt","file":"anisora_v3.2_i2v_wan_2.2_a14b_lne_q6p_svd.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan_v2_1_14b"},{"_origin":"Draw Things curated models.json","autoencoder":"wan_v2.1_video_vae_f16.ckpt","file":"anisora_v3.2_i2v_wan_2.2_a14b_lne_q8p.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan_v2_1_14b"},{"_origin":"Draw Things curated models.json","autoencoder":"anything_v3_vae_f16.ckpt","file":"anything_v3_f16.ckpt","text_encoder":"anything_v3_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things curated models.json","file":"arcane_v3_f16.ckpt","text_encoder":"arcane_v3_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"artofura_furry_mix_clip_vit_l14_f16.ckpt","file":"artofura_furry_mix_f16.ckpt","text_encoder":"artofura_furry_mix_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"artsy_vibe_v1_q5p_svd.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"artsy_vibe_v1_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"asian_realism_by_stable_yogi_pony_clip_vit_l14_f16.ckpt","file":"asian_realism_by_stable_yogi_pony_f16.ckpt","text_encoder":"asian_realism_by_stable_yogi_pony_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"aungir_aungir_t6a4_1405004_clip_vit_l14_f16.ckpt","file":"aungir_aungir_t6a4_1405004_f16.ckpt","text_encoder":"aungir_aungir_t6a4_1405004_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"aungir_aungir_t6ao4.5_1723632_clip_vit_l14_f16.ckpt","file":"aungir_aungir_t6ao4.5_1723632_f16.ckpt","text_encoder":"aungir_aungir_t6ao4.5_1723632_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"sdxl_vae_v1.0_f16.ckpt","file":"auraflow_v0.1_q5p.ckpt","text_encoder":"pile_t5_xl_encoder_q8p.ckpt","version":"auraflow"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"sdxl_vae_v1.0_f16.ckpt","file":"auraflow_v0.1_q8p.ckpt","text_encoder":"pile_t5_xl_encoder_q8p.ckpt","version":"auraflow"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"sdxl_vae_v1.0_f16.ckpt","file":"auraflow_v0.2_q5p.ckpt","text_encoder":"pile_t5_xl_encoder_q8p.ckpt","version":"auraflow"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"sdxl_vae_v1.0_f16.ckpt","file":"auraflow_v0.2_q8p.ckpt","text_encoder":"pile_t5_xl_encoder_q8p.ckpt","version":"auraflow"},{"_origin":"Draw Things curated models.json","file":"balloonart_v1_f16.ckpt","text_encoder":"balloonart_v1_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"big_love_clip_vit_l14_f16.ckpt","file":"big_love_f16.ckpt","text_encoder":"big_love_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"bismuth_illustrious_mix_clip_vit_l14_f16.ckpt","file":"bismuth_illustrious_mix_f16.ckpt","text_encoder":"bismuth_illustrious_mix_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"blendermix_clip_vit_l14_f16.ckpt","file":"blendermix_f16.ckpt","text_encoder":"blendermix_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"boleromixillustrious_clip_vit_l14_f16.ckpt","file":"boleromixillustrious_f16.ckpt","text_encoder":"boleromixillustrious_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"candied_anime_illustriousxl_clip_vit_l14_f16.ckpt","file":"candied_anime_illustriousxl_f16.ckpt","text_encoder":"candied_anime_illustriousxl_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"chroma_1_hd_annealed_q5p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"chroma_1_hd_annealed_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"chroma_1_hd_r0.1_q5p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"chroma_1_hd_r0.1_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"chroma_v38_detail_calibrated_q5p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"chroma_v38_detail_calibrated_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"chroma_v39_detail_calibrated_q5p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"chroma_v39_detail_calibrated_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"chroma_v40_detail_calibrated_q5p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"chroma_v40_detail_calibrated_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"chroma_v41_detail_calibrated_q5p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"chroma_v41_detail_calibrated_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"chroma_v42_detail_calibrated_q5p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"chroma_v42_detail_calibrated_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"chroma_v43_detail_calibrated_q5p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"chroma_v43_detail_calibrated_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"chroma_v44_detail_calibrated_q5p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"chroma_v44_detail_calibrated_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"chroma_v45_detail_calibrated_q5p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"chroma_v45_detail_calibrated_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"chroma_v46_detail_calibrated_q5p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"chroma_v46_detail_calibrated_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"chroma_v47_detail_calibrated_q5p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"chroma_v47_detail_calibrated_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"chroma_v48_detail_calibrated_q5p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"chroma_v48_detail_calibrated_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things curated models.json","autoencoder":"wan_v2.1_video_vae_f16.ckpt","clip_encoder":"open_clip_xlm_roberta_large_vit_h14_f16.ckpt","file":"chronoedit_14b_q6p_svd.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan_v2_1_14b"},{"_origin":"Draw Things curated models.json","autoencoder":"wan_v2.1_video_vae_f16.ckpt","clip_encoder":"open_clip_xlm_roberta_large_vit_h14_f16.ckpt","file":"chronoedit_14b_q8p.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan_v2_1_14b"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"circusmix_clip_vit_l14_f16.ckpt","file":"circusmix_f16.ckpt","text_encoder":"circusmix_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","file":"classicanim_v1_f16.ckpt","text_encoder":"classicanim_v1_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things curated models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"colorfulxl_v6.0_clip_vit_l14_f16.ckpt","file":"colorfulxl_v6.0_f16.ckpt","text_encoder":"colorfulxl_v6.0_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"colorfulxl_v6.0_clip_vit_l14_f16.ckpt","file":"colorfulxl_v6.0_q6p_q8p.ckpt","text_encoder":"colorfulxl_v6.0_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"copax_pony_xl_clip_vit_l14_f16.ckpt","file":"copax_pony_xl_f16.ckpt","text_encoder":"copax_pony_xl_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","file":"counterfeit_v3.0_f16.ckpt","text_encoder":"counterfeit_v3.0_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things curated models.json","file":"counterfeit_v3.0_q6p_q8p.ckpt","text_encoder":"counterfeit_v3.0_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"crow___pony_qp_clip_vit_l14_f16.ckpt","file":"crow___pony_qp_f16.ckpt","text_encoder":"crow___pony_qp_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"cyberillustrious_cyberrealistic_clip_vit_l14_f16.ckpt","file":"cyberillustrious_cyberrealistic_f16.ckpt","text_encoder":"cyberillustrious_cyberrealistic_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","file":"cyberpunk_anime_f16.ckpt","text_encoder":"cyberpunk_anime_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things official community uncurated_models.json","file":"cyberrealistic_f16.ckpt","text_encoder":"cyberrealistic_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"cyberrealistic_flux_v2.5_f16.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"cyberrealistic_flux_v2.5_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"cyberrealistic_pony_catalyst_clip_vit_l14_f16.ckpt","file":"cyberrealistic_pony_catalyst_f16.ckpt","text_encoder":"cyberrealistic_pony_catalyst_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"cyberrealistic_pony_semi_realistic_clip_vit_l14_f16.ckpt","file":"cyberrealistic_pony_semi_realistic_f16.ckpt","text_encoder":"cyberrealistic_pony_semi_realistic_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"cyberrealistic_pony_v8.5_1478064_clip_vit_l14_f16.ckpt","file":"cyberrealistic_pony_v8.5_1478064_f16.ckpt","text_encoder":"cyberrealistic_pony_v8.5_1478064_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"cyberrealistic_pony_v8_1346181_clip_vit_l14_f16.ckpt","file":"cyberrealistic_pony_v8_1346181_f16.ckpt","text_encoder":"cyberrealistic_pony_v8_1346181_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"cyberrealistic_pony_v9.0_alt_1_1691220_clip_vit_l14_f16.ckpt","file":"cyberrealistic_pony_v9.0_alt_1_1691220_f16.ckpt","text_encoder":"cyberrealistic_pony_v9.0_alt_1_1691220_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"cyberrealistic_xl_clip_vit_l14_f16.ckpt","file":"cyberrealistic_xl_f16.ckpt","text_encoder":"cyberrealistic_xl_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","file":"deliberate_v2_q6p_q8p.ckpt","text_encoder":"deliberate_v2_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"devmode_v0.3_turbo_q5p_svd.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"devmode_v0.3_turbo_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"dhxl_v1.0_clip_vit_l14_f16.ckpt","file":"dhxl_v1.0_f16.ckpt","text_encoder":"dhxl_v1.0_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","file":"disney_pixar_cartoon_type_b_q6p_q8p.ckpt","text_encoder":"disney_pixar_cartoon_type_b_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"dixar_clip_vit_l14_f16.ckpt","file":"dixar_f16.ckpt","text_encoder":"dixar_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","file":"dnd_30000_f16.ckpt","text_encoder":"dnd_30000_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things curated models.json","file":"dnd_classes_and_species_f16.ckpt","text_encoder":"dnd_classes_and_species_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"dreamisoa_remix_clip_vit_l14_f16.ckpt","file":"dreamisoa_remix_f16.ckpt","text_encoder":"dreamisoa_remix_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","file":"dreamshaper_v8_f16.ckpt","text_encoder":"dreamshaper_v8_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things curated models.json","file":"dreamshaper_v8_q6p_q8p.ckpt","text_encoder":"dreamshaper_v8_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things curated models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"dreamshaper_xl_v2.1_turbo_clip_vit_l14_f16.ckpt","file":"dreamshaper_xl_v2.1_turbo_f16.ckpt","text_encoder":"dreamshaper_xl_v2.1_turbo_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"dreamshaper_xl_v2.1_turbo_clip_vit_l14_f16.ckpt","file":"dreamshaper_xl_v2.1_turbo_q6p_q8p.ckpt","text_encoder":"dreamshaper_xl_v2.1_turbo_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"duchaiten_cool_age_clip_vit_l14_f16.ckpt","file":"duchaiten_cool_age_f16.ckpt","text_encoder":"duchaiten_cool_age_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"duchaiten_noobai_cinematic_clip_vit_l14_f16.ckpt","file":"duchaiten_noobai_cinematic_f16.ckpt","text_encoder":"duchaiten_noobai_cinematic_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"duchaiten_noobai_clip_vit_l14_f16.ckpt","file":"duchaiten_noobai_f16.ckpt","text_encoder":"duchaiten_noobai_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"duchaiten_pony_real_clip_vit_l14_f16.ckpt","file":"duchaiten_pony_real_f16.ckpt","text_encoder":"duchaiten_pony_real_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"duchaiten_pony_xl_no_score_v5.2_706363_clip_vit_l14_f16.ckpt","file":"duchaiten_pony_xl_no_score_v5.2_706363_f16.ckpt","text_encoder":"duchaiten_pony_xl_no_score_v5.2_706363_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"duchaiten_pony_xl_no_score_v6.0_817261_clip_vit_l14_f16.ckpt","file":"duchaiten_pony_xl_no_score_v6.0_817261_f16.ckpt","text_encoder":"duchaiten_pony_xl_no_score_v6.0_817261_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"editijon_k_pop_clip_vit_l14_f16.ckpt","file":"editijon_k_pop_f16.ckpt","text_encoder":"editijon_k_pop_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","file":"eldenring_v3_f16.ckpt","text_encoder":"eldenring_v3_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"epicrealism_xl_clip_vit_l14_f16.ckpt","file":"epicrealism_xl_f16.ckpt","text_encoder":"epicrealism_xl_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"eri_lumenveil_v1_illustrious_nai_clip_vit_l14_f16.ckpt","file":"eri_lumenveil_v1_illustrious_nai_f16.ckpt","text_encoder":"eri_lumenveil_v1_illustrious_nai_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_2_vae_f16.ckpt","file":"ernie_image_f16.ckpt","text_encoder":"ministral_3_3b_f16.ckpt","version":"ernie_image"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"flux_2_vae_f16.ckpt","file":"ernie_image_i8x.ckpt","text_encoder":"ministral_3_3b_q8p.ckpt","version":"ernieimage"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"flux_2_vae_f16.ckpt","file":"ernie_image_q6p.ckpt","text_encoder":"ministral_3_3b_q8p.ckpt","version":"ernieimage"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"flux_2_vae_f16.ckpt","file":"ernie_image_q8p.ckpt","text_encoder":"ministral_3_3b_q8p.ckpt","version":"ernieimage"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_2_vae_f16.ckpt","file":"ernie_image_turbo_f16.ckpt","text_encoder":"ministral_3_3b_f16.ckpt","version":"ernie_image"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"flux_2_vae_f16.ckpt","file":"ernie_image_turbo_i8x.ckpt","text_encoder":"ministral_3_3b_q8p.ckpt","version":"ernieimage"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"flux_2_vae_f16.ckpt","file":"ernie_image_turbo_q6p.ckpt","text_encoder":"ministral_3_3b_q8p.ckpt","version":"ernieimage"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"flux_2_vae_f16.ckpt","file":"ernie_image_turbo_q8p.ckpt","text_encoder":"ministral_3_3b_q8p.ckpt","version":"ernieimage"},{"_origin":"Draw Things curated models.json","file":"f222_f16.ckpt","version":"v1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"fantasm_pony_xl_clip_vit_l14_f16.ckpt","file":"fantasm_pony_xl_f16.ckpt","text_encoder":"fantasm_pony_xl_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"five_stars_illustrious_1.0_1398525_clip_vit_l14_f16.ckpt","file":"five_stars_illustrious_1.0_1398525_f16.ckpt","text_encoder":"five_stars_illustrious_1.0_1398525_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"five_stars_illustrious_4.0_1657020_clip_vit_l14_f16.ckpt","file":"five_stars_illustrious_4.0_1657020_f16.ckpt","text_encoder":"five_stars_illustrious_4.0_1657020_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"flux_1_canny_dev_q5p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"flux_1_canny_dev_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"flux_1_depth_dev_q5p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"flux_1_depth_dev_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"flux_1_dev_de_distill_q5p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"flux_1_dev_de_distill_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"flux_1_dev_f16.ckpt","text_encoder":"t5_xxl_encoder_f16.ckpt","version":"flux1"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"flux_1_dev_q5p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"flux_1_dev_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"flux_1_fill_dev_q5p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"flux_1_fill_dev_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"flux_1_kontext_dev_f16.ckpt","text_encoder":"t5_xxl_encoder_f16.ckpt","version":"flux1"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"flux_1_kontext_dev_q5p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"flux_1_kontext_dev_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"flux_1_krea_dev_q5p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"flux_1_krea_dev_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"flux_1_schnell_q5p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"flux_1_schnell_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_2_vae_f16.ckpt","file":"flux_2_dev_f16.ckpt","text_encoder":"mistral_small_3.2_24b_instruct_2506_f16.ckpt","version":"flux2"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_2_vae_f16.ckpt","file":"flux_2_dev_i8x.ckpt","text_encoder":"mistral_small_3.2_24b_instruct_2506_q8p.ckpt","version":"flux2"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_2_vae_f16.ckpt","file":"flux_2_dev_q6p.ckpt","text_encoder":"mistral_small_3.2_24b_instruct_2506_q8p.ckpt","version":"flux2"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_2_vae_f16.ckpt","file":"flux_2_dev_q8p.ckpt","text_encoder":"mistral_small_3.2_24b_instruct_2506_q8p.ckpt","version":"flux2"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_2_vae_f16.ckpt","file":"flux_2_klein_4b_f16.ckpt","text_encoder":"qwen_3_4b_f16.ckpt","version":"flux2_4b"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"flux_2_vae_f16.ckpt","file":"flux_2_klein_4b_i8x.ckpt","text_encoder":"qwen_3_4b_q8p.ckpt","version":"flux2_4b"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"flux_2_vae_f16.ckpt","file":"flux_2_klein_4b_q6p.ckpt","text_encoder":"qwen_3_4b_q8p.ckpt","version":"flux2_4b"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"flux_2_vae_f16.ckpt","file":"flux_2_klein_4b_q8p.ckpt","text_encoder":"qwen_3_4b_q8p.ckpt","version":"flux2_4b"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_2_vae_f16.ckpt","file":"flux_2_klein_9b_f16.ckpt","text_encoder":"qwen_3_8b_f16.ckpt","version":"flux2_9b"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_2_vae_f16.ckpt","file":"flux_2_klein_9b_i8x.ckpt","text_encoder":"qwen_3_8b_q8p.ckpt","version":"flux2_9b"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_2_vae_f16.ckpt","file":"flux_2_klein_9b_kv_f16.ckpt","text_encoder":"qwen_3_8b_f16.ckpt","version":"flux2_9b"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_2_vae_f16.ckpt","file":"flux_2_klein_9b_kv_i8x.ckpt","text_encoder":"qwen_3_8b_q8p.ckpt","version":"flux2_9b"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_2_vae_f16.ckpt","file":"flux_2_klein_9b_kv_q6p.ckpt","text_encoder":"qwen_3_8b_q8p.ckpt","version":"flux2_9b"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_2_vae_f16.ckpt","file":"flux_2_klein_9b_kv_q8p.ckpt","text_encoder":"qwen_3_8b_q8p.ckpt","version":"flux2_9b"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_2_vae_f16.ckpt","file":"flux_2_klein_9b_q6p.ckpt","text_encoder":"qwen_3_8b_q8p.ckpt","version":"flux2_9b"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_2_vae_f16.ckpt","file":"flux_2_klein_9b_q8p.ckpt","text_encoder":"qwen_3_8b_q8p.ckpt","version":"flux2_9b"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_2_vae_f16.ckpt","file":"flux_2_klein_base_4b_f16.ckpt","text_encoder":"qwen_3_4b_f16.ckpt","version":"flux2_4b"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"flux_2_vae_f16.ckpt","file":"flux_2_klein_base_4b_i8x.ckpt","text_encoder":"qwen_3_4b_q8p.ckpt","version":"flux2_4b"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"flux_2_vae_f16.ckpt","file":"flux_2_klein_base_4b_q6p.ckpt","text_encoder":"qwen_3_4b_q8p.ckpt","version":"flux2_4b"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"flux_2_vae_f16.ckpt","file":"flux_2_klein_base_4b_q8p.ckpt","text_encoder":"qwen_3_4b_q8p.ckpt","version":"flux2_4b"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_2_vae_f16.ckpt","file":"flux_2_klein_base_9b_f16.ckpt","text_encoder":"qwen_3_8b_f16.ckpt","version":"flux2_9b"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_2_vae_f16.ckpt","file":"flux_2_klein_base_9b_i8x.ckpt","text_encoder":"qwen_3_8b_q8p.ckpt","version":"flux2_9b"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_2_vae_f16.ckpt","file":"flux_2_klein_base_9b_q6p.ckpt","text_encoder":"qwen_3_8b_q8p.ckpt","version":"flux2_9b"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_2_vae_f16.ckpt","file":"flux_2_klein_base_9b_q8p.ckpt","text_encoder":"qwen_3_8b_q8p.ckpt","version":"flux2_9b"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"flux_2_vae_f16.ckpt","file":"flux_3_klein_base_i8x.ckpt","text_encoder":"qwen_3_vl_4b_instruct_i8x.ckpt","version":"flux3_5b"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"flux_2_vae_f16.ckpt","file":"flux_3_klein_i8x.ckpt","text_encoder":"qwen_3_vl_4b_instruct_i8x.ckpt","version":"flux3_5b"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"flux_2_vae_f16.ckpt","file":"flux_3_klein_q8p.ckpt","text_encoder":"qwen_3_vl_4b_instruct_i8x.ckpt","version":"flux3_5b"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"fooocus_inpaint_sd_xl_v2.6_f16.ckpt","text_encoder":"open_clip_vit_bigg14_f16.ckpt","version":"sdxlbase"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"fooocus_inpaint_sd_xl_v2.6_q6p_q8p.ckpt","text_encoder":"open_clip_vit_bigg14_f16.ckpt","version":"sdxlbase"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"furrytoonmix_clip_vit_l14_f16.ckpt","file":"furrytoonmix_f16.ckpt","text_encoder":"furrytoonmix_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"getphat_flux_reality_nsfw_v11_softcore_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things curated models.json","file":"ghibli_v1_f16.ckpt","text_encoder":"ghibli_v1_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things curated models.json","file":"hassanblend_v1.5.1.2_f16.ckpt","text_encoder":"hassanblend_v1.5.1.2_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"long_clip_vit_l14_f16.ckpt","file":"hidream_e1_1_f16.ckpt","text_encoder":"llama_3.1_8b_instruct_f16.ckpt","version":"hidream_i1"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"long_clip_vit_l14_f16.ckpt","file":"hidream_e1_1_q5p.ckpt","text_encoder":"llama_3.1_8b_instruct_q8p.ckpt","version":"hidreami1"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"long_clip_vit_l14_f16.ckpt","file":"hidream_e1_1_q8p.ckpt","text_encoder":"llama_3.1_8b_instruct_q8p.ckpt","version":"hidreami1"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"long_clip_vit_l14_f16.ckpt","file":"hidream_e1_full_q5p.ckpt","text_encoder":"llama_3.1_8b_instruct_q8p.ckpt","version":"hidreami1"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"long_clip_vit_l14_f16.ckpt","file":"hidream_e1_full_q8p.ckpt","text_encoder":"llama_3.1_8b_instruct_q8p.ckpt","version":"hidreami1"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"long_clip_vit_l14_f16.ckpt","file":"hidream_i1_dev_f16.ckpt","text_encoder":"llama_3.1_8b_instruct_f16.ckpt","version":"hidream_i1"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"long_clip_vit_l14_f16.ckpt","file":"hidream_i1_dev_i8x.ckpt","text_encoder":"llama_3.1_8b_instruct_q8p.ckpt","version":"hidreami1"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"long_clip_vit_l14_f16.ckpt","file":"hidream_i1_dev_q5p.ckpt","text_encoder":"llama_3.1_8b_instruct_q8p.ckpt","version":"hidreami1"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"long_clip_vit_l14_f16.ckpt","file":"hidream_i1_dev_q8p.ckpt","text_encoder":"llama_3.1_8b_instruct_q8p.ckpt","version":"hidreami1"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"long_clip_vit_l14_f16.ckpt","file":"hidream_i1_fast_f16.ckpt","text_encoder":"llama_3.1_8b_instruct_f16.ckpt","version":"hidream_i1"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"long_clip_vit_l14_f16.ckpt","file":"hidream_i1_fast_i8x.ckpt","text_encoder":"llama_3.1_8b_instruct_q8p.ckpt","version":"hidreami1"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"long_clip_vit_l14_f16.ckpt","file":"hidream_i1_fast_q5p.ckpt","text_encoder":"llama_3.1_8b_instruct_q8p.ckpt","version":"hidreami1"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"long_clip_vit_l14_f16.ckpt","file":"hidream_i1_fast_q8p.ckpt","text_encoder":"llama_3.1_8b_instruct_q8p.ckpt","version":"hidreami1"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"long_clip_vit_l14_f16.ckpt","file":"hidream_i1_full_f16.ckpt","text_encoder":"llama_3.1_8b_instruct_f16.ckpt","version":"hidream_i1"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"long_clip_vit_l14_f16.ckpt","file":"hidream_i1_full_i8x.ckpt","text_encoder":"llama_3.1_8b_instruct_q8p.ckpt","version":"hidreami1"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"long_clip_vit_l14_f16.ckpt","file":"hidream_i1_full_q5p.ckpt","text_encoder":"llama_3.1_8b_instruct_q8p.ckpt","version":"hidreami1"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"long_clip_vit_l14_f16.ckpt","file":"hidream_i1_full_q8p.ckpt","text_encoder":"llama_3.1_8b_instruct_q8p.ckpt","version":"hidreami1"},{"_origin":"Draw Things curated models.json","file":"hna_3dkx_1.1_f16.ckpt","text_encoder":"hna_3dkx_1.1_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"homosimile_xl_pony__illustrious__noobai_clip_vit_l14_f16.ckpt","file":"homosimile_xl_pony__illustrious__noobai_f16.ckpt","text_encoder":"homosimile_xl_pony__illustrious__noobai_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"hoseki_lustrousmix_illustriousxl_clip_vit_l14_f16.ckpt","file":"hoseki_lustrousmix_illustriousxl_f16.ckpt","text_encoder":"hoseki_lustrousmix_illustriousxl_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"hoseki_lustrousmix_pony_xl_clip_vit_l14_f16.ckpt","file":"hoseki_lustrousmix_pony_xl_f16.ckpt","text_encoder":"hoseki_lustrousmix_pony_xl_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"hs_ultrahd_cg_clip_vit_l14_f16.ckpt","file":"hs_ultrahd_cg_f16.ckpt","text_encoder":"hs_ultrahd_cg_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"hunyuan_video_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"hunyuan_video_t2v_720p_q5p_svd.ckpt","text_encoder":"llava_llama_3_8b_v1.1_q8p.ckpt","version":"hunyuan_video"},{"_origin":"Draw Things curated models.json","autoencoder":"hunyuan_video_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"hunyuan_video_t2v_720p_q8p.ckpt","text_encoder":"llava_llama_3_8b_v1.1_q8p.ckpt","version":"hunyuan_video"},{"_origin":"Draw Things curated models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"icatcher_cartoon_clip_vit_l14_f16.ckpt","file":"icatcher_cartoon_f16.ckpt","text_encoder":"icatcher_cartoon_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"icatcher_cartoon_clip_vit_l14_f16.ckpt","file":"icatcher_cartoon_q6p_q8p.ckpt","text_encoder":"icatcher_cartoon_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"icatcher_realistic_clip_vit_l14_f16.ckpt","file":"icatcher_realistic_f16.ckpt","text_encoder":"icatcher_realistic_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"icatcher_realistic_clip_vit_l14_f16.ckpt","file":"icatcher_realistic_q6p_q8p.ckpt","text_encoder":"icatcher_realistic_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"icedcoffeeil_clip_vit_l14_f16.ckpt","file":"icedcoffeeil_f16.ckpt","text_encoder":"icedcoffeeil_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_2_vae_f16.ckpt","file":"ideogram_4_fast_i6x.ckpt","text_encoder":"qwen_3_vl_8b_instruct_q8p.ckpt","version":"ideogram_4"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_2_vae_f16.ckpt","file":"ideogram_4_fast_i8x.ckpt","text_encoder":"qwen_3_vl_8b_instruct_q8p.ckpt","version":"ideogram_4"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_2_vae_f16.ckpt","file":"ideogram_4_fast_q6p.ckpt","text_encoder":"qwen_3_vl_8b_instruct_q8p.ckpt","version":"ideogram_4"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_2_vae_f16.ckpt","file":"ideogram_4_fast_q8p.ckpt","text_encoder":"qwen_3_vl_8b_instruct_q8p.ckpt","version":"ideogram_4"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_2_vae_f16.ckpt","file":"ideogram_4_i6x.ckpt","text_encoder":"qwen_3_vl_8b_instruct_q8p.ckpt","version":"ideogram_4"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_2_vae_f16.ckpt","file":"ideogram_4_i8x.ckpt","text_encoder":"qwen_3_vl_8b_instruct_q8p.ckpt","version":"ideogram_4"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_2_vae_f16.ckpt","file":"ideogram_4_instant_i6x.ckpt","text_encoder":"qwen_3_vl_8b_instruct_q8p.ckpt","version":"ideogram_4"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_2_vae_f16.ckpt","file":"ideogram_4_instant_i8x.ckpt","text_encoder":"qwen_3_vl_8b_instruct_q8p.ckpt","version":"ideogram_4"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_2_vae_f16.ckpt","file":"ideogram_4_instant_q6p.ckpt","text_encoder":"qwen_3_vl_8b_instruct_q8p.ckpt","version":"ideogram_4"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_2_vae_f16.ckpt","file":"ideogram_4_instant_q8p.ckpt","text_encoder":"qwen_3_vl_8b_instruct_q8p.ckpt","version":"ideogram_4"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_2_vae_f16.ckpt","file":"ideogram_4_q6p.ckpt","text_encoder":"qwen_3_vl_8b_instruct_q8p.ckpt","version":"ideogram_4"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_2_vae_f16.ckpt","file":"ideogram_4_q8p.ckpt","text_encoder":"qwen_3_vl_8b_instruct_q8p.ckpt","version":"ideogram_4"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"iffymix_xl_clip_vit_l14_f16.ckpt","file":"iffymix_xl_f16.ckpt","text_encoder":"iffymix_xl_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"illumiyume_xl_illustrious_clip_vit_l14_f16.ckpt","file":"illumiyume_xl_illustrious_f16.ckpt","text_encoder":"illumiyume_xl_illustrious_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"illustrij_clip_vit_l14_f16.ckpt","file":"illustrij_f16.ckpt","text_encoder":"illustrij_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"illustrij_genv1_clip_vit_l14_f16.ckpt","file":"illustrij_genv1_f16.ckpt","text_encoder":"illustrij_genv1_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"illustrij_genv1_clip_vit_l14_f16.ckpt","file":"illustrij_genv1_q6p_q8p.ckpt","text_encoder":"illustrij_genv1_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"illustrious_black_magic_illustrious_noob_more_clip_vit_l14_f16.ckpt","file":"illustrious_black_magic_illustrious_noob_more_f16.ckpt","text_encoder":"illustrious_black_magic_illustrious_noob_more_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"illustrious_gehenna_illustrious_checkpoint_clip_vit_l14_f16.ckpt","file":"illustrious_gehenna_illustrious_checkpoint_f16.ckpt","text_encoder":"illustrious_gehenna_illustrious_checkpoint_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"illustrious_realism_by_klaabu_clip_vit_l14_f16.ckpt","file":"illustrious_realism_by_klaabu_f16.ckpt","text_encoder":"illustrious_realism_by_klaabu_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"illustrious_xl_clip_vit_l14_f16.ckpt","file":"illustrious_xl_f16.ckpt","text_encoder":"illustrious_xl_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"illustrious_xl_personal_merge_noob_v_pred0.5_test_merge_updated_clip_vit_l14_f16.ckpt","file":"illustrious_xl_personal_merge_noob_v_pred0.5_test_merge_updated_f16.ckpt","text_encoder":"illustrious_xl_personal_merge_noob_v_pred0.5_test_merge_updated_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"inbetweij___no.04_clip_vit_l14_f16.ckpt","file":"inbetweij___no.04_f16.ckpt","text_encoder":"inbetweij___no.04_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"inbetweij___no.04_clip_vit_l14_f16.ckpt","file":"inbetweij___no.04_q6p_q8p.ckpt","text_encoder":"inbetweij___no.04_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"indigo_furry_mix_xl_clip_vit_l14_f16.ckpt","file":"indigo_furry_mix_xl_f16.ckpt","text_encoder":"indigo_furry_mix_xl_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"indigo_void_furry_fused_xl_furry_noobai_noobai_v1.7_1569147_clip_vit_l14_f16.ckpt","file":"indigo_void_furry_fused_xl_furry_noobai_noobai_v1.7_1569147_f16.ckpt","text_encoder":"indigo_void_furry_fused_xl_furry_noobai_noobai_v1.7_1569147_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"indigo_void_furry_fused_xl_furry_noobai_noobai_v1.8_1731573_clip_vit_l14_f16.ckpt","file":"indigo_void_furry_fused_xl_furry_noobai_noobai_v1.8_1731573_f16.ckpt","text_encoder":"indigo_void_furry_fused_xl_furry_noobai_noobai_v1.8_1731573_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"indigo_void_furry_vpred_furry_noobai_v_pred_clip_vit_l14_f16.ckpt","file":"indigo_void_furry_vpred_furry_noobai_v_pred_f16.ckpt","text_encoder":"indigo_void_furry_vpred_furry_noobai_v_pred_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","file":"inkpunk_v2_f16.ckpt","text_encoder":"inkpunk_v2_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","file":"instruct_pix2pix_22000_f16.ckpt","version":"v1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"ionsyx_clip_vit_l14_f16.ckpt","file":"ionsyx_f16.ckpt","text_encoder":"ionsyx_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"jib_mix_pony_realistic_clip_vit_l14_f16.ckpt","file":"jib_mix_pony_realistic_f16.ckpt","text_encoder":"jib_mix_pony_realistic_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"jib_mix_realistic_xl_clip_vit_l14_f16.ckpt","file":"jib_mix_realistic_xl_f16.ckpt","text_encoder":"jib_mix_realistic_xl_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","file":"juggernaut_reborn_f16.ckpt","text_encoder":"juggernaut_reborn_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things curated models.json","file":"juggernaut_reborn_q6p_q8p.ckpt","text_encoder":"juggernaut_reborn_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things curated models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"juggernaut_xl_ragnarok_clip_vit_l14_f16.ckpt","file":"juggernaut_xl_ragnarok_f16.ckpt","text_encoder":"juggernaut_xl_ragnarok_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"juggernaut_xl_ragnarok_clip_vit_l14_f16.ckpt","file":"juggernaut_xl_ragnarok_q6p_q8p.ckpt","text_encoder":"juggernaut_xl_ragnarok_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"juggernaut_xl_v9_clip_vit_l14_f16.ckpt","file":"juggernaut_xl_v9_f16.ckpt","text_encoder":"juggernaut_xl_v9_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"juggernaut_xl_v9_lightning_clip_vit_l14_f16.ckpt","file":"juggernaut_xl_v9_lightning_f16.ckpt","text_encoder":"juggernaut_xl_v9_lightning_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"juggernaut_xl_v9_lightning_clip_vit_l14_f16.ckpt","file":"juggernaut_xl_v9_lightning_q6p_q8p.ckpt","text_encoder":"juggernaut_xl_v9_lightning_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"juggernaut_xl_v9_clip_vit_l14_f16.ckpt","file":"juggernaut_xl_v9_q6p_q8p.ckpt","text_encoder":"juggernaut_xl_v9_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"juggernaut_xl_x_clip_vit_l14_f16.ckpt","file":"juggernaut_xl_x_f16.ckpt","text_encoder":"juggernaut_xl_x_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"juggernaut_xl_x_clip_vit_l14_f16.ckpt","file":"juggernaut_xl_x_q6p_q8p.ckpt","text_encoder":"juggernaut_xl_x_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"jujumix_clip_vit_l14_f16.ckpt","file":"jujumix_f16.ckpt","text_encoder":"jujumix_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"kageillustrious_illustrious_xl_clip_vit_l14_f16.ckpt","file":"kageillustrious_illustrious_xl_f16.ckpt","text_encoder":"kageillustrious_illustrious_xl_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"kandinsky_movq_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"kandinsky_f16.ckpt","text_encoder":"xlm_roberta_f16.ckpt","version":"kandinsky21"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"kantanmix_xlillustrious_clip_vit_l14_f16.ckpt","file":"kantanmix_xlillustrious_f16.ckpt","text_encoder":"kantanmix_xlillustrious_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"kft_requiem_fur_a_dream_illustrious_semi_real_checkpoint_clip_vit_l14_f16.ckpt","file":"kft_requiem_fur_a_dream_illustrious_semi_real_checkpoint_f16.ckpt","text_encoder":"kft_requiem_fur_a_dream_illustrious_semi_real_checkpoint_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"konanmix_noobai_xl_illustriousxl_clip_vit_l14_f16.ckpt","file":"konanmix_noobai_xl_illustriousxl_f16.ckpt","text_encoder":"konanmix_noobai_xl_illustriousxl_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"koronemix_illustrious_clip_vit_l14_f16.ckpt","file":"koronemix_illustrious_f16.ckpt","text_encoder":"koronemix_illustrious_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"qwen_image_vae_f16.ckpt","clip_encoder":"krea_2_raw_i4x.ckpt","file":"krea_2_raw_i4x.ckpt","text_encoder":"qwen_3_vl_4b_q8p.ckpt","version":"krea_2"},{"_origin":"Draw Things curated models.json","autoencoder":"qwen_image_vae_f16.ckpt","clip_encoder":"krea_2_raw_i8x.ckpt","file":"krea_2_raw_i8x.ckpt","text_encoder":"qwen_3_vl_4b_q8p.ckpt","version":"krea_2"},{"_origin":"Draw Things curated models.json","autoencoder":"qwen_image_vae_f16.ckpt","clip_encoder":"krea_2_raw_q6p.ckpt","file":"krea_2_raw_q6p.ckpt","text_encoder":"qwen_3_vl_4b_q8p.ckpt","version":"krea_2"},{"_origin":"Draw Things curated models.json","autoencoder":"qwen_image_vae_f16.ckpt","clip_encoder":"krea_2_raw_q8p.ckpt","file":"krea_2_raw_q8p.ckpt","text_encoder":"qwen_3_vl_4b_q8p.ckpt","version":"krea_2"},{"_origin":"Draw Things curated models.json","autoencoder":"qwen_image_vae_f16.ckpt","clip_encoder":"krea_2_turbo_i4x.ckpt","file":"krea_2_turbo_i4x.ckpt","text_encoder":"qwen_3_vl_4b_q8p.ckpt","version":"krea_2"},{"_origin":"Draw Things curated models.json","autoencoder":"qwen_image_vae_f16.ckpt","clip_encoder":"krea_2_turbo_i6x.ckpt","file":"krea_2_turbo_i6x.ckpt","text_encoder":"qwen_3_vl_4b_q8p.ckpt","version":"krea_2"},{"_origin":"Draw Things curated models.json","autoencoder":"qwen_image_vae_f16.ckpt","clip_encoder":"krea_2_turbo_i8x.ckpt","file":"krea_2_turbo_i8x.ckpt","text_encoder":"qwen_3_vl_4b_q8p.ckpt","version":"krea_2"},{"_origin":"Draw Things curated models.json","autoencoder":"qwen_image_vae_f16.ckpt","clip_encoder":"krea_2_turbo_q6p.ckpt","file":"krea_2_turbo_q6p.ckpt","text_encoder":"qwen_3_vl_4b_q8p.ckpt","version":"krea_2"},{"_origin":"Draw Things curated models.json","autoencoder":"qwen_image_vae_f16.ckpt","clip_encoder":"krea_2_turbo_q8p.ckpt","file":"krea_2_turbo_q8p.ckpt","text_encoder":"qwen_3_vl_4b_q8p.ckpt","version":"krea_2"},{"_origin":"Draw Things curated models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","file":"kwai_kolors_1.0_f16.ckpt","text_encoder":"chatglm3_6b_q6p_q8p.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","file":"kwai_kolors_1.0_q6p_q8p.ckpt","text_encoder":"chatglm3_6b_q6p_q8p.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","file":"kwai_kolors_inpainting_1.0_f16.ckpt","text_encoder":"chatglm3_6b_q6p_q8p.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","file":"kwai_kolors_inpainting_1.0_q6p_q8p.ckpt","text_encoder":"chatglm3_6b_q6p_q8p.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"like_reality_pony_by_ethanar_clip_vit_l14_f16.ckpt","file":"like_reality_pony_by_ethanar_f16.ckpt","text_encoder":"like_reality_pony_by_ethanar_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"lox_s_souls_mix_prisma__clip_vit_l14_f16.ckpt","file":"lox_s_souls_mix_prisma__f16.ckpt","text_encoder":"lox_s_souls_mix_prisma__open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"ltx_2.3_audio_video_vae_f16.ckpt","clip_encoder":"ltx_2.3_22b_dev_f16.ckpt","file":"ltx_2.3_22b_dev_f16.ckpt","text_encoder":"gemma_3_12b_it_qat_f16.ckpt","version":"ltx2_3"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"ltx_2.3_audio_video_vae_f16.ckpt","clip_encoder":"ltx_2.3_22b_dev_i8x.ckpt","file":"ltx_2.3_22b_dev_i8x.ckpt","text_encoder":"gemma_3_12b_it_qat_q8p.ckpt","version":"ltx2_3"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"ltx_2.3_audio_video_vae_f16.ckpt","clip_encoder":"ltx_2.3_22b_dev_q6p.ckpt","file":"ltx_2.3_22b_dev_q6p.ckpt","text_encoder":"gemma_3_12b_it_qat_q8p.ckpt","version":"ltx2_3"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"ltx_2.3_audio_video_vae_f16.ckpt","clip_encoder":"ltx_2.3_22b_dev_q8p.ckpt","file":"ltx_2.3_22b_dev_q8p.ckpt","text_encoder":"gemma_3_12b_it_qat_q8p.ckpt","version":"ltx2_3"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"ltx_2.3_audio_video_vae_f16.ckpt","clip_encoder":"ltx_2.3_22b_distilled_1.1_i8x.ckpt","file":"ltx_2.3_22b_distilled_1.1_i8x.ckpt","text_encoder":"gemma_3_12b_it_qat_q8p.ckpt","version":"ltx2_3"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"ltx_2.3_audio_video_vae_f16.ckpt","clip_encoder":"ltx_2.3_22b_distilled_1.1_q6p.ckpt","file":"ltx_2.3_22b_distilled_1.1_q6p.ckpt","text_encoder":"gemma_3_12b_it_qat_q8p.ckpt","version":"ltx2_3"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"ltx_2.3_audio_video_vae_f16.ckpt","clip_encoder":"ltx_2.3_22b_distilled_1.1_q8p.ckpt","file":"ltx_2.3_22b_distilled_1.1_q8p.ckpt","text_encoder":"gemma_3_12b_it_qat_q8p.ckpt","version":"ltx2_3"},{"_origin":"Draw Things curated models.json","autoencoder":"ltx_2.3_audio_video_vae_f16.ckpt","clip_encoder":"ltx_2.3_22b_distilled_f16.ckpt","file":"ltx_2.3_22b_distilled_f16.ckpt","text_encoder":"gemma_3_12b_it_qat_f16.ckpt","version":"ltx2_3"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"ltx_2.3_audio_video_vae_f16.ckpt","clip_encoder":"ltx_2.3_22b_distilled_i8x.ckpt","file":"ltx_2.3_22b_distilled_i8x.ckpt","text_encoder":"gemma_3_12b_it_qat_q8p.ckpt","version":"ltx2_3"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"ltx_2.3_audio_video_vae_f16.ckpt","clip_encoder":"ltx_2.3_22b_distilled_q6p.ckpt","file":"ltx_2.3_22b_distilled_q6p.ckpt","text_encoder":"gemma_3_12b_it_qat_q8p.ckpt","version":"ltx2_3"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"ltx_2.3_audio_video_vae_f16.ckpt","clip_encoder":"ltx_2.3_22b_distilled_q8p.ckpt","file":"ltx_2.3_22b_distilled_q8p.ckpt","text_encoder":"gemma_3_12b_it_qat_q8p.ckpt","version":"ltx2_3"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"ltx_2_audio_video_vae_f16.ckpt","clip_encoder":"ltx_2_19b_dev_q6p.ckpt","file":"ltx_2_19b_dev_q6p.ckpt","text_encoder":"gemma_3_12b_it_qat_q8p.ckpt","version":"ltx2"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"ltx_2_audio_video_vae_f16.ckpt","clip_encoder":"ltx_2_19b_dev_q8p.ckpt","file":"ltx_2_19b_dev_q8p.ckpt","text_encoder":"gemma_3_12b_it_qat_q8p.ckpt","version":"ltx2"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"ltx_2_audio_video_vae_f16.ckpt","clip_encoder":"ltx_2_19b_distilled_q6p.ckpt","file":"ltx_2_19b_distilled_q6p.ckpt","text_encoder":"gemma_3_12b_it_qat_q8p.ckpt","version":"ltx2"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"ltx_2_audio_video_vae_f16.ckpt","clip_encoder":"ltx_2_19b_distilled_q8p.ckpt","file":"ltx_2_19b_distilled_q8p.ckpt","text_encoder":"gemma_3_12b_it_qat_q8p.ckpt","version":"ltx2"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"lunargrapemix_illustrious_clip_vit_l14_f16.ckpt","file":"lunargrapemix_illustrious_f16.ckpt","text_encoder":"lunargrapemix_illustrious_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","file":"lvngvncnt_v2_f16.ckpt","text_encoder":"lvngvncnt_v2_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"madly_mix_ver_nightnoob_clip_vit_l14_f16.ckpt","file":"madly_mix_ver_nightnoob_f16.ckpt","text_encoder":"madly_mix_ver_nightnoob_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","file":"majicmix_realistic_v7_f16.ckpt","text_encoder":"majicmix_realistic_v7_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things curated models.json","file":"majicmix_realistic_v7_q6p_q8p.ckpt","text_encoder":"majicmix_realistic_v7_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things curated models.json","file":"mdjrny_v4_f16.ckpt","text_encoder":"mdjrny_v4_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"meichidarkmix_reload_clip_vit_l14_f16.ckpt","file":"meichidarkmix_reload_f16.ckpt","text_encoder":"meichidarkmix_reload_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"minimax_h3_vae_f16.ckpt","file":"minimax_h3_fl2va_i4x.ckpt","text_encoder":"qwen_3_vl_32b_50_i8x.ckpt","version":"minimax_h3"},{"_origin":"Draw Things curated models.json","autoencoder":"minimax_h3_vae_f16.ckpt","file":"minimax_h3_fl2va_i8x.ckpt","text_encoder":"qwen_3_vl_32b_50_i8x.ckpt","version":"minimax_h3"},{"_origin":"Draw Things curated models.json","autoencoder":"minimax_h3_vae_f16.ckpt","file":"minimax_h3_fl2va_q6p.ckpt","text_encoder":"qwen_3_vl_32b_50_i8x.ckpt","version":"minimax_h3"},{"_origin":"Draw Things curated models.json","autoencoder":"minimax_h3_vae_f16.ckpt","file":"minimax_h3_fl2va_q8p.ckpt","text_encoder":"qwen_3_vl_32b_50_i8x.ckpt","version":"minimax_h3"},{"_origin":"Draw Things curated models.json","autoencoder":"minimax_h3_vae_f16.ckpt","file":"minimax_h3_ref2va_i4x.ckpt","text_encoder":"qwen_3_vl_32b_50_i8x.ckpt","version":"minimax_h3"},{"_origin":"Draw Things curated models.json","autoencoder":"minimax_h3_vae_f16.ckpt","file":"minimax_h3_ref2va_i8x.ckpt","text_encoder":"qwen_3_vl_32b_50_i8x.ckpt","version":"minimax_h3"},{"_origin":"Draw Things curated models.json","autoencoder":"minimax_h3_vae_f16.ckpt","file":"minimax_h3_ref2va_q6p.ckpt","text_encoder":"qwen_3_vl_32b_50_i8x.ckpt","version":"minimax_h3"},{"_origin":"Draw Things curated models.json","autoencoder":"minimax_h3_vae_f16.ckpt","file":"minimax_h3_ref2va_q8p.ckpt","text_encoder":"qwen_3_vl_32b_50_i8x.ckpt","version":"minimax_h3"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","file":"minisd_v1.4_f16.ckpt","version":"v1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"miruku_clip_vit_l14_f16.ckpt","file":"miruku_f16.ckpt","text_encoder":"miruku_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"mistoon_anime_clip_vit_l14_f16.ckpt","file":"mistoon_anime_f16.ckpt","text_encoder":"mistoon_anime_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","file":"modi_v1_f16.ckpt","text_encoder":"modi_v1_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"mol_keun_mix_clip_vit_l14_f16.ckpt","file":"mol_keun_mix_f16.ckpt","text_encoder":"mol_keun_mix_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"molcajetemix_noobil_clip_vit_l14_f16.ckpt","file":"molcajetemix_noobil_f16.ckpt","text_encoder":"molcajetemix_noobil_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"nevolution_sdxl_v2_anim_1733673_clip_vit_l14_f16.ckpt","file":"nevolution_sdxl_v2_anim_1733673_f16.ckpt","text_encoder":"nevolution_sdxl_v2_anim_1733673_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"nevolution_sdxl_v2_base_1645671_clip_vit_l14_f16.ckpt","file":"nevolution_sdxl_v2_base_1645671_f16.ckpt","text_encoder":"nevolution_sdxl_v2_base_1645671_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","file":"nitro_v1_f16.ckpt","text_encoder":"nitro_v1_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"noobai_xl_nai_xl_clip_vit_l14_f16.ckpt","file":"noobai_xl_nai_xl_f16.ckpt","text_encoder":"noobai_xl_nai_xl_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"nostramix_clip_vit_l14_f16.ckpt","file":"nostramix_f16.ckpt","text_encoder":"nostramix_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"nova_3dcg_xl_clip_vit_l14_f16.ckpt","file":"nova_3dcg_xl_f16.ckpt","text_encoder":"nova_3dcg_xl_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"nova_animal_xl_illustrious_v3.0_1634769_clip_vit_l14_f16.ckpt","file":"nova_animal_xl_illustrious_v3.0_1634769_f16.ckpt","text_encoder":"nova_animal_xl_illustrious_v3.0_1634769_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"nova_animal_xl_v4.0_1028888_clip_vit_l14_f16.ckpt","file":"nova_animal_xl_v4.0_1028888_f16.ckpt","text_encoder":"nova_animal_xl_v4.0_1028888_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"nova_anime3d_xl_clip_vit_l14_f16.ckpt","file":"nova_anime3d_xl_f16.ckpt","text_encoder":"nova_anime3d_xl_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"nova_anime_xl_clip_vit_l14_f16.ckpt","file":"nova_anime_xl_f16.ckpt","text_encoder":"nova_anime_xl_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"nova_asian_xl_clip_vit_l14_f16.ckpt","file":"nova_asian_xl_f16.ckpt","text_encoder":"nova_asian_xl_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"nova_cross_xl_clip_vit_l14_f16.ckpt","file":"nova_cross_xl_f16.ckpt","text_encoder":"nova_cross_xl_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"nova_flat_xl_clip_vit_l14_f16.ckpt","file":"nova_flat_xl_f16.ckpt","text_encoder":"nova_flat_xl_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"nova_furry_xl_illustrious_v6a_1625975_clip_vit_l14_f16.ckpt","file":"nova_furry_xl_illustrious_v6a_1625975_f16.ckpt","text_encoder":"nova_furry_xl_illustrious_v6a_1625975_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"nova_furry_xl_illustrious_v6b_1626156_clip_vit_l14_f16.ckpt","file":"nova_furry_xl_illustrious_v6b_1626156_f16.ckpt","text_encoder":"nova_furry_xl_illustrious_v6b_1626156_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"nova_furry_xl_illustrious_v7a_1759909_clip_vit_l14_f16.ckpt","file":"nova_furry_xl_illustrious_v7a_1759909_f16.ckpt","text_encoder":"nova_furry_xl_illustrious_v7a_1759909_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"nova_furry_xl_illustrious_v7b_1760102_clip_vit_l14_f16.ckpt","file":"nova_furry_xl_illustrious_v7b_1760102_f16.ckpt","text_encoder":"nova_furry_xl_illustrious_v7b_1760102_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"nova_orange_xl_v7.0_1525944_clip_vit_l14_f16.ckpt","file":"nova_orange_xl_v7.0_1525944_f16.ckpt","text_encoder":"nova_orange_xl_v7.0_1525944_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"nova_orange_xl_v9.0_1701002_clip_vit_l14_f16.ckpt","file":"nova_orange_xl_v9.0_1701002_f16.ckpt","text_encoder":"nova_orange_xl_v9.0_1701002_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"nova_reality_xl_clip_vit_l14_f16.ckpt","file":"nova_reality_xl_f16.ckpt","text_encoder":"nova_reality_xl_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"nova_unreal_xl_clip_vit_l14_f16.ckpt","file":"nova_unreal_xl_f16.ckpt","text_encoder":"nova_unreal_xl_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"one_for_all_pony_fantasy_dpovae_clip_vit_l14_f16.ckpt","file":"one_for_all_pony_fantasy_dpovae_f16.ckpt","text_encoder":"one_for_all_pony_fantasy_dpovae_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"oops_all_toons_illustrious_v1.0_clip_vit_l14_f16.ckpt","file":"oops_all_toons_illustrious_v1.0_f16.ckpt","text_encoder":"oops_all_toons_illustrious_v1.0_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"oops_all_vpred_noobai_western_2d_furry_toon_clip_vit_l14_f16.ckpt","file":"oops_all_vpred_noobai_western_2d_furry_toon_f16.ckpt","text_encoder":"oops_all_vpred_noobai_western_2d_furry_toon_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"vae_ft_mse_840000_f16.ckpt","file":"ootd_vton_full_body_1.0_f16.ckpt","text_encoder":"clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things curated models.json","autoencoder":"vae_ft_mse_840000_f16.ckpt","file":"ootd_vton_upper_body_1.0_f16.ckpt","text_encoder":"clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things curated models.json","file":"papercut_v1_f16.ckpt","text_encoder":"papercut_v1_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"photonic_fusion_sdxl_clip_vit_l14_f16.ckpt","file":"photonic_fusion_sdxl_f16.ckpt","text_encoder":"photonic_fusion_sdxl_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"pikon_realism_v2_alt_clip_vit_l14_f16.ckpt","file":"pikon_realism_v2_alt_f16.ckpt","text_encoder":"pikon_realism_v2_alt_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"pikon_realism_v2_alt_clip_vit_l14_f16.ckpt","file":"pikon_realism_v2_alt_q6p_q8p.ckpt","text_encoder":"pikon_realism_v2_alt_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"sdxl_vae_v1.0_f16.ckpt","file":"pixart_sigma_xl_2_1024_ms_f16.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"pixart"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"sdxl_vae_v1.0_f16.ckpt","file":"pixart_sigma_xl_2_1024_ms_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"pixart"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"sdxl_vae_v1.0_f16.ckpt","file":"pixart_sigma_xl_2_512_ms_f16.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"pixart"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"sdxl_vae_v1.0_f16.ckpt","file":"pixart_sigma_xl_2_512_ms_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"pixart"},{"_origin":"Draw Things curated models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"pixelwave_10_clip_vit_l14_f16.ckpt","file":"pixelwave_10_f16.ckpt","text_encoder":"pixelwave_10_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"pixelwave_10_clip_vit_l14_f16.ckpt","file":"pixelwave_10_q6p_q8p.ckpt","text_encoder":"pixelwave_10_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"pixelwave_flux_1_schnell_04_q5p_svd.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"pixelwave_flux_1_schnell_04_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things curated models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"pixelwave_v9_clip_vit_l14_f16.ckpt","file":"pixelwave_v9_f16.ckpt","text_encoder":"pixelwave_v9_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"pixelwave_v9_clip_vit_l14_f16.ckpt","file":"pixelwave_v9_q6p_q8p.ckpt","text_encoder":"pixelwave_v9_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"plant_milk_model_suite_flax_1331188_clip_vit_l14_f16.ckpt","file":"plant_milk_model_suite_flax_1331188_f16.ckpt","text_encoder":"plant_milk_model_suite_flax_1331188_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"plant_milk_model_suite_hemp_ii_1714314_clip_vit_l14_f16.ckpt","file":"plant_milk_model_suite_hemp_ii_1714314_f16.ckpt","text_encoder":"plant_milk_model_suite_hemp_ii_1714314_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"playground_v2.5_f16.ckpt","text_encoder":"open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"playground_v2.5_q6p_q8p.ckpt","text_encoder":"open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"playground_v2_f16.ckpt","text_encoder":"open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"playground_v2_q6p_q8p.ckpt","text_encoder":"open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"pony_realism_v2.1_534642_clip_vit_l14_f16.ckpt","file":"pony_realism_v2.1_534642_f16.ckpt","text_encoder":"pony_realism_v2.1_534642_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"pony_realism_v2.2_914390_clip_vit_l14_f16.ckpt","file":"pony_realism_v2.2_914390_f16.ckpt","text_encoder":"pony_realism_v2.2_914390_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"praeclarus_illustriousxl_clip_vit_l14_f16.ckpt","file":"praeclarus_illustriousxl_f16.ckpt","text_encoder":"praeclarus_illustriousxl_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"prefect_illustrious_xl_v1.0_1379960_clip_vit_l14_f16.ckpt","file":"prefect_illustrious_xl_v1.0_1379960_f16.ckpt","text_encoder":"prefect_illustrious_xl_v1.0_1379960_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"prefect_illustrious_xl_v1.5_1731647_clip_vit_l14_f16.ckpt","file":"prefect_illustrious_xl_v1.5_1731647_f16.ckpt","text_encoder":"prefect_illustrious_xl_v1.5_1731647_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"prefect_pony_xl_clip_vit_l14_f16.ckpt","file":"prefect_pony_xl_f16.ckpt","text_encoder":"prefect_pony_xl_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"proteus_v0.3_clip_vit_l14_f16.ckpt","file":"proteus_v0.3_f16.ckpt","text_encoder":"proteus_v0.3_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"proteus_v0.3_clip_vit_l14_f16.ckpt","file":"proteus_v0.3_q6p_q8p.ckpt","text_encoder":"proteus_v0.3_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"qwen_image_vae_f16.ckpt","file":"qwen_image_1.0_bf16.ckpt","text_encoder":"qwen_2.5_vl_7b_f16.ckpt","version":"qwen_image"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"qwen_image_vae_f16.ckpt","file":"qwen_image_1.0_bf16_q6p.ckpt","text_encoder":"qwen_2.5_vl_7b_q8p.ckpt","version":"qwen_image"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"qwen_image_vae_f16.ckpt","file":"qwen_image_1.0_bf16_q8p.ckpt","text_encoder":"qwen_2.5_vl_7b_q8p.ckpt","version":"qwen_image"},{"_origin":"Draw Things curated models.json","autoencoder":"qwen_image_vae_f16.ckpt","file":"qwen_image_1.0_f16.ckpt","text_encoder":"qwen_2.5_vl_7b_f16.ckpt","version":"qwen_image"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"qwen_image_vae_f16.ckpt","file":"qwen_image_1.0_q6p.ckpt","text_encoder":"qwen_2.5_vl_7b_q8p.ckpt","version":"qwen_image"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"qwen_image_vae_f16.ckpt","file":"qwen_image_1.0_q8p.ckpt","text_encoder":"qwen_2.5_vl_7b_q8p.ckpt","version":"qwen_image"},{"_origin":"Draw Things curated models.json","autoencoder":"qwen_image_2.1_vae_f16.ckpt","file":"qwen_image_2.1_f16.ckpt","text_encoder":"qwen_3_vl_8b_instruct_f16.ckpt","version":"qwen_image_2_1"},{"_origin":"Draw Things curated models.json","autoencoder":"qwen_image_2.1_vae_f16.ckpt","file":"qwen_image_2.1_i4x.ckpt","text_encoder":"qwen_3_vl_8b_instruct_i8x.ckpt","version":"qwen_image_2_1"},{"_origin":"Draw Things curated models.json","autoencoder":"qwen_image_2.1_vae_f16.ckpt","file":"qwen_image_2.1_i8x.ckpt","text_encoder":"qwen_3_vl_8b_instruct_i8x.ckpt","version":"qwen_image_2_1"},{"_origin":"Draw Things curated models.json","autoencoder":"qwen_image_2.1_vae_f16.ckpt","file":"qwen_image_2.1_q6p.ckpt","text_encoder":"qwen_3_vl_8b_instruct_i8x.ckpt","version":"qwen_image_2_1"},{"_origin":"Draw Things curated models.json","autoencoder":"qwen_image_2.1_vae_f16.ckpt","file":"qwen_image_2.1_q8p.ckpt","text_encoder":"qwen_3_vl_8b_instruct_i8x.ckpt","version":"qwen_image_2_1"},{"_origin":"Draw Things curated models.json","autoencoder":"qwen_image_2.1_vae_f16.ckpt","file":"qwen_image_2.1_turbo_f16.ckpt","text_encoder":"qwen_3_vl_8b_instruct_f16.ckpt","version":"qwen_image_2_1"},{"_origin":"Draw Things curated models.json","autoencoder":"qwen_image_2.1_vae_f16.ckpt","file":"qwen_image_2.1_turbo_i4x.ckpt","text_encoder":"qwen_3_vl_8b_instruct_i8x.ckpt","version":"qwen_image_2_1"},{"_origin":"Draw Things curated models.json","autoencoder":"qwen_image_2.1_vae_f16.ckpt","file":"qwen_image_2.1_turbo_i8x.ckpt","text_encoder":"qwen_3_vl_8b_instruct_i8x.ckpt","version":"qwen_image_2_1"},{"_origin":"Draw Things curated models.json","autoencoder":"qwen_image_2.1_vae_f16.ckpt","file":"qwen_image_2.1_turbo_q6p.ckpt","text_encoder":"qwen_3_vl_8b_instruct_i8x.ckpt","version":"qwen_image_2_1"},{"_origin":"Draw Things curated models.json","autoencoder":"qwen_image_2.1_vae_f16.ckpt","file":"qwen_image_2.1_turbo_q8p.ckpt","text_encoder":"qwen_3_vl_8b_instruct_i8x.ckpt","version":"qwen_image_2_1"},{"_origin":"Draw Things curated models.json","autoencoder":"qwen_image_vae_f16.ckpt","file":"qwen_image_2512_bf16.ckpt","text_encoder":"qwen_2.5_vl_7b_f16.ckpt","version":"qwen_image"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"qwen_image_vae_f16.ckpt","file":"qwen_image_2512_bf16_i8x.ckpt","text_encoder":"qwen_2.5_vl_7b_q8p.ckpt","version":"qwen_image"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"qwen_image_vae_f16.ckpt","file":"qwen_image_2512_bf16_q6p.ckpt","text_encoder":"qwen_2.5_vl_7b_q8p.ckpt","version":"qwen_image"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"qwen_image_vae_f16.ckpt","file":"qwen_image_2512_bf16_q8p.ckpt","text_encoder":"qwen_2.5_vl_7b_q8p.ckpt","version":"qwen_image"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"qwen_image_vae_f16.ckpt","file":"qwen_image_2512_i8x.ckpt","text_encoder":"qwen_2.5_vl_7b_q8p.ckpt","version":"qwen_image"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"qwen_image_vae_f16.ckpt","file":"qwen_image_2512_q6p.ckpt","text_encoder":"qwen_2.5_vl_7b_q8p.ckpt","version":"qwen_image"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"qwen_image_vae_f16.ckpt","file":"qwen_image_2512_q8p.ckpt","text_encoder":"qwen_2.5_vl_7b_q8p.ckpt","version":"qwen_image"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"qwen_image_vae_f16.ckpt","clip_encoder":"qwen_2.5_vl_7b_vit_f16.ckpt","file":"qwen_image_edit_1.0_q6p.ckpt","text_encoder":"qwen_2.5_vl_7b_q8p.ckpt","version":"qwen_image"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"qwen_image_vae_f16.ckpt","clip_encoder":"qwen_2.5_vl_7b_vit_f16.ckpt","file":"qwen_image_edit_1.0_q8p.ckpt","text_encoder":"qwen_2.5_vl_7b_q8p.ckpt","version":"qwen_image"},{"_origin":"Draw Things curated models.json","autoencoder":"qwen_image_vae_f16.ckpt","clip_encoder":"qwen_2.5_vl_7b_vit_f16.ckpt","file":"qwen_image_edit_2509_bf16.ckpt","text_encoder":"qwen_2.5_vl_7b_f16.ckpt","version":"qwen_image"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"qwen_image_vae_f16.ckpt","clip_encoder":"qwen_2.5_vl_7b_vit_f16.ckpt","file":"qwen_image_edit_2509_bf16_q6p.ckpt","text_encoder":"qwen_2.5_vl_7b_q8p.ckpt","version":"qwen_image"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"qwen_image_vae_f16.ckpt","clip_encoder":"qwen_2.5_vl_7b_vit_f16.ckpt","file":"qwen_image_edit_2509_bf16_q8p.ckpt","text_encoder":"qwen_2.5_vl_7b_q8p.ckpt","version":"qwen_image"},{"_origin":"Draw Things curated models.json","autoencoder":"qwen_image_vae_f16.ckpt","clip_encoder":"qwen_2.5_vl_7b_vit_f16.ckpt","file":"qwen_image_edit_2509_f16.ckpt","text_encoder":"qwen_2.5_vl_7b_f16.ckpt","version":"qwen_image"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"qwen_image_vae_f16.ckpt","clip_encoder":"qwen_2.5_vl_7b_vit_f16.ckpt","file":"qwen_image_edit_2509_q6p.ckpt","text_encoder":"qwen_2.5_vl_7b_q8p.ckpt","version":"qwen_image"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"qwen_image_vae_f16.ckpt","clip_encoder":"qwen_2.5_vl_7b_vit_f16.ckpt","file":"qwen_image_edit_2509_q8p.ckpt","text_encoder":"qwen_2.5_vl_7b_q8p.ckpt","version":"qwen_image"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"qwen_image_vae_f16.ckpt","clip_encoder":"qwen_2.5_vl_7b_vit_f16.ckpt","file":"qwen_image_edit_2511_bf16_i8x.ckpt","text_encoder":"qwen_2.5_vl_7b_q8p.ckpt","version":"qwen_image"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"qwen_image_vae_f16.ckpt","clip_encoder":"qwen_2.5_vl_7b_vit_f16.ckpt","file":"qwen_image_edit_2511_bf16_q6p.ckpt","text_encoder":"qwen_2.5_vl_7b_q8p.ckpt","version":"qwen_image"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"qwen_image_vae_f16.ckpt","clip_encoder":"qwen_2.5_vl_7b_vit_f16.ckpt","file":"qwen_image_edit_2511_bf16_q8p.ckpt","text_encoder":"qwen_2.5_vl_7b_q8p.ckpt","version":"qwen_image"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"qwen_image_vae_f16.ckpt","clip_encoder":"qwen_2.5_vl_7b_vit_f16.ckpt","file":"qwen_image_edit_2511_i8x.ckpt","text_encoder":"qwen_2.5_vl_7b_q8p.ckpt","version":"qwen_image"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"qwen_image_vae_f16.ckpt","clip_encoder":"qwen_2.5_vl_7b_vit_f16.ckpt","file":"qwen_image_edit_2511_q6p.ckpt","text_encoder":"qwen_2.5_vl_7b_q8p.ckpt","version":"qwen_image"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"qwen_image_vae_f16.ckpt","clip_encoder":"qwen_2.5_vl_7b_vit_f16.ckpt","file":"qwen_image_edit_2511_q8p.ckpt","text_encoder":"qwen_2.5_vl_7b_q8p.ckpt","version":"qwen_image"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"qwen_image_layered_vae_f16.ckpt","file":"qwen_image_layered_1.0_bf16_q6p.ckpt","text_encoder":"qwen_2.5_vl_7b_q8p.ckpt","version":"qwen_image"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"qwen_image_layered_vae_f16.ckpt","file":"qwen_image_layered_1.0_bf16_q8p.ckpt","text_encoder":"qwen_2.5_vl_7b_q8p.ckpt","version":"qwen_image"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"ratatoskr_animal__creature_and_furry__clip_vit_l14_f16.ckpt","file":"ratatoskr_animal__creature_and_furry__f16.ckpt","text_encoder":"ratatoskr_animal__creature_and_furry__open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"rayflux_v3.0_aio_q5p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"rayflux_v3.0_aio_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"realcartoon_xl_clip_vit_l14_f16.ckpt","file":"realcartoon_xl_f16.ckpt","text_encoder":"realcartoon_xl_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"realism_by_stable_yogi_pony_v3_vae_992946_clip_vit_l14_f16.ckpt","file":"realism_by_stable_yogi_pony_v3_vae_992946_f16.ckpt","text_encoder":"realism_by_stable_yogi_pony_v3_vae_992946_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"realism_by_stable_yogi_v4.0_fp16_1422871_clip_vit_l14_f16.ckpt","file":"realism_by_stable_yogi_v4.0_fp16_1422871_f16.ckpt","text_encoder":"realism_by_stable_yogi_v4.0_fp16_1422871_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"realism_sdxl_by_stable_yogi_clip_vit_l14_f16.ckpt","file":"realism_sdxl_by_stable_yogi_f16.ckpt","text_encoder":"realism_sdxl_by_stable_yogi_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"realistic_blend_xl_clip_vit_l14_f16.ckpt","file":"realistic_blend_xl_f16.ckpt","text_encoder":"realistic_blend_xl_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","file":"realistic_vision_v5.1_f16.ckpt","text_encoder":"realistic_vision_v5.1_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things curated models.json","file":"realistic_vision_v5.1_q6p_q8p.ckpt","text_encoder":"realistic_vision_v5.1_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things curated models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"realvisxl_v4.0_clip_vit_l14_f16.ckpt","file":"realvisxl_v4.0_f16.ckpt","text_encoder":"realvisxl_v4.0_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"realvisxl_v4.0_clip_vit_l14_f16.ckpt","file":"realvisxl_v4.0_q6p_q8p.ckpt","text_encoder":"realvisxl_v4.0_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"redcraft___cads_updated_may4_commercial___advertising_design_system_clip_vit_l14_f16.ckpt","file":"redcraft___cads_updated_may4_commercial___advertising_design_system_f16.ckpt","text_encoder":"redcraft___cads_updated_may4_commercial___advertising_design_system_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","file":"redshift_768_v_f16.ckpt","text_encoder":"redshift_768_v_open_clip_vit_h14_f16.ckpt","version":"v2"},{"_origin":"Draw Things curated models.json","file":"redshift_v1_f16.ckpt","text_encoder":"redshift_v1_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things curated models.json","file":"rev_animated_v1.22_f16.ckpt","text_encoder":"rev_animated_v1.22_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things curated models.json","file":"rev_animated_v1.22_q6p_q8p.ckpt","text_encoder":"rev_animated_v1.22_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"richyrichmixixl_clip_vit_l14_f16.ckpt","file":"richyrichmixixl_f16.ckpt","text_encoder":"richyrichmixixl_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"rillusm___realistic_il_clip_vit_l14_f16.ckpt","file":"rillusm___realistic_il_f16.ckpt","text_encoder":"rillusm___realistic_il_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","file":"samdoesart_v3_f16.ckpt","text_encoder":"samdoesart_v3_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"schnellmode_v3.3_q5p_svd.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"schnellmode_v3.3_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things curated models.json","autoencoder":"sd3_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"sd3_large_3.5_f16.ckpt","text_encoder":"open_clip_vit_bigg14_f16.ckpt","version":"sd3_large"},{"_origin":"Draw Things curated models.json","autoencoder":"sd3_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"sd3_large_3.5_q6p.ckpt","text_encoder":"open_clip_vit_bigg14_f16.ckpt","version":"sd3_large"},{"_origin":"Draw Things curated models.json","autoencoder":"sd3_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"sd3_large_3.5_q8p.ckpt","text_encoder":"open_clip_vit_bigg14_f16.ckpt","version":"sd3_large"},{"_origin":"Draw Things curated models.json","autoencoder":"sd3_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"sd3_large_turbo_3.5_q6p.ckpt","text_encoder":"open_clip_vit_bigg14_f16.ckpt","version":"sd3_large"},{"_origin":"Draw Things curated models.json","autoencoder":"sd3_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"sd3_large_turbo_3.5_q8p.ckpt","text_encoder":"open_clip_vit_bigg14_f16.ckpt","version":"sd3_large"},{"_origin":"Draw Things curated models.json","autoencoder":"sd3_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"sd3_medium_3.5_f16.ckpt","text_encoder":"open_clip_vit_bigg14_f16.ckpt","version":"sd3"},{"_origin":"Draw Things curated models.json","autoencoder":"sd3_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"sd3_medium_3.5_q8p.ckpt","text_encoder":"open_clip_vit_bigg14_f16.ckpt","version":"sd3"},{"_origin":"Draw Things curated models.json","autoencoder":"sd3_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"sd3_medium_f16.ckpt","text_encoder":"open_clip_vit_bigg14_f16.ckpt","version":"sd3"},{"_origin":"Draw Things curated models.json","autoencoder":"sd3_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"sd3_medium_q8p.ckpt","text_encoder":"open_clip_vit_bigg14_f16.ckpt","version":"sd3"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","file":"sd_v1.4_f16.ckpt","version":"v1"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","file":"sd_v1.5_f16.ckpt","version":"v1"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","file":"sd_v1.5_inpainting_f16.ckpt","version":"v1"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","file":"sd_v2.0_768_v_f16.ckpt","text_encoder":"open_clip_vit_h14_f16.ckpt","version":"v2"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","file":"sd_v2.0_depth_f16.ckpt","text_encoder":"open_clip_vit_h14_f16.ckpt","version":"v2"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","file":"sd_v2.0_f16.ckpt","text_encoder":"open_clip_vit_h14_f16.ckpt","version":"v2"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","file":"sd_v2.0_inpainting_f16.ckpt","text_encoder":"open_clip_vit_h14_f16.ckpt","version":"v2"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","file":"sd_v2.1_768_v_f16.ckpt","text_encoder":"open_clip_vit_h14_f16.ckpt","version":"v2"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","file":"sd_v2.1_f16.ckpt","text_encoder":"open_clip_vit_h14_f16.ckpt","version":"v2"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"sdxl_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"sd_xl_base_0.9_f16.ckpt","text_encoder":"open_clip_vit_bigg14_f16.ckpt","version":"sdxlbase"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"sd_xl_base_1.0_f16.ckpt","text_encoder":"open_clip_vit_bigg14_f16.ckpt","version":"sdxlbase"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"sd_xl_base_1.0_q6p_q8p.ckpt","text_encoder":"open_clip_vit_bigg14_f16.ckpt","version":"sdxlbase"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"sdxl_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"sd_xl_refiner_0.9_f16.ckpt","text_encoder":"open_clip_vit_bigg14_f16.ckpt","version":"sdxlrefiner"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"sd_xl_refiner_1.0_f16.ckpt","text_encoder":"open_clip_vit_bigg14_f16.ckpt","version":"sdxlrefiner"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"sd_xl_refiner_1.0_q6p_q8p.ckpt","text_encoder":"open_clip_vit_bigg14_f16.ckpt","version":"sdxlrefiner"},{"_origin":"Draw Things curated models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"sd_xl_turbo_f16.ckpt","text_encoder":"open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"sd_xl_turbo_q6p_q8p.ckpt","text_encoder":"open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"seedvr2_vae_f16.ckpt","file":"seedvr2_3b_i8x.ckpt","text_encoder":"seedvr2_3b_i8x.ckpt","version":"seedvr2_3b"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"seedvr2_vae_f16.ckpt","file":"seedvr2_3b_q6p.ckpt","text_encoder":"seedvr2_3b_q6p.ckpt","version":"seedvr2_3b"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"seedvr2_vae_f16.ckpt","file":"seedvr2_3b_q8p.ckpt","text_encoder":"seedvr2_3b_q8p.ckpt","version":"seedvr2_3b"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"seedvr2_vae_f16.ckpt","file":"seedvr2_7b_i8x.ckpt","text_encoder":"seedvr2_7b_i8x.ckpt","version":"seedvr2_7b"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"seedvr2_vae_f16.ckpt","file":"seedvr2_7b_q6p.ckpt","text_encoder":"seedvr2_7b_q6p.ckpt","version":"seedvr2_7b"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"seedvr2_vae_f16.ckpt","file":"seedvr2_7b_q8p.ckpt","text_encoder":"seedvr2_7b_q8p.ckpt","version":"seedvr2_7b"},{"_origin":"Draw Things curated models.json","file":"seek_art_mega_v1_f16.ckpt","text_encoder":"seek_art_mega_v1_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"shuttle_jaguar_q5p_svd.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"shuttle_jaguar_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"shuttle_v3.1_aesthetic_q5p_svd.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"shuttle_v3.1_aesthetic_q8p.ckpt","text_encoder":"t5_xxl_encoder_q6p.ckpt","version":"flux1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"silvermoonmix_illustrious_evolved_clip_vit_l14_f16.ckpt","file":"silvermoonmix_illustrious_evolved_f16.ckpt","text_encoder":"silvermoonmix_illustrious_evolved_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"singlo_illustrious_clip_vit_l14_f16.ckpt","file":"singlo_illustrious_f16.ckpt","text_encoder":"singlo_illustrious_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"skibidimix_clip_vit_l14_f16.ckpt","file":"skibidimix_f16.ckpt","text_encoder":"skibidimix_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"hunyuan_video_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"skyreels_v1_hunyuan_i2v_q5p_svd.ckpt","text_encoder":"llava_llama_3_8b_v1.1_q8p.ckpt","version":"hunyuan_video"},{"_origin":"Draw Things curated models.json","autoencoder":"hunyuan_video_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"skyreels_v1_hunyuan_i2v_q8p.ckpt","text_encoder":"llava_llama_3_8b_v1.1_q8p.ckpt","version":"hunyuan_video"},{"_origin":"Draw Things curated models.json","autoencoder":"hunyuan_video_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"skyreels_v1_hunyuan_t2v_q5p_svd.ckpt","text_encoder":"llava_llama_3_8b_v1.1_q8p.ckpt","version":"hunyuan_video"},{"_origin":"Draw Things curated models.json","autoencoder":"hunyuan_video_vae_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"skyreels_v1_hunyuan_t2v_q8p.ckpt","text_encoder":"llava_llama_3_8b_v1.1_q8p.ckpt","version":"hunyuan_video"},{"_origin":"Draw Things curated models.json","autoencoder":"wan_v2.1_video_vae_f16.ckpt","clip_encoder":"open_clip_xlm_roberta_large_vit_h14_f16.ckpt","file":"skyreels_v2_i2v_1.3b_540p_f16.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan_v2_1_1_3b"},{"_origin":"Draw Things curated models.json","autoencoder":"wan_v2.1_video_vae_f16.ckpt","clip_encoder":"open_clip_xlm_roberta_large_vit_h14_f16.ckpt","file":"skyreels_v2_i2v_1.3b_540p_q8p.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan_v2_1_1_3b"},{"_origin":"Draw Things curated models.json","autoencoder":"wan_v2.1_video_vae_f16.ckpt","clip_encoder":"open_clip_xlm_roberta_large_vit_h14_f16.ckpt","file":"skyreels_v2_i2v_14b_540p_q6p_svd.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan_v2_1_14b"},{"_origin":"Draw Things curated models.json","autoencoder":"wan_v2.1_video_vae_f16.ckpt","clip_encoder":"open_clip_xlm_roberta_large_vit_h14_f16.ckpt","file":"skyreels_v2_i2v_14b_540p_q8p.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan_v2_1_14b"},{"_origin":"Draw Things curated models.json","autoencoder":"wan_v2.1_video_vae_f16.ckpt","clip_encoder":"open_clip_xlm_roberta_large_vit_h14_f16.ckpt","file":"skyreels_v2_i2v_14b_720p_q6p_svd.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan_v2_1_14b"},{"_origin":"Draw Things curated models.json","autoencoder":"wan_v2.1_video_vae_f16.ckpt","clip_encoder":"open_clip_xlm_roberta_large_vit_h14_f16.ckpt","file":"skyreels_v2_i2v_14b_720p_q8p.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan_v2_1_14b"},{"_origin":"Draw Things curated models.json","autoencoder":"wan_v2.1_video_vae_f16.ckpt","file":"skyreels_v2_t2v_14b_540p_q6p_svd.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan_v2_1_14b"},{"_origin":"Draw Things curated models.json","autoencoder":"wan_v2.1_video_vae_f16.ckpt","file":"skyreels_v2_t2v_14b_540p_q8p.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan_v2_1_14b"},{"_origin":"Draw Things curated models.json","autoencoder":"wan_v2.1_video_vae_f16.ckpt","file":"skyreels_v2_t2v_14b_720p_q6p_svd.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan_v2_1_14b"},{"_origin":"Draw Things curated models.json","autoencoder":"wan_v2.1_video_vae_f16.ckpt","file":"skyreels_v2_t2v_14b_720p_q8p.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan_v2_1_14b"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"smooth_mix_noobai_illustrious_pony_illustrious_v.2_1401686_clip_vit_l14_f16.ckpt","file":"smooth_mix_noobai_illustrious_pony_illustrious_v.2_1401686_f16.ckpt","text_encoder":"smooth_mix_noobai_illustrious_pony_illustrious_v.2_1401686_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"smooth_mix_noobai_illustrious_pony_illustrious_v.3_1470541_clip_vit_l14_f16.ckpt","file":"smooth_mix_noobai_illustrious_pony_illustrious_v.3_1470541_f16.ckpt","text_encoder":"smooth_mix_noobai_illustrious_pony_illustrious_v.3_1470541_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"smooth_mix_noobai_illustrious_pony_noobai_v2.0_1629718_clip_vit_l14_f16.ckpt","file":"smooth_mix_noobai_illustrious_pony_noobai_v2.0_1629718_f16.ckpt","text_encoder":"smooth_mix_noobai_illustrious_pony_noobai_v2.0_1629718_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"smooth_mix_noobai_illustrious_pony_pony_v.3_997031_clip_vit_l14_f16.ckpt","file":"smooth_mix_noobai_illustrious_pony_pony_v.3_997031_f16.ckpt","text_encoder":"smooth_mix_noobai_illustrious_pony_pony_v.3_997031_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"smooth_mix_noobai_illustrious_pony_realism_1675843_clip_vit_l14_f16.ckpt","file":"smooth_mix_noobai_illustrious_pony_realism_1675843_f16.ckpt","text_encoder":"smooth_mix_noobai_illustrious_pony_realism_1675843_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"snowpony_clip_vit_l14_f16.ckpt","file":"snowpony_f16.ckpt","text_encoder":"snowpony_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","file":"spiderverse_v1_f16.ckpt","text_encoder":"spiderverse_v1_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things curated models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"ssd_1b_f16.ckpt","text_encoder":"open_clip_vit_bigg14_f16.ckpt","version":"ssd_1b"},{"_origin":"Draw Things curated models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"clip_vit_l14_f16.ckpt","file":"ssd_1b_q6p_q8p.ckpt","text_encoder":"open_clip_vit_bigg14_f16.ckpt","version":"ssd_1b"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"steinillustrious_clip_vit_l14_f16.ckpt","file":"steinillustrious_f16.ckpt","text_encoder":"steinillustrious_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","file":"supermarionation_v2_f16.ckpt","text_encoder":"supermarionation_v2_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things curated models.json","clip_encoder":"svd_i2v_1.0_f16.ckpt","file":"svd_i2v_1.0_f16.ckpt","text_encoder":"open_clip_vit_h14_vision_model_f16.ckpt","version":"svd_i2v"},{"_origin":"Draw Things curated models.json","clip_encoder":"svd_i2v_1.0_q6p_q8p.ckpt","file":"svd_i2v_1.0_q6p_q8p.ckpt","text_encoder":"open_clip_vit_h14_vision_model_f16.ckpt","version":"svd_i2v"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","clip_encoder":"svd_i2v_xt_1.0_f16.ckpt","file":"svd_i2v_xt_1.0_f16.ckpt","text_encoder":"open_clip_vit_h14_vision_model_f16.ckpt","version":"svdi2v"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","clip_encoder":"svd_i2v_xt_1.0_q6p_q8p.ckpt","file":"svd_i2v_xt_1.0_q6p_q8p.ckpt","text_encoder":"open_clip_vit_h14_vision_model_f16.ckpt","version":"svdi2v"},{"_origin":"Draw Things curated models.json","clip_encoder":"svd_i2v_xt_1.1_f16.ckpt","file":"svd_i2v_xt_1.1_f16.ckpt","text_encoder":"open_clip_vit_h14_vision_model_f16.ckpt","version":"svd_i2v"},{"_origin":"Draw Things curated models.json","clip_encoder":"svd_i2v_xt_1.1_q6p_q8p.ckpt","file":"svd_i2v_xt_1.1_q6p_q8p.ckpt","text_encoder":"open_clip_vit_h14_vision_model_f16.ckpt","version":"svd_i2v"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"throwing_pasta_mix_spaghetti_clip_vit_l14_f16.ckpt","file":"throwing_pasta_mix_spaghetti_f16.ckpt","text_encoder":"throwing_pasta_mix_spaghetti_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"toonify_toonify_illustrious_1495132_clip_vit_l14_f16.ckpt","file":"toonify_toonify_illustrious_1495132_f16.ckpt","text_encoder":"toonify_toonify_illustrious_1495132_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"toonify_toonify_ponydiffusionxl_410539_clip_vit_l14_f16.ckpt","file":"toonify_toonify_ponydiffusionxl_410539_f16.ckpt","text_encoder":"toonify_toonify_ponydiffusionxl_410539_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","file":"trnlgcy_f16.ckpt","text_encoder":"trnlgcy_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things official community uncurated_models.json","file":"tt_animerge_f16.ckpt","text_encoder":"tt_animerge_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"ultra_realistic_by_stable_yogi_pony_clip_vit_l14_f16.ckpt","file":"ultra_realistic_by_stable_yogi_pony_f16.ckpt","text_encoder":"ultra_realistic_by_stable_yogi_pony_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"umbroxponyxl_clip_vit_l14_f16.ckpt","file":"umbroxponyxl_f16.ckpt","text_encoder":"umbroxponyxl_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"uncanny_valley_clip_vit_l14_f16.ckpt","file":"uncanny_valley_f16.ckpt","text_encoder":"uncanny_valley_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"untitled_pony_clip_vit_l14_f16.ckpt","file":"untitled_pony_f16.ckpt","text_encoder":"untitled_pony_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"virtual_diffusion_pony_xl_2025_clip_vit_l14_f16.ckpt","file":"virtual_diffusion_pony_xl_2025_f16.ckpt","text_encoder":"virtual_diffusion_pony_xl_2025_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"vixon_s_dream_weave_clip_vit_l14_f16.ckpt","file":"vixons_dream_weave_f16.ckpt","text_encoder":"vixon_s_dream_weave_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","file":"voxelart_v1_f16.ckpt","text_encoder":"voxelart_v1_clip_vit_l14_f16.ckpt","version":"v1"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"wai_shuffle_noob_clip_vit_l14_f16.ckpt","file":"wai_shuffle_noob_f16.ckpt","text_encoder":"wai_shuffle_noob_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things curated models.json","autoencoder":"wan_v2.1_video_vae_f16.ckpt","clip_encoder":"open_clip_xlm_roberta_large_vit_h14_f16.ckpt","file":"wan_2.1_1.3b_fun_inp_f16.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan_v2_1_1_3b"},{"_origin":"Draw Things curated models.json","autoencoder":"wan_v2.1_video_vae_f16.ckpt","clip_encoder":"open_clip_xlm_roberta_large_vit_h14_f16.ckpt","file":"wan_2.1_1.3b_fun_inp_q8p.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan_v2_1_1_3b"},{"_origin":"Draw Things curated models.json","autoencoder":"wan_v2.1_video_vae_f16.ckpt","clip_encoder":"open_clip_xlm_roberta_large_vit_h14_f16.ckpt","file":"wan_2.1_1.3b_v1.1_fun_inp_f16.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan_v2_1_1_3b"},{"_origin":"Draw Things curated models.json","autoencoder":"wan_v2.1_video_vae_f16.ckpt","clip_encoder":"open_clip_xlm_roberta_large_vit_h14_f16.ckpt","file":"wan_2.1_1.3b_v1.1_fun_inp_q8p.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan_v2_1_1_3b"},{"_origin":"Draw Things curated models.json","autoencoder":"wan_v2.1_video_vae_f16.ckpt","clip_encoder":"open_clip_xlm_roberta_large_vit_h14_f16.ckpt","file":"wan_2.1_14b_fun_inp_q6p_svd.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan_v2_1_14b"},{"_origin":"Draw Things curated models.json","autoencoder":"wan_v2.1_video_vae_f16.ckpt","clip_encoder":"open_clip_xlm_roberta_large_vit_h14_f16.ckpt","file":"wan_2.1_14b_fun_inp_q8p.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan_v2_1_14b"},{"_origin":"Draw Things curated models.json","autoencoder":"wan_v2.1_video_vae_f16.ckpt","clip_encoder":"open_clip_xlm_roberta_large_vit_h14_f16.ckpt","file":"wan_2.1_14b_i2v_fusionx_q6p_svd.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan_v2_1_14b"},{"_origin":"Draw Things curated models.json","autoencoder":"wan_v2.1_video_vae_f16.ckpt","clip_encoder":"open_clip_xlm_roberta_large_vit_h14_f16.ckpt","file":"wan_2.1_14b_i2v_fusionx_q8p.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan_v2_1_14b"},{"_origin":"Draw Things curated models.json","autoencoder":"wan_v2.1_video_vae_f16.ckpt","file":"wan_2.1_14b_t2v_fusionx_q6p_svd.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan_v2_1_14b"},{"_origin":"Draw Things curated models.json","autoencoder":"wan_v2.1_video_vae_f16.ckpt","file":"wan_2.1_14b_t2v_fusionx_q8p.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan_v2_1_14b"},{"_origin":"Draw Things curated models.json","autoencoder":"wan_v2.1_video_vae_f16.ckpt","clip_encoder":"open_clip_xlm_roberta_large_vit_h14_f16.ckpt","file":"wan_2.1_14b_v1.1_fun_inp_q6p_svd.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan_v2_1_14b"},{"_origin":"Draw Things curated models.json","autoencoder":"wan_v2.1_video_vae_f16.ckpt","clip_encoder":"open_clip_xlm_roberta_large_vit_h14_f16.ckpt","file":"wan_2.1_14b_v1.1_fun_inp_q8p.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan_v2_1_14b"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"wan_v2.1_video_vae_f16.ckpt","file":"wan_v2.1_1.3b_480p_f16.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan21_1_3b"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"wan_v2.1_video_vae_f16.ckpt","file":"wan_v2.1_1.3b_480p_q8p.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan21_1_3b"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"wan_v2.1_video_vae_f16.ckpt","file":"wan_v2.1_14b_720p_q5p_svd.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan21_14b"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"wan_v2.1_video_vae_f16.ckpt","file":"wan_v2.1_14b_720p_q6p_svd.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan21_14b"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"wan_v2.1_video_vae_f16.ckpt","file":"wan_v2.1_14b_720p_q8p.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan21_14b"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"wan_v2.1_video_vae_f16.ckpt","clip_encoder":"open_clip_xlm_roberta_large_vit_h14_f16.ckpt","file":"wan_v2.1_14b_i2v_480p_q6p_svd.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan21_14b"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"wan_v2.1_video_vae_f16.ckpt","clip_encoder":"open_clip_xlm_roberta_large_vit_h14_f16.ckpt","file":"wan_v2.1_14b_i2v_480p_q8p.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan21_14b"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"wan_v2.1_video_vae_f16.ckpt","clip_encoder":"open_clip_xlm_roberta_large_vit_h14_f16.ckpt","file":"wan_v2.1_14b_i2v_720p_q6p_svd.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan21_14b"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"wan_v2.1_video_vae_f16.ckpt","clip_encoder":"open_clip_xlm_roberta_large_vit_h14_f16.ckpt","file":"wan_v2.1_14b_i2v_720p_q8p.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan21_14b"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"wan_v2.2_video_vae_f16.ckpt","file":"wan_v2.2_5b_ti2v_f16.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan22_5b"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"wan_v2.2_video_vae_f16.ckpt","file":"wan_v2.2_5b_ti2v_q8p.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan22_5b"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"wan_v2.1_video_vae_f16.ckpt","file":"wan_v2.2_a14b_hne_i2v_i8x.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan21_14b"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"wan_v2.1_video_vae_f16.ckpt","file":"wan_v2.2_a14b_hne_i2v_q6p_svd.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan21_14b"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"wan_v2.1_video_vae_f16.ckpt","file":"wan_v2.2_a14b_hne_i2v_q8p.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan21_14b"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"wan_v2.1_video_vae_f16.ckpt","file":"wan_v2.2_a14b_hne_t2v_i8x.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan21_14b"},{"_origin":"Draw Things curated models.json","autoencoder":"wan_v2.1_video_vae_f16.ckpt","file":"wan_v2.2_a14b_hne_t2v_lightning_250928_q6p_svd.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan_v2_1_14b"},{"_origin":"Draw Things curated models.json","autoencoder":"wan_v2.1_video_vae_f16.ckpt","file":"wan_v2.2_a14b_hne_t2v_lightning_250928_q8p.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan_v2_1_14b"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"wan_v2.1_video_vae_f16.ckpt","file":"wan_v2.2_a14b_hne_t2v_q6p_svd.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan21_14b"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"wan_v2.1_video_vae_f16.ckpt","file":"wan_v2.2_a14b_hne_t2v_q8p.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan21_14b"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"wan_v2.1_video_vae_f16.ckpt","file":"wan_v2.2_a14b_lne_i2v_i8x.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan21_14b"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"wan_v2.1_video_vae_f16.ckpt","file":"wan_v2.2_a14b_lne_i2v_q6p_svd.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan21_14b"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"wan_v2.1_video_vae_f16.ckpt","file":"wan_v2.2_a14b_lne_i2v_q8p.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan21_14b"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"wan_v2.1_video_vae_f16.ckpt","file":"wan_v2.2_a14b_lne_t2v_i8x.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan21_14b"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"wan_v2.1_video_vae_f16.ckpt","file":"wan_v2.2_a14b_lne_t2v_q6p_svd.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan21_14b"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"wan_v2.1_video_vae_f16.ckpt","file":"wan_v2.2_a14b_lne_t2v_q8p.ckpt","text_encoder":"umt5_xxl_encoder_q8p.ckpt","version":"wan21_14b"},{"_origin":"Draw Things curated models.json","file":"wd_v1.3_f16.ckpt","version":"v1"},{"_origin":"Draw Things curated models.json","autoencoder":"wurstchen_3.0_stage_a_hq_f16.ckpt","file":"wurstchen_3.0_stage_c_f32_f16.ckpt","text_encoder":"open_clip_vit_bigg14_f16.ckpt","version":"wurstchen_v3_0_stage_c"},{"_origin":"Draw Things curated models.json","autoencoder":"wurstchen_3.0_stage_a_hq_f16.ckpt","file":"wurstchen_3.0_stage_c_f32_q6p_q8p.ckpt","text_encoder":"open_clip_vit_bigg14_f16.ckpt","version":"wurstchen_v3_0_stage_c"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","file":"z_image_1.0_f16.ckpt","text_encoder":"qwen_3_vl_4b_instruct_f16.ckpt","version":"z_image"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"flux_1_vae_f16.ckpt","file":"z_image_1.0_i8x.ckpt","text_encoder":"qwen_3_vl_4b_instruct_q8p.ckpt","version":"z_image"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"flux_1_vae_f16.ckpt","file":"z_image_1.0_q6p.ckpt","text_encoder":"qwen_3_vl_4b_instruct_q8p.ckpt","version":"z_image"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"flux_1_vae_f16.ckpt","file":"z_image_1.0_q8p.ckpt","text_encoder":"qwen_3_vl_4b_instruct_q8p.ckpt","version":"z_image"},{"_origin":"Draw Things curated models.json","autoencoder":"flux_1_vae_f16.ckpt","file":"z_image_turbo_1.0_f16.ckpt","text_encoder":"qwen_3_vl_4b_instruct_f16.ckpt","version":"z_image"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"flux_1_vae_f16.ckpt","file":"z_image_turbo_1.0_i8x.ckpt","text_encoder":"qwen_3_vl_4b_instruct_q8p.ckpt","version":"z_image"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"flux_1_vae_f16.ckpt","file":"z_image_turbo_1.0_q6p.ckpt","text_encoder":"qwen_3_vl_4b_instruct_q8p.ckpt","version":"z_image"},{"_origin":"Draw Things Swift ModelZoo.builtinSpecifications","autoencoder":"flux_1_vae_f16.ckpt","file":"z_image_turbo_1.0_q8p.ckpt","text_encoder":"qwen_3_vl_4b_instruct_q8p.ckpt","version":"z_image"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"zavychromaxl_clip_vit_l14_f16.ckpt","file":"zavychromaxl_f16.ckpt","text_encoder":"zavychromaxl_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"},{"_origin":"Draw Things official community uncurated_models.json","autoencoder":"sdxl_vae_v1.0_f16.ckpt","clip_encoder":"zep9_clip_vit_l14_f16.ckpt","file":"zep9_f16.ckpt","text_encoder":"zep9_open_clip_vit_bigg14_f16.ckpt","version":"sdxl_base_v0.9"}]'''
_CT_DT_CATALOG_CONFLICTS = {}

def _ct_load_embedded_catalog():
    for item in json.loads(_CT_DT_CATALOG_ROWS_JSON):
        filename = item.get('file')
        version = item.get('version')
        if not isinstance(filename,str) or not filename.lower().endswith('.ckpt') or not isinstance(version,str):
            continue
        key = filename.casefold()
        previous = DT_BUILTIN_EXACT.get(key)
        if previous is not None:
            if _ct_version_identity(previous['version']) != _ct_version_identity(version):
                _CT_DT_CATALOG_CONFLICTS[key] = (previous['version'], version)
            # Validated records keep their known graph dependencies.
            continue
        DT_BUILTIN_EXACT[key] = item

def _ct_version_identity(value):
    value = str(value).casefold().replace('.', '_').replace('-', '_')
    aliases = {'sdxlbasev0_9':'sdxl_base_v0_9','sdxl_base_v0_9':'sdxl',
               'qwenimage':'qwen_image','zimage':'z_image','flux_1':'flux1','flux2_4b':'flux2_4b',
               'flux2klein4b':'flux2_4b','ltx2_3':'ltx2_3'}
    return aliases.get(value,value)

_ct_load_embedded_catalog()
# --- End embedded Draw Things model catalog ---

ARCH={'sdxl_base_v0.9':'sdxl','z_image':'z_image','qwen_image':'qwen_image','flux1':'flux1','flux2_4b':'flux2_4b'}

class ModelResolutionError(ValueError): pass


# Resolution is per distinct historical filename. Only filename-derived
# search strings leave the machine; no image/prompt/DB contents are transmitted.
from urllib.error import HTTPError as _CT_HTTPError, URLError as _CT_URLError
from urllib.parse import urlencode as _ct_urlencode
from urllib.request import Request as _CT_Request, urlopen as _ct_urlopen

_CT_CIVITAI_API = 'https://civitai.com/api/v1/models'
_CT_CIVITAI_TIMEOUT = 7
_CT_CIVITAI_MAX_REPLY = 8_000_000
_CT_CIVITAI_GENERIC = frozenset({'model','checkpoint','diffusion','diffusionpytorchmodel',
    'pytorchmodel','modelweights','weights','unet','transformer','final','latest','ema','fp16','bf16'})
_CT_CIVITAI_PRECISION = frozenset({'f16','f32','bf16','f8','i8x','i4x','q8p','q6p',
    'q4p','q5p','q8','int8','fp16','fp32'})
_CT_CIVITAI_EXTS = ('.safetensors','.ckpt','.pt','.pth','.bin','.gguf')


def _ct_civitai_stem(filename):
    name=str(filename).replace('\\','/').rsplit('/',1)[-1]
    for ext in _CT_CIVITAI_EXTS:
        if name.casefold().endswith(ext):
            return name[:-len(ext)]
    return name


def _ct_civitai_compact(value):
    return re.sub('[^a-z0-9]','',str(value).casefold())


def _ct_civitai_source_stem(filename):
    base=_ct_civitai_stem(filename)
    parts=re.split('[_ .-]',base)
    if len(parts)>1 and parts[-1].casefold() in _CT_CIVITAI_PRECISION:
        base=base[:-(len(parts[-1])+1)]
    return base


def _ct_civitai_file_equal(dt_name, original_file):
    requested=_ct_civitai_compact(_ct_civitai_source_stem(dt_name))
    full=_ct_civitai_compact(_ct_civitai_stem(dt_name))
    if len(requested)<7 or requested in _CT_CIVITAI_GENERIC:
        return False
    target=_ct_civitai_compact(_ct_civitai_stem(original_file))
    return bool(target and target in (requested,full))


def _ct_civitai_queries(filename):
    """Discover Civitai *model titles*, not nonexistent DT .ckpt source files.

    Descriptor words such as NSFW often occur in DT-imported names but not
    the published Civitai title. They are removed for *discovery* only; the
    final match must also corroborate the VERSION and a published FILE.
    """
    base=_ct_civitai_source_stem(filename)
    spaced=re.sub(r'(?<=[a-z])(?=[A-Z])',' ',base)
    spaced=re.sub(r'[_ .-]+',' ',spaced).strip()
    without_version=re.sub(r'\s+(?:v\d+(?:\.\d+)?|[a-z]{1,3}\d{1,2})$','',spaced,flags=re.I).strip()
    descriptor_removed=re.sub(r'(?i)nsfw',' ',without_version)
    descriptor_removed=re.sub(r'\s+',' ',descriptor_removed).strip()
    choices=[spaced,without_version,spaced.split(' ')[0] if spaced else '',
             descriptor_removed,base]
    return list(dict.fromkeys(q for q in choices if len(_ct_civitai_compact(q))>=5))[:6]


def _ct_civitai_descriptive_root(value):
    """Ignore common descriptive title qualifiers ONLY with version/file proof.

    This must NOT be used to identify a family on its own. These labels can
    differ between the original DT-imported filename, Civitai model TITLE,
    and the actual published version FILE while still referring to the same
    named release; see WAI-NSFW-illustrious v8.0.
    """
    return re.sub(r'(?:nsfw|sdxl)', '', _ct_civitai_compact(value))


def _ct_civitai_correlated_version(dt_filename, model_title, version_name, file_name):
    """Strong model-title + exact version-token + published FILE corroboration.

    Evidence need not have an *identical* original .safetensors name: Draw
    Things may have a shorter imported name, and Civitai filenames sometimes
    contain publication/asset numbers. Never infer from just a similar TITLE,
    substring, version number, or shared model family.
    """
    dt=_ct_civitai_compact(_ct_civitai_source_stem(dt_filename))
    tag=_ct_civitai_compact(version_name)
    if len(tag)<3 or not dt.endswith(tag):return False
    dt_root=dt[:-len(tag)]
    title=_ct_civitai_compact(model_title)
    file_stem=_ct_civitai_stem(file_name)
    # A third-party registry may publish a different filename for a model version.
    # Trim only an unmistakable separate *long numeric* asset suffix.
    file_stem=re.sub(r'[_\-.]\d{5,}$','',file_stem)
    file_compact=_ct_civitai_compact(file_stem)
    cleaned_dt=_ct_civitai_descriptive_root(dt_root)
    cleaned_title=_ct_civitai_descriptive_root(title)
    cleaned_file=_ct_civitai_descriptive_root(file_compact)
    if len(cleaned_dt)<7 or cleaned_dt in _CT_CIVITAI_GENERIC:return False
    # Version must be present in both DT name AND the published source file.
    if not cleaned_file.endswith(tag):return False
    if cleaned_file[:-len(tag)]!=cleaned_dt:return False
    if cleaned_title!=cleaned_dt:return False
    # For descriptor-adjusted identities, demand a long distinctive root;
    # shorter common family names could collide despite a matching version.
    if (dt_root!=title or dt!=file_compact) and len(cleaned_dt)<9:
        # Exact title remains sufficient if FILE merely has a long numeric ID.
        if dt_root!=title:return False
    return True


# Civitai matched-model/version/file corroboration

def _ct_civitai_family(base):
    """Known Civitai *version-level* baseModel labels, never DT filenames."""
    s=re.sub(r'[\s_.-]+',' ',str(base).casefold()).strip()
    if re.match(r'^z ?image(?:turbo|base| |$)',s): return 'z_image','Z-Image'
    if re.match(r'^flux ?1(?: |$)',s): return 'flux1','FLUX.1'
    if re.match(r'^flux ?2(?: |$)',s):
        if 'klein' in s and re.search(r'\b4b\b',s): return 'flux2_4b','FLUX.2 Klein 4B'
        return 'flux2','FLUX.2'
    if re.match(r'^qwen ?image(?: |$)',s): return 'qwen_image','Qwen Image'
    if s=='illustrious' or s.startswith('illustrious '): return 'sdxl_base_v0.9','SDXL (Illustrious)'
    if s=='pony' or s.startswith('pony '): return 'sdxl_base_v0.9','SDXL (Pony)'
    if s=='noobai' or s.startswith('noobai '): return 'sdxl_base_v0.9','SDXL (NoobAI)'
    if re.match(r'^sdxl(?: |$)',s): return 'sdxl_base_v0.9','SDXL'
    if re.match(r'^sd 1(?: |$)',s): return 'sd_1','Stable Diffusion 1.x'
    if re.match(r'^sd 2(?: |$)',s): return 'sd_2','Stable Diffusion 2.x'
    if re.match(r'^sd ?3(?: |$)',s): return 'sd_3','Stable Diffusion 3'
    if re.match(r'^ltx(?: |$|2)',s): return 'ltx','LTX video'
    return None, str(base)


def _ct_civitai_ssl_context():
    """Use validated root CAs without requiring SSL_CERT_FILE in the shell.

    Python.org macOS installs may lack a configured system trust store.
    Prefer an explicit user SSL_CERT_FILE if set; otherwise use certifi when
    available. Keep certificate verification enabled in every case.
    """
    if _ct_os.environ.get('SSL_CERT_FILE'):
        return _ct_ssl.create_default_context()
    try:
        import certifi
    except ImportError:
        return _ct_ssl.create_default_context()
    return _ct_ssl.create_default_context(cafile=certifi.where())


def _ct_civitai_request(url):
    """Both Civitai list search AND /models/{id} detail use this same client."""
    request=_CT_Request(url,headers={'Accept':'application/json',
        'User-Agent':'ComfyThings-v1.01 (Draw Things history metadata conversion)'})
    with _ct_urlopen(request,timeout=_CT_CIVITAI_TIMEOUT,
                     context=_ct_civitai_ssl_context()) as response:
        if 'json' not in response.headers.get('Content-Type','').casefold():
            raise ValueError('response content is not JSON')
        blob=response.read(_CT_CIVITAI_MAX_REPLY+1)
    if len(blob)>_CT_CIVITAI_MAX_REPLY:
        raise ValueError('Civitai response exceeds safety limit')
    payload=json.loads(blob)
    if not isinstance(payload,dict):
        raise ValueError('unrecognized Civitai JSON structure')
    if re.fullmatch(r'https://civitai\.com/api/v1/models/\d+',url):
        if not isinstance(payload.get('modelVersions'),list):
            raise ValueError('unrecognized Civitai model detail response')
    elif not isinstance(payload.get('items'),list):
        raise ValueError('unrecognized Civitai model search response')
    return payload


# Civitai model-detail version coverage

def _ct_civitai_candidate_title(filename, title):
    """Only fetch details for the same distinctive model title/brand.

    Title alone is NEVER accepted as classification evidence. Even after
    fetching details, matching demands version + published source file.
    """
    stem=_ct_civitai_source_stem(filename)
    m=re.search(r'[_ .-](?:v\d+(?:\.\d+)*|[a-z]{1,8}\d{1,3})$',stem,flags=re.I)
    if not m:return False
    dt_root=_ct_civitai_descriptive_root(stem[:m.start()])
    brand=_ct_civitai_descriptive_root(title)
    return len(dt_root)>=7 and dt_root not in _CT_CIVITAI_GENERIC and dt_root==brand


def _ct_civitai_lookup(filename, *, get=None, max_pages=2):
    """Civitai title discovery -> full model versions -> exact/corroborated match.

    Civitai search may expose only a subset of a model's older versions.
    Fetch /api/v1/models/{id} for a distinctively matching *model title*
    before deciding that a version is absent. Never confirm by title alone.
    """
    if get is None:get=_ct_civitai_request
    seen=set();matches=[];failures=[];truncated=False;calls=0
    candidate_ids=set()
    MAX_DETAILS=8

    def inspect_model(item):
        if not isinstance(item,dict) or item.get('type') not in ('Checkpoint',None):return
        for version in item.get('modelVersions') or []:
            if not isinstance(version,dict):continue
            base=version.get('baseModel')
            if not isinstance(base,str) or not base.strip():continue
            for f in version.get('files') or []:
                if not isinstance(f,dict):continue
                name=f.get('name')
                if not isinstance(name,str):continue
                is_exact=_ct_civitai_file_equal(filename,name)
                is_correlated=_ct_civitai_correlated_version(
                    filename,item.get('name',''),version.get('name',''),name)
                if not (is_exact or is_correlated):continue
                identity=(item.get('id'),version.get('id'),name)
                if identity in seen:continue
                seen.add(identity)
                matches.append({'model_id':item.get('id'),
                    'version_id':version.get('id'),'file':name,'base':base,
                    'title':item.get('name',''),'version':version.get('name',''),
                    'evidence':'Exact normalized published version file' if is_exact
                               else 'Corroborated Civitai model title + version + file'})

    for query in _ct_civitai_queries(filename):
        cursor=None
        for page in range(max_pages):
            params={'query':query,'types':'Checkpoint','nsfw':'true',
                    'primaryFileOnly':'false','limit':'50'}
            if cursor:params['cursor']=cursor
            url=_CT_CIVITAI_API+'?'+_ct_urlencode(params)
            calls+=1
            try:
                response=get(url)
                if not isinstance(response,dict) or not isinstance(response.get('items'),list):
                    raise ValueError('unrecognized Civitai search JSON')
            except _CT_HTTPError as err:
                failures.append('HTTP %s'%err.code)
                if err.code in (401,403,429):
                    return {'status':'error','reason':'; '.join(failures),
                            'calls':calls,'matches':matches}
                break
            except (TimeoutError,_CT_URLError,OSError,ValueError,TypeError,KeyError) as err:
                failures.append(type(err).__name__+': '+str(err)[:120])
                return {'status':'ambiguous' if matches else 'error',
                        'reason':'; '.join(failures),'calls':calls,'matches':matches}
            for item in response['items']:
                if not isinstance(item,dict) or item.get('type')!='Checkpoint':continue
                inspect_model(item)
                if _ct_civitai_candidate_title(filename,item.get('name','')):
                    mid=item.get('id')
                    if isinstance(mid,int) and not isinstance(mid,bool) and mid>0:
                        candidate_ids.add(mid)
            metadata=response.get('metadata') or {}
            next_cursor=metadata.get('nextCursor')
            if not next_cursor or next_cursor==cursor:break
            if page+1>=max_pages:
                truncated=True;break
            cursor=next_cursor

    if failures or truncated:
        return {'status':'ambiguous' if matches else 'error' if failures else 'unknown',
                'reason':('search incomplete: ' if truncated else 'API problem: ')+'; '.join(failures),
                'calls':calls,'matches':matches}
    if len(candidate_ids)>MAX_DETAILS:
        return {'status':'ambiguous','reason':'Too many possible model-title matches to verify safely',
                'calls':calls,'matches':matches}

    # List results are discovery-only. Fetch complete version catalogs for
    # candidate titles (one HTTP GET per distinct Civitai model ID).
    for mid in sorted(candidate_ids):
        calls+=1
        try:
            detail=get(_CT_CIVITAI_API+'/'+str(mid))
            if (not isinstance(detail,dict) or detail.get('id')!=mid or
                not isinstance(detail.get('modelVersions'),list) or
                detail.get('type') not in (None,'Checkpoint')):
                raise ValueError('unrecognized Civitai model detail for ID %s'%mid)
            inspect_model(detail)
        except _CT_HTTPError as err:
            failures.append('Model %s HTTP %s'%(mid,err.code))
        except (TimeoutError,_CT_URLError,OSError,ValueError,TypeError,KeyError) as err:
            failures.append('Model %s %s: %s'%(mid,type(err).__name__,str(err)[:100]))
    if failures:
        return {'status':'ambiguous' if matches else 'error',
                'reason':'Civitai model details could not be fully checked: '+'; '.join(failures),
                'calls':calls,'matches':matches}
    if not matches:
        return {'status':'unknown','reason':'No confidently corroborated Civitai version/file match',
                'calls':calls,'matches':[]}
    families={_ct_civitai_family(m['base'])[0] or 'unmapped:'+m['base'].casefold() for m in matches}
    if len(families)>1:
        return {'status':'ambiguous','reason':'Matched Civitai versions disagree on baseModel',
                'calls':calls,'matches':matches}
    first=matches[0]
    evidence=' + '.join(dict.fromkeys(m['evidence'] for m in matches))
    return {'status':'confirmed','reason':evidence,'calls':calls,'matches':matches,
            'base':first['base'],'version':_ct_civitai_family(first['base'])[0],
            'label':_ct_civitai_family(first['base'])[1]}


def _ct_civitai_graph_spec(version, base):
    """Architecture references below are *known reference DT model dependencies*,
    NOT asserted original dependencies of the converted checkpoint. Never
    substitute the original model itself; all loader filenames remain preserved.
    """
    specs={
      'sdxl_base_v0.9':{},
      'z_image':{'text_encoder':'qwen_3_vl_4b_instruct_q8p.ckpt',
                 'autoencoder':'flux_1_vae_f16.ckpt'},
      'qwen_image':{'text_encoder':'qwen_2.5_vl_7b_q8p.ckpt',
                    'autoencoder':'qwen_image_vae_f16.ckpt'},
      'flux1':{'text_encoder':'t5_xxl_encoder_q6p.ckpt',
               'clip_encoder':'clip_vit_l14_f16.ckpt','autoencoder':'flux_1_vae_f16.ckpt'},
      'flux2_4b':{'text_encoder':'qwen_3_4b_q8p.ckpt',
                  'autoencoder':'flux_2_vae_f16.ckpt'},
    }
    fields=dict(specs.get(version,{}))
    fields['version']=version
    fields['_origin']='Civitai version file + baseModel; architecture support template'
    if version in specs:
        fields['_reference_dependencies'] = bool(fields.get('text_encoder'))
    return fields


class _CTModelResolver:
    """Run-lifetime cache: exactly one resolution per distinct DT filename."""
    def __init__(self, get=None):
        self.cache={}
        self.get=get

    def resolve(self, filename):
        key=str(filename).casefold()
        if key in self.cache:return self.cache[key]
        basename=Path(str(filename).replace('\\','/')).name
        spec,origin=model_spec(basename)
        if spec is not None:
            label=_ct_dt_display_family(spec['version'])
            result={'status':'confirmed','source':'Draw Things official catalog',
                    'source_description':origin,'family_label':label,
                    'spec':spec,'reason':'Exact registered native Draw Things filename'}
        elif not basename:
            result={'status':'unknown','source':'None',
                    'family_label':'Unknown (could not identify definitively)',
                    'spec':None,'reason':'No checkpoint name in historical generation'}
        elif basename.casefold() in _CT_DT_CATALOG_CONFLICTS:
            result={'status':'ambiguous','source':'Draw Things official catalog conflict',
                    'family_label':'Unknown (could not identify definitively)',
                    'spec':None,'reason':'Conflicting official exact family records (no Civitai override)'}
        else:
            lookup=_ct_civitai_lookup(basename,get=self.get)
            if lookup['status']=='confirmed' and lookup.get('version'):
                spec=_ct_civitai_graph_spec(lookup['version'],lookup['base'])
                refs=lookup['matches']
                first=refs[0]
                source=('Civitai online model/version/file evidence: model %s version %s; '
                        'baseModel=%s; original published file=%s' % (
                            first['model_id'],first['version_id'],lookup['base'],first['file']))
                result={'status':'confirmed','source':'Civitai','source_description':source,
                        'family_label':lookup['label'],'spec':spec,'reason':source}
            elif lookup['status']=='confirmed':
                # Novel baseModel is still documented, but cannot be mapped to
                # verified ComfyUI topology. Preserve label; never guess graph.
                result={'status':'confirmed','source':'Civitai','source_description':
                        'Civitai exact published version file; unrecognized baseModel='+lookup['base'],
                        'family_label':lookup['base'],'spec':{'version':'unmapped:'+lookup['base']},
                        'reason':'Civitai version baseModel not mapped to ComfyUI graph'}
            else:
                result={'status':lookup['status'],'source':'Civitai lookup',
                        'family_label':'Unknown (could not identify definitively)',
                        'spec':None,'reason':lookup['reason']}
        self.cache[key]=result
        return result


def _ct_dt_display_family(version):
    v=str(version)
    names={'v1':'Stable Diffusion 1.x','v2':'Stable Diffusion 2.x',
           'sdxlbase':'SDXL','sdxlrefiner':'SDXL Refiner',
           'sdxl_base_v0.9':'SDXL','sd3':'Stable Diffusion 3',
           'sd3_large':'Stable Diffusion 3 Large',
           'z_image':'Z-Image','qwen_image':'Qwen Image',
           'flux1':'FLUX.1','flux2_4b':'FLUX.2 Klein 4B',
           'flux2_9b':'FLUX.2 Klein 9B','ltx2':'LTX-2','ltx2_3':'LTX-2.3',
           'wan21_14b':'Wan 2.1 14B','wan_v2_1_14b':'Wan 2.1 14B',
           'wan21_1_3b':'Wan 2.1 1.3B','wan_v2_1_1_3b':'Wan 2.1 1.3B'}
    return names.get(v,v.replace('_',' '))


def _ct_add_family_to_note(workflow, meta, resolution):
    """Leftmost consolidated Note contains the exact model and provenance."""
    original=str(meta.get('model') or '(not recorded)')
    family=resolution['family_label']
    label='confirmed' if resolution['status']=='confirmed' else resolution['status']
    header=('Original Draw Things model filename: '+original+'\n'
            'Model family: '+family+'\n'
            'Identification: '+label+' — '+resolution['source']+'\n'
            'Identification details: '+resolution['reason']+'\n'
            'Family identification is separate from whether this ComfyUI graph supports its architecture.\n'
            'Please review all reconstructed nodes/settings and select compatible installed files.\n')
    if resolution.get('spec',{}):
        if resolution['spec'].get('_reference_dependencies'):
            header+=('Compatible architecture reference encoders/VAE are template examples, '
                     'not verified historical filenames or evidence that they are installed.\n')
    first=next((n for n in workflow['nodes'] if n['type']=='Note'),None)
    if first is None:
        g=Graph()
        g.note('Model identification',header,[0,0])
        note=g.nodes[0]
        note['id']=max([n['id'] for n in workflow['nodes']],default=0)+1
        note['order']=max([n['order'] for n in workflow['nodes']],default=-1)+1
        workflow['nodes'].append(note)
        workflow['last_node_id']=note['id']
    else:
        first['widgets_values']=[header+'\n'+str(first.get('widgets_values',[''])[0])]

def model_spec(file, resolved=None):
    """Use confirmed exact DT specification first, then supplied Civitai resolution.

    The standalone exporter never downloads Draw Things registries at runtime.
    Unrecognized models are never classified by approximate filename.
    """
    key = Path(str(file).replace('\\','/')).name.casefold()
    if key in _CT_DT_CATALOG_CONFLICTS:
        return None, 'Conflicting official DT family specifications for exact name'
    entry = DT_BUILTIN_EXACT.get(key)
    if entry is not None:
        return entry, entry['_origin']
    if resolved is not None and resolved.get('source') == 'Civitai' and resolved.get('spec'):
        return resolved['spec'], resolved['source_description']
    return None, (resolved.get('reason') if resolved else
                  'No embedded exact DT family and Civitai lookup not supplied')

def generation_class(data):
    v=data.get('v2',{})
    if not isinstance(v,dict):return 'unparseable_generation_config'
    if v.get('hiresFix') or data.get('hires_fix'):return 'hires_fix_needs_verified_stages'
    if data.get('upscaler') or v.get('upscaler'):return 'upscaler_requires_source_processing_graph'
    if float(data.get('strength',1))<1:return 'image_to_image_source_not_embedded'
    if v.get('controls'):return 'control_source_images_not_embedded'
    if v.get('refinerModel'):return 'refiner_requires_separate_stage'
    if v.get('tiledDiffusion'):return 'tiled_diffusion_not_reconstructed'
    return 'basic_text_to_image'


def _float(value,what,ct,lo=-1000,hi=1000):
    return ct._as_float(value,what,lo,hi)


def _common(d,ct):
    v=d.get('v2',{})
    m=re.fullmatch(r'(\d+)x(\d+)',str(d.get('size','')))
    if not m:raise ModelResolutionError('missing or invalid original image dimensions')
    w,h=map(int,m.groups())
    if min(w,h)<16 or max(w,h)>16384:raise ModelResolutionError('invalid dimensions')
    seed=ct._as_int(d.get('seed'),'seed',0,2**64-1)
    steps=ct._as_int(d.get('steps'),'steps',1,10000)
    cfg=_float(d.get('scale'),'CFG',ct,0,1000)
    warnings=[]
    try:
        sampler,scheduler=ct.sampler_mapping(d.get('sampler',''),warnings)
    except (ConversionError, ValueError, TypeError) as exc:
        # The sampler's exact algorithm may be unavailable in stock ComfyUI.
        # Give users a connected edit-ready graph with a disclosed provisional
        # selection rather than dropping the entire generation pipeline.
        sampler,scheduler='euler','normal'
        warnings.append('Original sampler %r has no established vanilla ComfyUI equivalent; provisional Euler/normal selected. Review before running. (%s)' % (d.get('sampler'),exc))
    batch=ct._as_int(v.get('batchSize',1),'batchSize',1,4096)
    return v,w,h,seed,steps,cfg,sampler,scheduler,batch,warnings


def _node(g,kind,title,x,y,ins,outs,fields,widgets=None):
    return g.add(kind,title,[x,y],ins,outs,fields,widgets)


def _notes(g,title,body):
    g.note(title,body,[-950,750])


def _source_name(spec,key):
    x=spec.get(key)
    if not isinstance(x,str) or not x.strip():
        raise ModelResolutionError('DT source architecture specification missing '+key)
    return x  # EXACT DT name, without extension, path, or casing modification.


def _lora_chain(g,data,model,clip,family,ct,notices):
    source_loras=data.get('v2',{}).get('loras',[])
    if not isinstance(source_loras,list):raise ModelResolutionError('malformed LoRA list')
    for index,row in enumerate(source_loras,1):
        if not isinstance(row,dict) or not isinstance(row.get('file'),str) or not row.get('file'):
            notices.append('Unparseable LoRA entry %d: retained in DT provenance only'%index);continue
        name=row['file'];mode=row.get('mode')
        if mode=='refiner':
            notices.append('%s applies to refiner; not connected to base sampler'%name);continue
        if mode not in ('base','all'):
            notices.append('%s has unresolved stage mode %r; not connected'%(name,mode));continue
        strength=_float(row.get('weight'),'LoRA '+name,ct)
        # DT already validated this LoRA/model combination when it generated
        # the source image. Preserve the source LoRA name and weight exactly;
        # no external LoRA registry is read or needed.
        if family=='sdxl':
            # DT's single LoRA strength has no separate CLIP strength. ComfyUI
            # exposes separate strengths, so use same value with explicit note.
            n=_node(g,'LoraLoader','DT LoRA %d'%index,-400,index*220,
                    [('model','MODEL'),('clip','CLIP')],[('MODEL','MODEL'),('CLIP','CLIP')],
                    {'lora_name':name,'strength_model':strength,'strength_clip':strength},
                    [name,strength,strength])
            g.connect(model,'MODEL',n,'model');g.connect(clip,'CLIP',n,'clip')
            clip=n
            notices.append('%s: DT single LoRA weight copied to model and CLIP strengths; target split not specified by DT'%name)
        else:
            # Matches official Qwen Image LoRA and core model-only LoRA node;
            # individual DT LoRA file compatibility is NOT guaranteed.
            n=_node(g,'LoraLoaderModelOnly','DT LoRA %d'%index,-400,index*220,
                    [('model','MODEL')],[('MODEL','MODEL')],
                    {'lora_name':name,'strength_model':strength},[name,strength])
            g.connect(model,'MODEL',n,'model')
            notices.append('%s: connected model-only per architecture topology, target LoRA file may need manual selection'%name)
        model=n
    return model,clip


def _encode(g,clip,text,title,y):
    n=_node(g,'CLIPTextEncode',title,200,y,[('clip','CLIP')],[('CONDITIONING','CONDITIONING')],{'text':text},[text])
    g.connect(clip,'CLIP',n,'clip');return n


def _sdxl_encode(g,clip,d,positive,y,w,h):
    v=d.get('v2',{});text=str(d.get('c' if positive else 'uc',''))
    # Do not presume independently-recorded positive encoder texts also
    # describe the negative branch; apply the extra fields on positive only.
    l=(v.get('clipLText') or '') if positive and v.get('separateClipL') else text
    global_text=(v.get('openClipGText') or '') if positive and v.get('separateOpenClipG') else text
    fields={'width':w,'height':h,'crop_w':int(v.get('cropLeft') or 0),
            'crop_h':int(v.get('cropTop') or 0),'target_width':w,'target_height':h,
            'text_g':global_text,'text_l':l}
    n=_node(g,'CLIPTextEncodeSDXL','SDXL '+('Positive' if positive else 'Negative'),200,y,
            [('clip','CLIP')],[('CONDITIONING','CONDITIONING')],fields,list(fields.values()))
    g.connect(clip,'CLIP',n,'clip');return n


def _zero_negative(g,pos):
    neg=_node(g,'ConditioningZeroOut','Zero negative conditioning',200,450,
              [('conditioning','CONDITIONING')],[('CONDITIONING','CONDITIONING')],{},[])
    g.connect(pos,'CONDITIONING',neg,'conditioning');return neg


def _sample_and_save(g,m,pos,neg,vae,w,h,batch,seed,steps,cfg,sampler,scheduler,family):
    latent_type=('EmptyLatentImage' if family=='sdxl' else
                 'EmptyFlux2LatentImage' if family=='flux2_4b' else 'EmptySD3LatentImage')
    latent=_node(g,latent_type,'Source dimensions',180,660,[],[('LATENT','LATENT')],
                 {'width':w,'height':h,'batch_size':batch},[w,h,batch])
    sampler_node=_node(g,'KSampler','Source sampler',710,180,
        [('model','MODEL'),('positive','CONDITIONING'),('negative','CONDITIONING'),('latent_image','LATENT')],
        [('LATENT','LATENT')],{'seed':seed,'steps':steps,'cfg':cfg,'sampler_name':sampler,
                             'scheduler':scheduler,'denoise':1.},
        [seed,'fixed',steps,cfg,sampler,scheduler,1.])
    for src,out,dst in [(m,'MODEL','model'),(pos,'CONDITIONING','positive'),
                         (neg,'CONDITIONING','negative'),(latent,'LATENT','latent_image')]:
        g.connect(src,out,sampler_node,dst)
    dec=_node(g,'VAEDecode','Decode',1190,180,
              [('samples','LATENT'),('vae','VAE')],[('IMAGE','IMAGE')],{},[])
    g.connect(sampler_node,'LATENT',dec,'samples');g.connect(vae,'VAE',dec,'vae')
    save=_node(g,'SaveImage','Save Image',1580,180,[('images','IMAGE')],[],
               {'filename_prefix':'ComfyThings'},['ComfyThings'])
    g.connect(dec,'IMAGE',save,'images')



def _ct_flux2_sample_and_save(g, model, positive, negative, vae,
                              w,h,batch,seed,steps,cfg,sampler):
    """Stock ComfyUI FLUX.2 Klein 4B sampling topology.

    ComfyUI's published FLUX.2 Klein 4B text-to-image templates use
    CFGGuider -> SamplerCustomAdvanced with Flux2Scheduler, KSamplerSelect,
    RandomNoise and EmptyFlux2LatentImage. Keeping this topology (rather
    than generic KSampler's standard scheduler) matters for FLUX.2.
    Source sampler/schedule semantics may not exactly coincide with Draw Things.
    """
    latent=_node(g,'EmptyFlux2LatentImage','Source dimensions',170,660,[],
                 [('LATENT','LATENT')],
                 {'width':w,'height':h,'batch_size':batch},[w,h,batch])
    guider=_node(g,'CFGGuider','FLUX.2 conditioning',720,90,
                 [('model','MODEL'),('positive','CONDITIONING'),('negative','CONDITIONING')],
                 [('GUIDER','GUIDER')],{'cfg':cfg},[cfg])
    g.connect(model,'MODEL',guider,'model')
    g.connect(positive,'CONDITIONING',guider,'positive')
    g.connect(negative,'CONDITIONING',guider,'negative')
    selector=_node(g,'KSamplerSelect','Source sampler',720,300,[],
                   [('SAMPLER','SAMPLER')],{'sampler_name':sampler},[sampler])
    sched=_node(g,'Flux2Scheduler','FLUX.2 schedule',720,440,[],
                [('SIGMAS','SIGMAS')],{'steps':steps,'width':w,'height':h},
                [steps,w,h])
    noise=_node(g,'RandomNoise','Source seed',720,640,[],
                [('NOISE','NOISE')],{'noise_seed':seed},[seed,'fixed'])
    sampler_node=_node(g,'SamplerCustomAdvanced','FLUX.2 sampler',1100,180,
                 [('noise','NOISE'),('guider','GUIDER'),('sampler','SAMPLER'),
                  ('sigmas','SIGMAS'),('latent_image','LATENT')],
                 [('output','LATENT'),('denoised_output','LATENT')],{},[])
    for src,port,dest in ((noise,'NOISE','noise'),(guider,'GUIDER','guider'),
                          (selector,'SAMPLER','sampler'),(sched,'SIGMAS','sigmas'),
                          (latent,'LATENT','latent_image')):
        g.connect(src,port,sampler_node,dest)
    decoder=_node(g,'VAEDecode','Decode',1480,180,
                  [('samples','LATENT'),('vae','VAE')],[('IMAGE','IMAGE')],{},[])
    g.connect(sampler_node,'output',decoder,'samples')
    g.connect(vae,'VAE',decoder,'vae')
    save=_node(g,'SaveImage','Save Image',1870,180,
               [('images','IMAGE')],[],{'filename_prefix':'ComfyUI'},['ComfyUI'])
    g.connect(decoder,'IMAGE',save,'images')

def build(d,spec,ct):
    """Return (workflow, API, notes). No target-environment filename guessing."""
    d=dict(d);d['_family']=spec['version'];arch=ARCH.get(spec['version'])
    if not arch:raise ModelResolutionError('model architecture not supported')
    v,w,h,seed,steps,cfg,sampler,scheduler,batch,notices=_common(d,ct)
    g=ct.Graph();name=str(d['model'])
    if arch=='sdxl':
        load=_node(g,'CheckpointLoaderSimple','DT SDXL checkpoint',-830,-120,[],
                   [('MODEL','MODEL'),('CLIP','CLIP'),('VAE','VAE')],{'ckpt_name':name},[name])
        model=clip=vae=load
        if spec.get('autoencoder'):
            vae=_node(g,'VAELoader','DT registered VAE',-830,340,[],[('VAE','VAE')],
                      {'vae_name':spec['autoencoder']},[spec['autoencoder']])
        model,clip=_lora_chain(g,d,model,clip,arch,ct,notices)
        pos=_sdxl_encode(g,clip,d,True,20,w,h)
        neg=_sdxl_encode(g,clip,d,False,320,w,h)
        if spec.get('text_encoder'):notices.append('DT registers a separate SDXL text encoder; checkpoint CLIP compatibility must be selected in ComfyUI')
    else:
        text=_source_name(spec,'text_encoder');vae_name=_source_name(spec,'autoencoder')
        model=_node(g,'UNETLoader','DT diffusion model',-850,-120,[],[('MODEL','MODEL')],
                    {'unet_name':name,'weight_dtype':'default'},[name,'default'])
        if arch=='flux1':
            clip_l=_source_name(spec,'clip_encoder')
            clip=_node(g,'DualCLIPLoader','DT FLUX text encoders',-850,110,[],[('CLIP','CLIP')],
                       {'clip_name1':clip_l,'clip_name2':text,'type':'flux','device':'default'},
                       [clip_l,text,'flux','default'])
        else:
            clip=_node(g,'CLIPLoader','DT text encoder',-850,110,[],[('CLIP','CLIP')],
                       {'clip_name':text,'type':'lumina2' if arch=='z_image' else 'flux2' if arch=='flux2_4b' else 'qwen_image','device':'default'},
                       [text,'lumina2' if arch=='z_image' else 'flux2' if arch=='flux2_4b' else 'qwen_image','default'])
        vae=_node(g,'VAELoader','DT registered VAE',-850,350,[],[('VAE','VAE')],
                  {'vae_name':vae_name},[vae_name])
        model,clip=_lora_chain(g,d,model,clip,arch,ct,notices)
        if arch in ('z_image','qwen_image'):
            shift=_float(v.get('shift'),'DT shift',ct,0,100)
            flow=_node(g,'ModelSamplingAuraFlow','Flow sampling (recorded shift)',200,-180,
                       [('model','MODEL')],[('MODEL','MODEL')],{'shift':shift},[shift])
            g.connect(model,'MODEL',flow,'model');model=flow
            if v.get('resolutionDependentShift'):
                notices.append('DT resolutionDependentShift enabled; numerical equivalence to ComfyUI fixed shift is NOT established')
        pos=_encode(g,clip,str(d.get('c','')),'Positive Prompt',160)
        if arch=='flux1':
            # Official ComfyUI FLUX.1 uses FluxGuidance conditioning plus CFG=1.
            guidance=_float(v.get('guidanceEmbed'),'DT guidanceEmbed',ct,0,100)
            gf=_node(g,'FluxGuidance','DT embedded guidance',500,160,
                     [('conditioning','CONDITIONING')],[('CONDITIONING','CONDITIONING')],
                     {'guidance':guidance},[guidance])
            g.connect(pos,'CONDITIONING',gf,'conditioning');pos=gf
            if cfg!=1: notices.append('FLUX.1 KSampler CFG fixed at 1; DT CFG %.6g preserved here (not mapped directly)'%cfg)
            cfg=1.0
            if d.get('uc'):notices.append('DT negative text preserved in metadata; FLUX.1 flow uses zero-negative conditioning')
            neg=_zero_negative(g,pos)
        else:
            neg=_encode(g,clip,str(d.get('uc','')),'Negative Prompt',450) if arch=='flux2_4b' or (d.get('uc') and not v.get('zeroNegativePrompt')) else _zero_negative(g,pos)
            if v.get('zeroNegativePrompt') and d.get('uc'):notices.append('DT zeroNegativePrompt enabled; negative text saved in metadata but zero conditioning used')
    if arch=='flux2_4b':
        _ct_flux2_sample_and_save(g,model,pos,neg,vae,w,h,batch,seed,steps,cfg,sampler)
        notices.append('ComfyUI native FLUX.2 Klein 4B topology: Qwen3-4B CLIPLoader(type=flux2), EmptyFlux2LatentImage, Flux2Scheduler and SamplerCustomAdvanced. Original Draw Things schedule may differ; check before running.')
    else:
        _sample_and_save(g,model,pos,neg,vae,w,h,batch,seed,steps,cfg,sampler,scheduler,arch)
    notices.append('DT filenames retained verbatim; choose corresponding installed ComfyUI-compatible weights manually when needed')
    _notes(g,'Architecture details — '+arch.upper(),
        'ARCHITECTURE from explicit DT model registration: '+spec['version']+'\n'
        'Model: '+name+'\n'
        'DT filenames are not evidence of ComfyUI-loadable weights.\n'
        'Execution depends on compatible installed ComfyUI weights.\n'
        'Generation metadata, detailed LoRA weights and original settings preserved in source.\n'+
        '\n'.join('- '+x for x in notices))
    wf,api=g.finish()
    return wf,api,notices


def one(d,ct,resolved=None):
    filename=str(d.get('model',''))
    spec,origin=model_spec(filename,resolved)
    state=generation_class(d)
    row={'model':filename,'source_family':None if spec is None else spec.get('version'),
         'identification':origin,'generation_class':state,'status':None,'notes':[]}
    if spec is None:
        row['status']='family_unknown_or_conflicted';return row,None
    if spec.get('version') not in ARCH:
        row['status']='family_not_implemented';return row,None
    if state!='basic_text_to_image':
        row['status']='additional_source_stages';return row,None
    try:
        wf,api,notices=build(d,spec,ct)
    except (ModelResolutionError,ct.ConversionError,ValueError,TypeError) as exc:
        row['status']='graph_not_built';row['notes']=[str(exc)];return row,None
    row['status']='structural_graph_built_unverified_in_comfyui'
    row['notes']=notices
    row['node_types']=dict(sorted(Counter(n['type'] for n in wf['nodes']).items()))
    return row,(wf,api)




def _ct_information_only_workflow(meta, reason, family=None):
    """One readable, comprehensive Note; complete XMP metadata is unchanged.

    Previously the generic converter divided pretty-printed JSON into many
    fragments with repeated `PARTIAL (1/8)` headings and boilerplate. The exporter
    renders one summary + one complete, unaltered metadata JSON appendix.
    The Note is informational: no ComfyUI sampling nodes are fabricated.
    """
    model = str(meta.get('model') or 'Not recorded')
    summary = [
        'Generation information: Original settings are preserved; no generation nodes were added.',
        'Reason: ' + str(reason),
        '', 'ORIGINAL GENERATION',
        'Model: ' + model,
        'Architecture: ' + (str(family) if family else 'Not definitively identified'),
    ]
    for label, field in (('Resolution','size'), ('Seed','seed'), ('Steps','steps'),
                         ('Sampler','sampler'), ('Guidance','scale'), ('Denoising strength','strength')):
        if meta.get(field) is not None:
            summary.append(label + ': ' + str(meta[field]))
    config = meta.get('v2') or {}
    if isinstance(config, dict) and config.get('loras'):
        summary += ['', 'LORAS']
        for lora in config['loras']:
            if isinstance(lora, dict):
                summary.append('%s | weight=%s | stage=%s' % (
                    lora.get('file','Unknown'), lora.get('weight','Not recorded'),
                    lora.get('mode','Not recorded')))
    summary += ['', 'POSITIVE PROMPT', str(meta.get('c') or '(empty)'),
                '', 'NEGATIVE PROMPT', str(meta.get('uc') or '(empty)'),
                '', 'COMPLETE ORIGINAL DRAW THINGS METADATA (JSON)',
                'All fields are preserved verbatim in this appendix and in PNG XMP.',
                json.dumps(meta, indent=2, ensure_ascii=False, allow_nan=False)]
    g = Graph()
    g.note('Generation information', '\n'.join(summary), (0,0))
    return (*g.finish(), [str(reason)])


_CT_CHECK_BEFORE_RUNNING = (
    'Workflow reconstructed using available Draw Things generation information. '
    'Please review the nodes and settings for accuracy before running.'
)


def _ct_generation_record_note(meta, interpretation, reasons, extra=None):
    """One source-faithful, readable note; no categorical execution claims."""
    lines = [interpretation, 'Reconstruction details: ' + str(reasons),
             '', 'ORIGINAL GENERATION',
             'Model: ' + str(meta.get('model') or '(not recorded)')]
    for title, key in (('Resolution', 'size'), ('Seed', 'seed'), ('Steps', 'steps'),
                       ('Sampler', 'sampler'), ('Guidance/CFG', 'scale'),
                       ('Denoising strength', 'strength')):
        if meta.get(key) is not None:
            lines.append(title + ': ' + str(meta[key]))
    if extra:
        lines.extend(['', 'CONVERSION DETAILS', *extra])
    v = meta.get('v2')
    if isinstance(v, dict) and v.get('loras'):
        lines.extend(['', 'ORIGINAL LORAS'])
        for l in v['loras']:
            if isinstance(l, dict):
                lines.append('%s | weight=%s | stage=%s' % (
                    l.get('file'), l.get('weight'), l.get('mode')))
            else:
                lines.append(str(l))
    lines.extend(['', 'POSITIVE PROMPT', str(meta.get('c') or '(empty)'),
                  '', 'NEGATIVE PROMPT', str(meta.get('uc') or '(empty)'),
                  '', 'COMPLETE ORIGINAL DRAW THINGS METADATA (JSON)',
                  'Original settings are also embedded in the PNG independently:',
                  json.dumps(meta, ensure_ascii=False, indent=2, allow_nan=False)])
    return '\n'.join(lines)


def _ct_best_effort_baseline(meta, dims, reason, family=None):
    """Connected basic text-to-image pipeline, plus explicit uncertainty warning.

    The model/encoder/latent choices use authoritative architecture data when
    available. A family without a supported graph uses provisional checkpoint
    topology; no attempt is made to assert its architecture or original stages.
    """
    ct = _ct_sys.modules[__name__]
    g = Graph()
    notes = []
    d = dict(meta)
    if not re.fullmatch(r'\d+x\d+', str(d.get('size') or '')):
        d['size'] = '%dx%d' % dims
        notes.append('Original canvas dimensions were missing; used exported image dimensions.')
    try:
        v,w,h,seed,steps,cfg,sampler,scheduler,batch,warnings=_common(d,ct)
    except (ConversionError, ModelResolutionError, ValueError, TypeError, OverflowError) as exc:
        # Better to keep the original information than construct corrupt fields.
        g.note('Generation information', _ct_generation_record_note(meta,
               'Only the available generation details were recovered.',
               str(exc)), [35, 80])
        wf,api=g.finish()
        return wf,api,[str(reason),str(exc)]
    notes.extend(warnings)
    name=str(d.get('model') or '')
    ckpt=_node(g,'CheckpointLoaderSimple','Provisional checkpoint',40,60,[],
               [('MODEL','MODEL'),('CLIP','CLIP'),('VAE','VAE')],
               {'ckpt_name':name},[name])
    model=clip=vae=ckpt
    if family:
        notes.append('Model family %s is identified, but no architecture-specific native ComfyUI graph is implemented for it; provisional checkpoint topology used, review loaders and compatible weights.' % family)
    else:
        notes.append('Provisional checkpoint-based ComfyUI topology used because the Draw Things model family could not be identified definitively; check the model architecture and change the loaders if necessary.')
    v=d.get('v2') if isinstance(d.get('v2'),dict) else {}
    if v.get('loras'):
        try:
            model,clip=_lora_chain(g,d,model,clip,'sdxl',ct,notes)
        except (ValueError,TypeError,ConversionError) as exc:
            notes.append('LoRA settings preserved in the metadata; could not construct all loaders: '+str(exc))
    pos=_encode(g,clip,str(d.get('c') or ''),'Positive Prompt',120)
    neg=_encode(g,clip,str(d.get('uc') or ''),'Negative Prompt',350)
    _sample_and_save(g,model,pos,neg,vae,w,h,batch,seed,steps,cfg,sampler,scheduler,'sdxl')
    g.note('Generation information', _ct_generation_record_note(
        meta, 'Best-effort ComfyUI workflow reconstructed from source values.',
        reason, notes), [35,900])
    wf,api=g.finish()
    return wf,api,[str(reason), *notes]


def _ct_best_effort_known(meta, dims, reason, spec):
    """Use the architecture's connected native template even if advanced
    second-stage operations cannot be replicated from source materials."""
    ct = _ct_sys.modules[__name__]
    try:
        wf,api,warnings=build(meta,spec,ct)
    except (ConversionError, ModelResolutionError, ValueError, TypeError, OverflowError) as exc:
        # Do not silently substitute an SD checkpoint for a known FLUX/Qwen/etc.
        g=Graph()
        g.note('Generation information', _ct_generation_record_note(
            meta,'Available Draw Things parameters recovered.',
            '%s; %s' % (reason, exc)), [35,80])
        wf,api=g.finish()
        return wf,api,[str(reason),str(exc)]
    notice='A baseline text-to-image path was reconstructed; source-image, upscale, multistage or control stages may require further manual adaptation.'
    notes=[notice, reason, *warnings]
    for n in wf['nodes']:
        if n['type']=='Note':
            n['widgets_values']=[_ct_generation_record_note(
                meta,'Best-effort architecture-aware reconstruction.',reason,notes)]
    return wf,api,notes


def _ct_profile_operations(meta):
    """Observed Draw Things processing operations; a config toggle alone
    does not prove that an optional stage actually ran.
    """
    profile = meta.get('profile')
    if not isinstance(profile, dict):
        return None
    timings = profile.get('timings')
    if not isinstance(timings, list):
        return None
    return {row.get('name') for row in timings if isinstance(row, dict)
            and isinstance(row.get('name'), str)}


def _ct_replace_input_source(g, dest, socket, src, output):
    """Replace a connected native-node input without renumbering Comfy links.

    Retaining the link ID ensures canvas and API graphs remain identical, and
    untouched architecture nodes keep their original IDs and connections.
    """
    target = g.nodes[dest - 1]
    ports = [i for i, port in enumerate(target['inputs']) if port['name'] == socket]
    if len(ports) != 1:
        raise ModelResolutionError('missing target port ' + socket)
    ti = ports[0]
    old_link_id = target['inputs'][ti]['link']
    if old_link_id is None:
        raise ModelResolutionError('cannot substitute unconnected target port ' + socket)
    link = g.links[old_link_id - 1]
    if link[0] != old_link_id or link[3] != dest or link[4] != ti:
        raise ModelResolutionError('graph link table is inconsistent')
    old_source_id, old_out_idx, typ = link[1], link[2], link[5]
    source_ports = [i for i, port in enumerate(g.nodes[src - 1]['outputs']) if port['name'] == output]
    if len(source_ports) != 1:
        raise ModelResolutionError('missing source output ' + output)
    oi = source_ports[0]
    if g.nodes[src - 1]['outputs'][oi]['type'] != typ:
        raise ModelResolutionError('source output type does not match target')
    old_out = g.nodes[old_source_id - 1]['outputs'][old_out_idx]
    old_out['links'].remove(old_link_id)
    if not old_out['links']:
        old_out['links'] = None
    new_out = g.nodes[src - 1]['outputs'][oi]
    new_out['links'] = (new_out['links'] or []) + [old_link_id]
    link[1], link[2] = src, oi
    g.api[str(dest)]['inputs'][socket] = [str(src), oi]


def _ct_role(g, node_id, role):
    # Cosmetic layout identifier; never changes the node type or API prompt.
    g.nodes[node_id - 1]['properties']['comfythings_stage'] = role
    return node_id


def _ct_add_multistage(wf, api, meta, dims):
    """Best-effort, source-evidenced Hires Fix and model-upscaling stages.

    Keep ComfyUI-native connected nodes, all source filenames unchanged, and
    preserve the unaltered original generation record independently. Hires
    second-pass is enabled only when both first-pass metadata and the recorded
    processing history support it; no `stage2Steps` guesses are made.
    """
    v = meta.get('v2') if isinstance(meta.get('v2'), dict) else {}
    events = _ct_profile_operations(meta)
    base_match = re.fullmatch(r'(\d+)x(\d+)',str(meta.get('size') or ''))
    if not base_match:
        return wf, api, []
    base_w, base_h = (int(x) for x in base_match.groups())
    output_w, output_h = dims
    g = Graph()
    g.nodes, g.links, g.api = wf['nodes'], wf['links'], api
    notices = []
    samplers = [n for n in g.nodes if n['type'] == 'KSampler']
    decoders = [n for n in g.nodes if n['type'] == 'VAEDecode']
    savers = [n for n in g.nodes if n['type'] == 'SaveImage']
    if len(savers) != 1 or len(decoders) != 1:
        return wf, api, notices
    save_id = savers[0]['id']
    final_decode = decoders[0]
    decode_id = final_decode['id']
    # Find the VAE loader connected to the actual decoder, not an unused VAE.
    vae_link = next((l for l in g.links if l[3] == decode_id and
                     final_decode['inputs'][l[4]]['name'] == 'vae'),None)
    vae_id = vae_link[1] if vae_link else None
    vae_out = g.nodes[vae_id - 1]['outputs'][vae_link[2]]['name'] if vae_link else None

    hires_enabled = v.get('hiresFix') is True or (v.get('hiresFix') is None and meta.get('hires_fix') is True)
    second_pass_recorded = bool(events is not None and {'second_pass_sampling','second_pass_image_encoded'} <= events)
    if hires_enabled:
        if events is not None and not second_pass_recorded:
            notices.append('Hires Fix was enabled in settings, but the recorded processing profile does not contain a second sampling pass; no extra sampler was invented.')
        elif not samplers or len(samplers)!=1 or vae_id is None:
            notices.append('Hires Fix settings preserved, but no standard first sampler and VAE were available to extend.')
        else:
            try:
                first_w = int(v.get('hiresFixWidth'))
                first_h = int(v.get('hiresFixHeight'))
                strength = _as_float(v.get('hiresFixStrength',meta.get('second_stage_strength')),
                                     'Hires Fix second-pass strength', 0,1)
                if not (first_w >= 16 and first_h >= 16 and first_w <= base_w and first_h <= base_h
                        and all(n % 8 == 0 for n in (first_w,first_h,base_w,base_h))
                        and strength > 0):
                    raise ValueError('invalid first-pass dimensions or second-pass strength')
                first_sampler = samplers[0]
                first_id = first_sampler['id']
                first_latent_link = next(l for l in g.links if l[3] == first_id
                    and first_sampler['inputs'][l[4]]['name'] == 'latent_image')
                first_latent_id = first_latent_link[1]
                first_latent = g.nodes[first_latent_id - 1]
                if first_latent['type'] not in ('EmptyLatentImage','EmptySD3LatentImage'):
                    raise ValueError('source latent type unavailable for Hires Fix')
                first_latent['widgets_values'][:2] = [first_w,first_h]
                g.api[str(first_latent_id)]['inputs'].update(width=first_w,height=first_h)

                # Read the validated base sampler's existing model/conditioning.
                sampler_sources={}
                for l in g.links:
                    if l[3]==first_id:
                        key=first_sampler['inputs'][l[4]]['name']
                        sampler_sources[key]=(l[1],g.nodes[l[1]-1]['outputs'][l[2]]['name'])
                for name in ('model','positive','negative'):
                    if name not in sampler_sources:
                        raise ValueError('first-pass sampler missing '+name)
                first_dec = _ct_role(g,g.add('VAEDecode','First-pass decode',[3000,60],
                    [('samples','LATENT'),('vae','VAE')],[('IMAGE','IMAGE')],{},[]),'first_decode')
                g.connect(first_id,'LATENT',first_dec,'samples')
                g.connect(vae_id,vae_out,first_dec,'vae')
                scale = _ct_role(g,g.add('ImageScale','Resize for second pass',[3400,60],
                    [('image','IMAGE')],[('IMAGE','IMAGE')],
                    {'upscale_method':'bilinear','width':base_w,'height':base_h,'crop':'center'},
                    ['bilinear',base_w,base_h,'center']),'hires_resize')
                g.connect(first_dec,'IMAGE',scale,'image')
                enc = _ct_role(g,g.add('VAEEncode','Encode second pass',[3800,60],
                    [('pixels','IMAGE'),('vae','VAE')],[('LATENT','LATENT')],{},[]),'hires_encode')
                g.connect(scale,'IMAGE',enc,'pixels')
                g.connect(vae_id,vae_out,enc,'vae')
                original=g.api[str(first_id)]['inputs']
                sd={k:original[k] for k in ('seed','steps','cfg','sampler_name','scheduler')}
                extra = dict(sd,denoise=strength)
                second = _ct_role(g,g.add('KSampler','Second sampling pass',[4200,60],
                    [('model','MODEL'),('positive','CONDITIONING'),('negative','CONDITIONING'),('latent_image','LATENT')],
                    [('LATENT','LATENT')],extra,
                    [sd['seed'],'fixed',sd['steps'],sd['cfg'],sd['sampler_name'],sd['scheduler'],strength]),'hires_second_sampler')
                for key in ('model','positive','negative'):
                    src,op=sampler_sources[key]
                    g.connect(src,op,second,key)
                g.connect(enc,'LATENT',second,'latent_image')
                _ct_replace_input_source(g,decode_id,'samples',second,'LATENT')
                final_decode['properties']['comfythings_stage']='final_decode'
                notices.append('Connected Hires Fix: first pass %dx%d, resize to %dx%d, VAE re-encode, second sampling denoise %.6g. Second-pass sampler/steps reused provisionally from first pass; stage2Steps was not assumed to mean Hires steps.'%(first_w,first_h,base_w,base_h,strength))
                if events is None:
                    notices.append('Hires Fix processing profile unavailable; two-pass path is inferred from the enabled configuration.')
            except (ValueError,TypeError,OverflowError,ConversionError,ModelResolutionError,StopIteration) as exc:
                # Note: shape changes should not be made on an invalid graph.
                # Inputs are prevalidated above for known, supported histories.
                notices.append('Hires Fix settings retained; two-pass construction needs review: '+str(exc))

    upscaler = v.get('upscaler') or meta.get('upscaler')
    if isinstance(upscaler,str) and upscaler.strip():
        if events is not None and 'image_upscaled' not in events:
            notices.append('An upscaler was configured, but the processing profile does not record an upscaling operation; did not add an unobserved stage.')
        else:
            # Distinct Draw Things .ckpt filenames are not necessarily
            # ComfyUI loadable: preserve the exact source file reference.
            loader = _ct_role(g,g.add('UpscaleModelLoader','Source upscale model',[3000,480],[],
                 [('UPSCALE_MODEL','UPSCALE_MODEL')],{'model_name':upscaler},[upscaler]),'upscaler_loader')
            scale_model = _ct_role(g,g.add('ImageUpscaleWithModel','Source upscaler',[3400,480],
                 [('upscale_model','UPSCALE_MODEL'),('image','IMAGE')],[('IMAGE','IMAGE')],{},[]),'upscale_model')
            g.connect(loader,'UPSCALE_MODEL',scale_model,'upscale_model')
            g.connect(decode_id,'IMAGE',scale_model,'image')
            final_image = scale_model
            if output_w > 0 and output_h > 0:
                exact = _ct_role(g,g.add('ImageScale','Exported image dimensions',[3800,480],
                    [('image','IMAGE')],[('IMAGE','IMAGE')],
                    {'upscale_method':'lanczos','width':output_w,'height':output_h,'crop':'center'},
                    ['lanczos',output_w,output_h,'center']),'post_upscale_resize')
                g.connect(scale_model,'IMAGE',exact,'image')
                final_image = exact
            _ct_replace_input_source(g,save_id,'images',final_image,'IMAGE')
            factor=v.get('upscalerScaleFactor')
            notices.append('Connected post-generation model upscale: original DT model %s; final exported pixels %dx%d (source canvas %dx%d). UpscaleModelLoader expects a compatible ComfyUI upscale model; select it manually. Reported source scale factor: %s.'%(
                upscaler,output_w,output_h,base_w,base_h,str(factor)))
            if events is None:
                notices.append('No detailed processing profile; upscaler stage inferred from source settings.')
    if 'face_restored' in (events or set()) or v.get('faceRestoration'):
        notices.append('Source face-restoration setting/stage remains in metadata; vanilla ComfyUI has no equivalent built-in restoration model node. Face-restoration operation was not invented.')
    if v.get('tiledDiffusion'):
        notices.append('Source tiled diffusion settings remain in metadata; tiled diffusion is not modeled by these standard ComfyUI nodes.')
    if notices:
        if not any(n['type']=='Note' for n in g.nodes):
            g.note('Generation information','Original Draw Things generation settings retained.',[20,1000])
        for n in g.nodes:
            if n['type']=='Note':
                n['widgets_values']=[str(n['widgets_values'][0]) + '\n\nMULTISTAGE CONVERSION\n' + '\n'.join('- '+v for v in notices)]
                break
    wf['last_node_id']=len(g.nodes)
    wf['last_link_id']=len(g.links)
    validate_graph(wf,api)
    return wf,api,notices


def _ct_architecture_graph(meta, dims, resolved=None):
    """Connected, architecture-aware best-effort graph, with source-evidenced stages."""
    ct=_ct_sys.modules[__name__]
    try:
        item,graph=one(meta,ct,resolved)
    except Exception as exc:
        item={'status':'generation_settings_need_review','identification':str(exc),
              'source_family':None,'notes':[]}
        graph=None
    family=item.get('source_family')
    if graph is not None:
        wf,api=graph
        notes=item.get('notes',[])
        status='architecture_graph'
    else:
        reasons='; '.join(filter(None,[str(item.get('status') or ''),
                 str(item.get('identification') or ''),
                 *(str(v) for v in item.get('notes',[]))]))
        spec,_=model_spec(str(meta.get('model') or ''),resolved)
        if spec is not None and spec.get('version') in ARCH:
            wf,api,notes=_ct_best_effort_known(meta,dims,reasons,spec)
            status='architecture_graph_best_effort'
        else:
            wf,api,notes=_ct_best_effort_baseline(meta,dims,reasons,family)
            status='best_effort_checkpoint_baseline'
    wf,api,stage_notes=_ct_add_multistage(wf,api,meta,dims)
    if stage_notes:
        status += '_multistage'
    return wf,api,[*notes,*stage_notes],status,family

# --- ComfyUI workflow presentation and provenance ---
# Post-processing affects canvas metadata and SaveImage's default basename only.
# It deliberately leaves sampling graphs, model names, links, steps, and tensor
# processing untouched. The original Draw Things source history/XMP is unchanged.
_CT_WORKFLOW_ATTRIBUTION = 'This workflow was converted from Draw Things by ComfyThings'
_CT_WORKFLOW_LAYOUT_REVISION = 'ComfyThings-v1.01-model-family-resolver'


def _ct_text_wrapped_rows(text, available_chars):
    """Estimate visible lines for a multi-line prompt in a ComfyUI text widget."""
    raw = str(text or '').split('\n')
    return max(1, sum(max(1, math.ceil(len(line) / available_chars)) for line in raw))


def _ct_fit_prompt_node(node):
    """Display up to 16 lines for long prompts, not 16 blank rows for short ones."""
    kind = node['type']
    if kind not in ('CLIPTextEncode', 'CLIPTextEncodeSDXL'):
        return
    vals = node.get('widgets_values_named', {})
    if not vals:
        names = ('text_g', 'text_l') if kind == 'CLIPTextEncodeSDXL' else ('text',)
        fields = (node.get('widgets_values') or [])
        if kind == 'CLIPTextEncodeSDXL' and len(fields) >= 2:
            vals = dict(zip(names, fields[-2:]))
        elif fields:
            vals = {'text': fields[0]}
    if kind == 'CLIPTextEncode':
        # A text field uses roughly 50-55 characters per row at a 410px width.
        node['size'] = [425, 110 + 20 * min(16, max(2, _ct_text_wrapped_rows(vals.get('text', ''), 54)))]
    else:
        # The SDXL node has a larger controls area and two separate text fields.
        # Count visible rows of both while allowing each to remain compact.
        rows_g = min(16, max(2, _ct_text_wrapped_rows(vals.get('text_g', ''), 54)))
        rows_l = min(16, max(2, _ct_text_wrapped_rows(vals.get('text_l', ''), 54)))
        node['size'] = [440, 255 + 19 * (rows_g + rows_l)]


def _ct_one_workflow_note(workflow):
    """Keep ONE provenance panel to the LEFT of all functional ComfyUI nodes.

    The panel starts level with the source/model nodes. It deliberately does not
    occupy a 'last stage' position or appear below the generated image path.
    This is display-only; there are no Note links or sampling consequences.
    """
    existing = [n for n in workflow['nodes'] if n['type'] == 'Note']
    ordinary = [n for n in workflow['nodes'] if n['type'] != 'Note']
    lines = [_CT_WORKFLOW_ATTRIBUTION]
    if ordinary or any(
        str((n.get('widgets_values') or [''])[0]).startswith('Only the available generation details')
        for n in existing
    ):
        lines.append(_CT_CHECK_BEFORE_RUNNING)
    for n in existing:
        value = n.get('widgets_values', [''])
        value = str(value[0]) if value else ''
        if value.startswith(_CT_WORKFLOW_ATTRIBUTION):
            value = value[len(_CT_WORKFLOW_ATTRIBUTION):].lstrip('\n')
        heading = str(n.get('title') or n['type']).strip()
        if value:
            lines += ['', heading, value]
    body = '\n'.join(lines).rstrip()
    wrapped_rows = _ct_text_wrapped_rows(body, 84)
    height = min(900, max(175, 95 + 16 * wrapped_rows))
    width = 660 if not ordinary else 485
    note_x = 40
    note_y = 60
    if ordinary:
        left = min(float(n['pos'][0]) for n in ordinary)
        top = min(float(n['pos'][1]) for n in ordinary)
        # The entire graph stays to the RIGHT of the informational panel,
        # including when an architecture builder supplies negative positions.
        # Avoid a second, redundant 500px shift when the semantic layout
        # already reserved a lane for the panel.
        dx = max(0, int(note_x + width + 100 - left))
        if dx:
            for n in ordinary:
                n['pos'][0] += dx
        note_y = max(40, int(top))
    nid = max([n['id'] for n in ordinary], default=0) + 1
    note = {
        'id': nid, 'type': 'Note', 'title': 'ComfyThings — Conversion Information',
        'pos': [note_x, note_y], 'size': [width, height],
        'flags': {}, 'order': max([n['order'] for n in ordinary], default=-1) + 1,
        'mode': 0, 'inputs': [], 'outputs': [],
        'properties': {'Node name for S&R': 'Note'},
        'widgets_values': [body],
    }
    assert not any(n['id'] in (ln[1], ln[3]) for n in existing for ln in workflow['links'])
    workflow['nodes'] = ordinary + [note]
    workflow['last_node_id'] = max(n['id'] for n in workflow['nodes'])


def _ct_layout_architecture(workflow):
    """Readable left-to-right stages, not snake-folding based on graph depth.

    Primary design rules:
      - provenance Note in a dedicated far-left lane (placed after this pass);
      - first row is MODEL -> LoRA -> PROMPTS/CONDITIONING -> SAMPLER;
      - Hires Fix, if present, occupies its OWN left-to-right second row;
      - postprocessing (decode / upscale / resize / Save Image) occupies a
        final left-to-right row, NEVER reversed;
      - Save Image is rightmost across ALL functional nodes;
      - multi-source/auxiliary nodes sit beside the stage they feed.

    Nodes are arranged by their semantic function, *not* arbitrarily wrapped
    after a fixed rank. We accept moderate width for legibility and short
    connections. This operation changes ONLY node positions.
    """
    nodes = [n for n in workflow['nodes'] if n['type'] != 'Note']
    if not nodes:
        return
    staged = any(n.get('properties', {}).get('comfythings_stage') for n in nodes)
    if staged:
        ids = set()
        def select(predicate):
            out = [n for n in nodes if n['id'] not in ids and predicate(n)]
            ids.update(n['id'] for n in out)
            return out
        role = lambda n: n.get('properties', {}).get('comfythings_stage')
        # Explicit stage tags have priority; source/decoder distinction
        # matters for a real two-pass Hires Fix pipeline.
        first_decode = select(lambda n: role(n) == 'first_decode')
        hires_resize = select(lambda n: role(n) == 'hires_resize')
        hires_encode = select(lambda n: role(n) == 'hires_encode')
        second_sample = select(lambda n: role(n) == 'hires_second_sampler')
        final_decode = select(lambda n: role(n) == 'final_decode' or n['type'] == 'VAEDecode')
        upscale = select(lambda n: role(n) == 'upscale_model')
        upscale_loader = select(lambda n: role(n) == 'upscaler_loader')
        post_resize = select(lambda n: role(n) == 'post_upscale_resize')
        saves = select(lambda n: n['type'] == 'SaveImage')

        models = select(lambda n: n['type'] in (
            'CheckpointLoaderSimple','UNETLoader','CLIPLoader','DualCLIPLoader',
            'VAELoader', 'ModelSamplingAuraFlow'))
        loras = select(lambda n: n['type'] in ('LoraLoader', 'LoraLoaderModelOnly'))
        prompts = select(lambda n: n['type'] in (
            'CLIPTextEncode','CLIPTextEncodeSDXL','ConditioningZeroOut',
            'FluxGuidance','EmptyLatentImage','EmptySD3LatentImage',
            'EmptyFlux2LatentImage'))
        sampler_setup = select(lambda n: n['type'] in (
            'KSamplerSelect','Flux2Scheduler','RandomNoise','CFGGuider'))
        samplers = select(lambda n: n['type'] in ('KSampler','SamplerCustomAdvanced'))
        # Future supported node types do not disappear or retain arbitrary
        # old positions: put them with the prompts, pending specialized layout.
        unclassified = select(lambda n: True)
        prompts.extend(unclassified)

        # Semantic, left-to-right lanes rather than depth-based wrapping.
        # Each lane may use *vertical* substeps, such as successive LoRAs or
        # Hires resize->encode. This keeps connected processing stages close.
        def width(group):
            return max((float(n['size'][0]) for n in group), default=280.)
        def height(group):
            return sum(float(n['size'][1]) for n in group) + 58 * max(0,len(group)-1)
        def column_x(groups, origin):
            result=[float(origin)]
            for group in groups[:-1]:
                result.append(result[-1]+width(group)+84)
            return result
        def draw(groups, xs, y, *, y_offsets=None):
            for col, group in enumerate(groups):
                offset=(y_offsets or {}).get(col,0)
                for n in group:
                    n['pos']=[int(xs[col]),int(y+offset)]
                    offset += float(n['size'][1])+58

        gen=[models,loras,prompts,sampler_setup+samplers]
        gx=column_x(gen,630)
        draw(gen,gx,60)
        base_bottom=60+max(height(group) for group in gen)

        hires_present=bool(first_decode or hires_resize or hires_encode or second_sample)
        if hires_present:
            # First decode -> resize and encode (two vertically arranged
            # steps) -> second sampling. The end of the previous sampler is
            # nearby; no 1500px backwards link across the whole first row.
            hires=[first_decode,hires_resize+hires_encode,second_sample]
            hx=column_x(hires,max(gx[-1]-200,gx[0]+200))
            hy=base_bottom+155
            draw(hires,hx,hy)
            output_y=hy+max(height(group) for group in hires)+155
            output_x=hx[-1]-200
        else:
            output_y=max(60,base_bottom-145)
            output_x=gx[-1]-115

        if upscale or upscale_loader or post_resize:
            # A single postprocessing cluster: the upscale model loader
            # directly ABOVE its consumer, output resize BELOW the consumer.
            # All three operations share one column rather than consuming
            # three screen-wide columns. Save Image remains one step RIGHT.
            out=[final_decode,upscale_loader+upscale+post_resize,saves]
            ox=column_x(out,output_x)
            draw(out,ox,output_y)
            # Align final SaveImage with its immediate producer, not the
            # external upscale-model loader above it.
            if saves:
                final_producer=post_resize[-1] if post_resize else (upscale[-1] if upscale else None)
                if final_producer:
                    for save in saves:
                        save['pos'][1]=int(final_producer['pos'][1])
        else:
            out=[final_decode,saves]
            ox=column_x(out,output_x)
            draw(out,ox,output_y)
        # A universal physical-right rule. The Save panel is a destination,
        # not a source, regardless of how much content precedes it.
        if saves:
            max_right=max((float(n['pos'][0])+float(n['size'][0]) for n in nodes
                           if n['type']!='SaveImage'),default=0)
            for save in saves:
                if float(save['pos'][0]) <= max_right:
                    save['pos'][0]=int(max_right+85)
                max_right=float(save['pos'][0])+float(save['size'][0])
        return

    # Standard (non-multistage) workflows keep the previously GPU-validated
    # family-specific arrangement, with a universal far-right Save Image rule.
    if not any(n['type'] in ('CheckpointLoaderSimple','UNETLoader', 'DualCLIPLoader',
                           'ModelSamplingAuraFlow', 'CLIPTextEncodeSDXL') for n in nodes):
        # For unrecognized graphs only position the final output as needed;
        # never obscure or overlap existing elements.
        pass
    else:
        bytype = {}
        for n in nodes:
            bytype.setdefault(n['type'], []).append(n)
        loaders = [n for n in nodes if n['type'] in (
            'CheckpointLoaderSimple','UNETLoader','CLIPLoader','DualCLIPLoader','VAELoader')]
        loras = [n for n in nodes if n['type'] in ('LoraLoader','LoraLoaderModelOnly')]
        flow = bytype.get('ModelSamplingAuraFlow', [])
        prompts = [n for n in nodes if n['type'] in ('CLIPTextEncode','CLIPTextEncodeSDXL')]
        guidance = bytype.get('FluxGuidance', []) + bytype.get('CFGGuider', [])
        negative_zero = bytype.get('ConditioningZeroOut', [])
        latent = [n for n in nodes if n['type'] in (
            'EmptyLatentImage','EmptySD3LatentImage','EmptyFlux2LatentImage')]
        sampler_setup = (bytype.get('KSamplerSelect', []) +
                         bytype.get('Flux2Scheduler', []) + bytype.get('RandomNoise', []))
        sampler = bytype.get('KSampler', []) + bytype.get('SamplerCustomAdvanced', [])
        decoders = bytype.get('VAEDecode', [])
        saves = bytype.get('SaveImage', [])
        known = {n['id'] for group in (loaders,loras,flow,prompts,guidance,
                 negative_zero,latent,sampler_setup,sampler,decoders,saves) for n in group}
        if len(known) == len(nodes):
            columns = [loaders]
            if loras: columns.append(loras)
            columns.append(flow + prompts + latent)
            if guidance or negative_zero: columns.append(guidance + negative_zero)
            if sampler_setup: columns.append(sampler_setup)
            columns.extend([sampler,decoders,saves])
            columns = [col for col in columns if col]
            x = 40
            for column in columns:
                y = 60
                for n in column:
                    n['pos'] = [x, y]
                    y += n['size'][1] + 65
                x += max(n['size'][0] for n in column) + 75

    # Universal invariant for all workflows: Save Image to the right of
    # every functional node. Keep it where it already is when correct.
    saves = [n for n in nodes if n['type'] == 'SaveImage']
    if saves:
        others = [n for n in nodes if n['type'] != 'SaveImage']
        max_right = max((float(n['pos'][0]) + float(n['size'][0]) for n in others),
                        default=0)
        next_x = max_right + 85
        for n in saves:
            if float(n['pos'][0]) < next_x:
                n['pos'][0] = int(next_x)
            next_x = float(n['pos'][0]) + float(n['size'][0]) + 85


def _ct_polish_workflow(workflow, api, *, architecture_aware=False):
    """Polish supported workflow presentation without changing sampling."""
    for n in workflow['nodes']:
        if n['type'] != 'Note':
            # Do not apply custom explanatory titles to functional nodes. The UI
            # displays the ComfyUI native display name for the node type.
            n.pop('title', None)
            entry = api.get(str(n['id']))
            if entry is not None and isinstance(entry.get('_meta'), dict):
                entry['_meta'].pop('title', None)
            _ct_fit_prompt_node(n)
            if n['type'] == 'SaveImage':
                n['widgets_values'] = ['ComfyUI']
                if 'widgets_values_named' in n:
                    n['widgets_values_named']['filename_prefix'] = 'ComfyUI'
                if entry is not None:
                    entry['inputs']['filename_prefix'] = 'ComfyUI'
    if architecture_aware:
        _ct_layout_architecture(workflow)
    _ct_one_workflow_note(workflow)
    validate_graph(workflow, api)
    return workflow, api


# --- Optional per-clip audio sidecars ---
# WAV is 32-bit IEEE float to retain the decoded source tensor's precision.
# No external model catalogs, ffmpeg, FLAC codec or video assembly are involved.
_CT_AUDIO_PCM_RATE_CHOICES = (16000, 24000, 32000, 44100, 48000, 96000)
_CT_WAV_MAX_DATA = (1 << 32) - 256


def _ct_audio_tensor_descriptor(conn, clip):
    """Strictly locate and sanity-check the clip's source audio tensor.

    The source clip FlatBuffer stores an audio tensor ID, *not* audio codec or
    sampling rate. DT's tensors.dim is a 12-element little-endian int32 array;
    the two active elements in observed LTX2.3 exports are [channels, samples].
    Never interpret FPS as audio sampling rate.
    """
    aid = clip['audio_id']
    if not aid:
        return None
    row = conn.execute('SELECT format, datatype, dim, length(data), data FROM tensors WHERE name=?',
                       ('audio_' + str(aid),)).fetchone()
    if row is None:
        raise CTError(f'clip {clip["id"]} references missing audio tensor {aid}')
    format_code, dtype_code, dim, encoded_length, encoded = row
    if format_code != 1 or dtype_code != 16384:
        raise CTError('unsupported Draw Things audio tensor encoding '
                      f'(format={format_code}, datatype={dtype_code}) for clip {clip["id"]}')
    if not isinstance(dim, bytes) or len(dim) != 48:
        raise CTError(f'clip {clip["id"]} has malformed audio tensor dimensions')
    dims = _ct_struct.unpack('<12i',dim)
    channels, samples = dims[:2]
    if any(dims[2:]) or channels not in (1,2) or not 0 < samples <= 100_000_000:
        raise CTError(f'clip {clip["id"]} unsupported audio tensor dimensions: {dims[:4]}')
    if not encoded or encoded_length < 16:
        raise CTError(f'clip {clip["id"]} has empty audio FPZIP data')
    # This is a conservative WAV/RIFF size cap; no artificial audio truncation.
    if channels*samples*4 > _CT_WAV_MAX_DATA:
        raise CTError(f'clip {clip["id"]} audio would exceed the WAV RIFF size limit')
    # The clip record has playback FPS but no sample rate. Identify common rate
    # by aligning its audio length with the timing of N generated video frames.
    # A final 10 ms of audio padding is observed in both real DT LTX2.3 clips.
    video_seconds = max(0., (clip['count']-1)/clip['fps'])
    if clip['count'] < 2:
        raise CTError(f'clip {clip["id"]}: cannot determine audio sample rate from one video frame')
    comparisons = []
    for rate in _CT_AUDIO_PCM_RATE_CHOICES:
        difference = abs(samples / rate - video_seconds)
        comparisons.append((difference,rate))
    comparisons.sort()
    difference,rate = comparisons[0]
    # We require a unique, close match. This is not a silent, model-agnostic
    # 'all videos must be 48kHz' assumption.
    if (difference > max(.025, min(.05, video_seconds*.01)) or
        (len(comparisons) > 1 and comparisons[1][0] - difference < .02)):
        raise CTError(f'clip {clip["id"]}: sample rate not reliably recoverable '
                      f'({samples} samples, {clip["count"]} frames, {clip["fps"]} FPS)')
    return {'id':aid,'channels':channels,'samples':samples,'rate':rate,
            'encoded':encoded,'encoded_sha256':hashlib.sha256(encoded).hexdigest(),
            'rate_inferred':True, 'duration_s': samples/rate}


def _ct_audio_samples(desc):
    try:
        import fpzip
        import numpy as np
    except ImportError as exc:
        raise CTError('audio extraction requires: python3 -m pip install numpy fpzip') from exc
    try:
        uncompressed = fpzip.decompress(desc['encoded'],order='C')
    except Exception as exc:
        raise CTError('audio FPZIP decode failed: '+str(exc)) from exc
    samples = np.asarray(uncompressed)
    if samples.size != desc['channels']*desc['samples']:
        raise CTError('audio FPZIP shape/size differs from source tensor dimensions')
    if samples.dtype.kind != 'f' or samples.dtype.itemsize not in (4,8):
        raise CTError('decoded audio tensor is not floating-point')
    samples = samples.reshape((desc['channels'],desc['samples'])).astype('<f4',copy=False)
    if not np.isfinite(samples).all():
        raise CTError('decoded audio tensor contains non-finite samples')
    return samples


def _ct_wav_chunks(desc, channel_samples, owner):
    """Yield a deterministic RIFF/WAVE 32-bit IEEE float stream in chunks.

    The private CTID chunk proves provenance for safe resumability. Unknown
    WAV chunks are explicitly supported by RIFF, so this remains a WAV file.
    """
    import numpy as np
    from itertools import chain
    channels,samples,rate=desc['channels'],desc['samples'],desc['rate']
    provenance=json_text(owner).encode('utf-8')
    if len(provenance)>64_000:
        raise CTError('oversized WAV provenance')
    def chunk(tag,payload):
        return tag+_ct_struct.pack('<I',len(payload))+payload+(b'\x00' if len(payload)%2 else b'')
    fmt = chunk(b'fmt ', _ct_struct.pack('<HHIIHH',3,channels,rate,rate*channels*4,channels*4,32))
    fact=chunk(b'fact',_ct_struct.pack('<I',samples))
    ident=chunk(b'CTID',provenance)
    data_len=channels*samples*4
    header_len=12+len(fmt)+len(fact)+len(ident)+8
    if header_len+data_len-8 >= (1<<32):
        raise CTError('RIFF output exceeds 4 GiB')
    yield b'RIFF'+_ct_struct.pack('<I',header_len+data_len-8)+b'WAVE'
    yield fmt
    yield fact
    yield ident
    yield b'data'+_ct_struct.pack('<I',data_len)
    for first in range(0,samples,65536):
        last=min(samples,first+65536)
        # shape source: [channel,frame], WAV requires frame-interleaved [frame,channel].
        yield np.ascontiguousarray(channel_samples[:,first:last].T,dtype='<f4').tobytes(order='C')


def _ct_wav_owner(path):
    """Find CTID provenance without interpreting or loading the audio body."""
    try:
        with path.open('rb') as f:
            head=f.read(12)
            if len(head)!=12 or head[:4]!=b'RIFF' or head[8:]!=b'WAVE':
                return None
            while True:
                hdr=f.read(8)
                if len(hdr)!=8:
                    return None
                name,sz=hdr[:4],_ct_struct.unpack('<I',hdr[4:])[0]
                if name==b'CTID' and sz<64000:
                    return json.loads(f.read(sz).decode('utf-8'))
                if name==b'data':
                    return None
                f.seek(sz+(sz&1),1)
    except (ValueError,TypeError,OSError,UnicodeError,json.JSONDecodeError):
        return None


def _ct_wave_matches(path, desc, audio_data, owner):
    """Deep verification: byte-for-byte comparison of all WAV headers and samples."""
    if _ct_wav_owner(path)!=owner:
        return False
    try:
        with path.open('rb') as f:
            for part in _ct_wav_chunks(desc,audio_data,owner):
                if f.read(len(part))!=part:
                    return False
            return not f.read(1)
    except OSError:
        return False


def _ct_export_audio(conn,clip,folder,dbname,first_wall_clock_us,*,dry_run=False,no_birthtime=False):
    """Once per clip, export/resume/repair a lossless stereo WAV safely.

    Never overwrite an unrelated existing audio.wav. Writes an exclusive
    temporary file, verifies all content, then publishes atomically.
    """
    if not clip['audio_id']:
        return 'no-audio'
    desc=_ct_audio_tensor_descriptor(conn,clip)
    owner={'source':'ComfyThings Draw Things clip audio', 'database_name':dbname,
           'clip_id':clip['id'],'clip_audio_tensor_id':desc['id'],
           'source_fpzip_sha256':desc['encoded_sha256'],
           'sample_rate_hz':desc['rate'],'sample_rate_inferred':True,
           'channels':desc['channels'],'samples_per_channel':desc['samples'],
           'fps':clip['fps'],'frame_count':clip['count']}
    wav=folder/(folder.name+'.wav')
    legacy=folder/'audio.wav'
    if dry_run:
        return 'planned'
    folder.mkdir(parents=True,exist_ok=True)
    audio_data=_ct_audio_samples(desc)
    if wav.exists() and _ct_wave_matches(wav,desc,audio_data,owner):
        if legacy != wav and legacy.is_file() and not legacy.is_symlink():
            if _ct_wave_matches(legacy,desc,audio_data,owner):
                legacy.unlink()  # safe duplicate (same generated PCM + metadata)
            else:
                raise CTError('unrelated/changed legacy audio.wav alongside valid WAV; refusing removal')
        return 'verified'
    if legacy != wav and (legacy.exists() or legacy.is_symlink()) and not wav.exists():
        if legacy.is_symlink() or not _ct_wave_matches(legacy,desc,audio_data,owner):
            raise CTError('legacy audio.wav is unrelated or damaged; refusing migration')
        _ct_os.link(legacy,wav)  # no overwrite; keep source until published
        legacy.unlink()
        return 'verified'
    if wav.exists() and _ct_wav_owner(wav)!=owner:
        raise CTError(f'clip {clip["id"]}: unrelated or unverifiable named WAV occupies {wav}; '
                      'refusing overwrite')
    repairing=wav.exists()
    fd,raw=tempfile.mkstemp(prefix='.comfythings-',suffix='.ctpart',dir=str(folder))
    tmp=_ct_Path(raw)
    try:
        with _ct_os.fdopen(fd,'wb') as out:
            fd=-1
            for part in _ct_wav_chunks(desc,audio_data,owner):
                out.write(part)
            out.flush();_ct_os.fsync(out.fileno())
        if not _ct_wave_matches(tmp,desc,audio_data,owner):
            raise CTError('generated audio WAV failed verification before publication')
        try:
            _ct_set_created_time(tmp,first_wall_clock_us,set_birthtime=(
                _ct_sys.platform=='darwin' and not no_birthtime))
        except OSError as exc:
            print('    WARNING: audio timestamp update failed: '+str(exc),file=_ct_sys.stderr)
        if repairing and _ct_wav_owner(wav)!=owner:
            raise CTError('audio sidecar ownership changed during repair')
        _ct_publish_temp(tmp,wav,repair=repairing)
        return 'repaired' if repairing else 'saved'
    finally:
        if fd>=0:
            _ct_os.close(fd)
        tmp.unlink(missing_ok=True)



# --- Atomic video encoding and export ---
# An FFmpeg executable and ffprobe are required ONLY when encoding/verifying MP4.
# No custom ComfyUI video nodes, original model assets, or retained intermediate
# PNG/WAV files are required in MP4-only mode.
import subprocess as _ct_subprocess
import tempfile as _ct_tempfile

_CT_MP4_NAME = 'video.mp4'
_CT_MP4_SOURCE = 'ComfyThings Draw Things clip MP4'

_CT_VIDEO_QUALITIES = ('default', 'high', 'professional', 'lossless')


def _ct_filename_prefix(value):
    """Reject unsafe/ambiguous cross-platform file and directory stems."""
    if (not isinstance(value, str) or not value or len(value) > 80 or
            value != value.strip() or value in ('.', '..') or
            value[-1] in ' .' or any(ord(c) < 32 or ord(c) == 127 for c in value) or
            any(c in '/\\\\:*?"<>|' for c in value) or
            not _ct_re.fullmatch(r'[\w -]+', value, flags=_ct_re.UNICODE) or
            value.casefold() in {'con', 'prn', 'aux', 'nul',
                                 *(f'com{n}' for n in range(1, 10)),
                                 *(f'lpt{n}' for n in range(1, 10))}):
        raise ValueError('prefix must be 1–80 characters using letters, digits, '
                         'spaces, underscores or hyphens; no trailing space, '
                         'reserved device name, path or special characters')
    return value


def _ct_video_profile(video_format='mp4', video_quality='default'):
    """Container-aware profiles; the standard profile preserves default encoding."""
    if video_quality == 'default':
        if video_format == 'webm':
            return dict(encoder='libvpx-vp9', codec='vp9', audio='libopus',
                        audio_codec='opus', pixel='yuv420p', input_bits=8,
                        padded=True, crf='28', profile='libvpx-vp9-crf28-yuv420p-opus192k-v1')
        return dict(encoder='libx264', codec='h264', audio='aac',
                    audio_codec='aac', pixel='yuv420p', input_bits=8,
                    padded=True, crf='18', profile='libx264-crf18-yuv420p-aac192k-v1')
    if video_format == 'webm':
        raise CTError('video quality %s is not supported in WebM; use --video-format mp4, mov or mkv' % video_quality)
    if video_quality == 'high':
        return dict(encoder='libx264', codec='h264', audio='aac',
                    audio_codec='aac', pixel='yuv420p', input_bits=8,
                    padded=True, crf='14', profile='libx264-crf14-yuv420p-aac192k-v1')
    if video_quality == 'professional':
        return dict(encoder='libx265', codec='hevc', audio='aac',
                    audio_codec='aac', pixel='yuv420p10le', input_bits=16,
                    padded=True, crf='18', profile='libx265-crf18-yuv420p10le-aac192k-v1')
    if video_quality == 'lossless':
        if video_format != 'mkv':
            raise CTError('lossless video requires Matroska: use --video-format mkv')
        return dict(encoder='ffv1', codec='ffv1', audio='pcm_f32le',
                    audio_codec='pcm_f32le', pixel='gbrp16le', input_bits=16,
                    padded=False, crf=None, profile='ffv1-gbrp16le-pcmf32-lossless-v1')
    raise CTError('unrecognized video quality: '+str(video_quality))


def _ct_apply_filename_prefix(workflow, api, prefix):
    """Change generated ComfyUI SaveImage/SaveVideo filename-prefix inputs only.

    Provenance, model checkpoint names, user prompts and DT settings are NOT
    rewritten. Call after graph polishing, before workflow fingerprinting.
    """
    if prefix == 'DrawThings':
        return workflow, api
    def transformed(v):
        if not isinstance(v,str):
            return v
        # ComfyUI video saves can use a directory prefix before basename.
        for starter in ('DrawThings_', 'video/DrawThings_'):
            if v.startswith(starter):
                return ('video/' if starter.startswith('video/') else '') + prefix + '_' + v[len(starter):]
        return v
    for n in workflow.get('nodes', []):
        if n.get('type') not in ('SaveImage','SaveVideo'):
            continue
        w=n.get('widgets_values_named')
        if isinstance(w,dict) and 'filename_prefix' in w:
            w['filename_prefix']=transformed(w['filename_prefix'])
        w=n.get('widgets_values')
        if isinstance(w,list):
            n['widgets_values']=[transformed(v) for v in w]
    for n in api.values():
        if not isinstance(n,dict) or n.get('class_type') not in ('SaveImage','SaveVideo'):
            continue
        ins=n.get('inputs')
        if isinstance(ins,dict) and 'filename_prefix' in ins:
            ins['filename_prefix']=transformed(ins['filename_prefix'])
    return workflow, api



def _ct_ffmpeg_tools(video_format="mp4", video_quality="default"):
    """Preflight MP4 executables before any files or output folders are made."""
    encoder = _ct_shutil.which('ffmpeg')
    probe = _ct_shutil.which('ffprobe')
    if not encoder or not probe:
        missing=', '.join(name for name,present in (('ffmpeg',encoder),('ffprobe',probe)) if not present)
        raise CTError('MP4 export requires FFmpeg and ffprobe; missing from PATH: %s. '
                      'macOS: brew install ffmpeg (provides both executables); '
                      'Windows: install complete FFmpeg binaries and add bin to PATH. '
                      'For PNG/WAV-only exports, use --no-videos.' % missing)
    for title, executable in (('FFmpeg', encoder), ('ffprobe', probe)):
        try:
            result = _ct_subprocess.run([executable, '-version'],
                     stdout=_ct_subprocess.DEVNULL, stderr=_ct_subprocess.DEVNULL,
                     timeout=10, check=False)
        except (OSError, _ct_subprocess.TimeoutExpired) as exc:
            raise CTError('MP4 mode requires a working %s executable: %s. macOS: brew install ffmpeg (provides ffmpeg + ffprobe)' % (title, exc)) from exc
        if result.returncode:
            raise CTError('MP4 mode requires a working %s executable (version check failed). macOS: brew install ffmpeg (provides both tools)' % title)
    spec = _ct_video_profile(video_format, video_quality)
    required = [spec['encoder'], spec['audio']]
    result=_ct_subprocess.run([encoder,'-hide_banner','-encoders'],
           stdout=_ct_subprocess.PIPE,stderr=_ct_subprocess.PIPE,timeout=20)
    if result.returncode:
        raise CTError('FFmpeg could not list available encoders')
    names=result.stdout.decode('utf-8','replace')
    for codec in required:
        if not _ct_re.search(r'(?m)^\s*[VASFD.]{6}\s+'+_ct_re.escape(codec)+r'\s', names):
            raise CTError('%s output requires the FFmpeg %s encoder; macOS: brew install ffmpeg' % (video_format.upper(), codec))
    return encoder, probe


def _ct_ffprobe(path, exe=None, *, count=False):
    exe = exe or _ct_shutil.which('ffprobe')
    if not exe:
        return None
    cmd = [exe, '-v', 'error', '-show_entries',
           'format_tags=comment:stream=codec_type,codec_name,pix_fmt,width,height,nb_read_frames,avg_frame_rate',
           '-show_streams', '-show_format', '-of', 'json']
    if count:
        cmd.insert(3, '-count_frames')
    cmd.append(str(path))
    try:
        result = _ct_subprocess.run(cmd, stdout=_ct_subprocess.PIPE,
                                    stderr=_ct_subprocess.PIPE, timeout=1800,
                                    check=False)
        if result.returncode or len(result.stdout) > 2_000_000:
            return None
        return json.loads(result.stdout.decode('utf-8'))
    except (OSError, _ct_subprocess.TimeoutExpired, ValueError, UnicodeError):
        return None


def _ct_mp4_owner(path, probe=None):
    info = _ct_ffprobe(path, probe)
    if not info:
        return None
    tags = info.get('format', {}).get('tags', {})
    comment = next((v for k,v in tags.items() if k.casefold() == 'comment'), '')
    if not comment.startswith('ComfyThingsMP4:'):
        return None
    try:
        owner = json.loads(comment[len('ComfyThingsMP4:'):])
        return owner if owner.get('source') == _CT_MP4_SOURCE else None
    except (ValueError, TypeError, AttributeError):
        return None


def _ct_mp4_plan_fingerprint(clip, rows, index, desc):
    """Identify the exact stored clip and frame-history selection, not UI version.

    This signature catches changed/edited frame histories or switched audio.
    The original FPZIP tensor bytes are not altered or permanently re-encoded.
    """
    digest = hashlib.sha256(b'ComfyThings-MP4-inputs-v1\0')
    digest.update(clip['raw'])
    for row in rows:
        _, lineage, logical, _, blob, h = row
        tensor = index.get((lineage, logical))
        if tensor is None or isinstance(tensor, Exception):
            raise CTError('clip %s: generated frame has no valid tensor' % clip['id'])
        tensor_id, width, height = tensor
        if (width, height) != (clip['width'], clip['height']):
            raise CTError('clip %s: history frame dimensions differ from clip' % clip['id'])
        digest.update(_ct_struct.pack('<qqqq', h['index_in_a_clip'], lineage,
                                      logical, tensor_id))
        digest.update(hashlib.sha256(blob).digest())
    if desc:
        digest.update(bytes.fromhex(desc['encoded_sha256']))
        digest.update(str(desc['rate']).encode('ascii'))
    return digest.hexdigest()


def _ct_mp4_owner_equivalent(stored, expected):
    """Respect compatible existing MP4 provenance without re-encoding.

    Earlier builds recorded all exact clip lineage and encoding properties,
    but lacked the redundant container name. Only MP4 is eligible for this
    narrow backwards-compatible comparison.
    """
    if not isinstance(stored, dict):
        return False
    if stored == expected:
        return True
    if expected.get('container') != 'mp4' or 'container' in stored:
        return False
    without_container=dict(expected)
    without_container.pop('container',None)
    return stored == without_container


def _ct_mp4_verify(path, owner, probe):
    info = _ct_ffprobe(path, probe, count=True)
    if not info or not _ct_mp4_owner_equivalent(_ct_mp4_owner(path, probe), owner):
        return False
    streams = info.get('streams', [])
    video = [s for s in streams if s.get('codec_type') == 'video']
    audios = [s for s in streams if s.get('codec_type') == 'audio']
    if len(video) != 1 or video[0].get('codec_name') != owner.get('codec', 'h264').split('+')[0]:
        return False
    if (int(video[0].get('width', -1)) != owner['mp4_width'] or
            int(video[0].get('height', -1)) != owner['mp4_height'] or
            int(video[0].get('nb_read_frames') or -1) != owner['frame_count']):
        return False
    try:
        actual_rate = video[0].get('avg_frame_rate', '0/1')
        from fractions import Fraction as _ct_fraction
        if abs(float(_ct_fraction(actual_rate)) - owner['fps']) > max(.001,owner['fps']*.001):
            return False
    except (ValueError,ZeroDivisionError,TypeError):
        return False
    if owner.get('video_pixel_format') and video[0].get('pix_fmt') != owner['video_pixel_format']:
        return False
    if owner['audio_present']:
        if len(audios) != 1 or audios[0].get('codec_name') != owner.get('audio_codec', 'opus' if owner.get('container') == 'webm' else 'aac'):
            return False
    elif audios:
        return False
    return True


def _ct_mp4_existing_frames(folder, rows, clip, dbname, utc, filename_prefix='DrawThings', require_16bit=False):
    """Return a trusted complete PNG sequence or None (then stream from DB).

    Do not treat filenames alone as proof: validate each stored frame's
    ComfyThings provenance, clip/frame index, and complete compressed pixels.
    """
    base = filename_prefix + '_' + _ct_dt_from_epoch_us(rows[0][0], utc).strftime('%Y%m%d_%H%M%S_%f')
    pattern = str(folder / (base + '_%06d.png'))
    for row in rows:
        unix_us, lineage, logical, _, _, h = row
        path = folder / (base + '_%06d.png' % (h['index_in_a_clip']+1))
        if not path.is_file() or path.is_symlink():
            return None
        owner = _ct_provenance_in_file(path)
        if not owner or any((owner.get(k) != v for k,v in {
                'database_name': dbname, 'clip_id': clip['id'],
                'frame_index':h['index_in_a_clip'], 'lineage':lineage,
                'logical_time':logical, 'seed':h['seed'],
                'wall_clock_raw':h['wall_clock']}.items())):
            return None
        if owner.get('content_type') != 'video_frame':
            return None
        if require_16bit and '16-bit' not in owner.get('image_encoding', ''):
            return None
        try:
            # CRC/container validation is performed by the PNG parser;
            # verify the full lossless image stream independently.
            _ct_verify_idat_stream(path, clip['width'], clip['height'],
                                   16 if '16-bit' in owner.get('image_encoding','') else 8)
        except Exception:
            return None
    return pattern


def _ct_mp4_temp_audio(conn, clip, desc, folder, dbname, *, no_birthtime=False,
                       source_audio_dir=None):
    """Make temporary or reuse existing WAV; never leave a new standalone WAV."""
    if desc is None:
        return None, None
    owner = {'source':'ComfyThings Draw Things clip audio','database_name':dbname,
             'clip_id':clip['id'], 'clip_audio_tensor_id':desc['id'],
             'source_fpzip_sha256':desc['encoded_sha256'],
             'sample_rate_hz':desc['rate'], 'sample_rate_inferred':True,
             'channels':desc['channels'], 'samples_per_channel':desc['samples'],
             'fps':clip['fps'],'frame_count':clip['count']}
    # Reuse verified WAVs in previously exported clip folders, even when
    # MP4-only mode puts its output in the root. Never create a standalone WAV.
    for directory in ([source_audio_dir] if source_audio_dir is not None else []) + [folder]:
        for filename in (directory.name+'.wav','audio.wav'):
            current = directory/filename
            if current.is_file() and not current.is_symlink() and _ct_wav_owner(current) == owner:
                return current, None
    fd, tmpname = _ct_tempfile.mkstemp(prefix='.comfythings-mp4-', suffix='.wav', dir=str(folder))
    tmp = _ct_Path(tmpname)
    try:
        samples = _ct_audio_samples(desc)
        with _ct_os.fdopen(fd, 'wb') as f:
            fd=-1
            for part in _ct_wav_chunks(desc, samples, owner):
                f.write(part)
        return tmp, tmp
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        if fd != -1:
            _ct_os.close(fd)


def _ct_cleanup_abandoned_mp4(root):
    """Clean failed staging in the root (MP4-only) and clip folders (both)."""
    removed=0
    if not root.is_dir():
        return 0
    folders=[root] + [p for p in root.iterdir() if p.is_dir() and not p.is_symlink()]
    videos = root/'videos'
    if videos.is_dir() and not videos.is_symlink():
        folders.extend(p for p in videos.iterdir() if p.is_dir() and not p.is_symlink())
    for folder in folders:
        for path in folder.glob('.comfythings-mp4-*'):
            if (path.suffix not in ('.mp4','.mov','.mkv','.webm','.wav','.progress') or
                    path.is_symlink() or not path.is_file()):
                continue
            try:
                path.unlink()
                removed+=1
            except OSError:
                pass
    return removed


def _ct_choose_root_mp4_path(root, stem, dbname, clip_id, claimed, *, video_format="mp4", encoding_profile=None):
    """Stable, provenance-aware MP4 name in the ROOT export directory.

    Prevent accidental overwrite when an unrelated MP4 shares the same DT
    timestamp. When a previous export claimed a suffixed name, reuse it.
    No directories are created by this function.
    """
    import re as _root_re
    existing=[]
    if root.is_dir():
        for candidate in root.glob(stem+'*.'+video_format):
            tail=candidate.stem[len(stem):]
            if tail and not _root_re.fullmatch(r'_[1-9]\d*',tail):
                continue
            if candidate in claimed or candidate.is_symlink() or not candidate.is_file():
                continue
            owner=_ct_mp4_owner(candidate)
            if (owner and owner.get('database_name')==dbname and owner.get('clip_id')==clip_id
                    and (encoding_profile is None or owner.get('encoding_profile') == encoding_profile)):
                existing.append((0 if not tail else int(tail[1:]),candidate))
    if existing:
        path=min(existing)[1]
        claimed.add(path)
        return path
    for n in range(1000000):
        candidate=root/(stem+('' if n==0 else '_'+str(n))+'.'+video_format)
        if candidate in claimed or candidate.exists() or candidate.is_symlink():
            continue
        claimed.add(candidate)
        return candidate
    raise CTError('too many MP4 filename collisions for '+stem)


def _ct_mp4_decode_ok(ffmpeg, path):
    """Decode the complete media streams instead of trusting header metadata."""
    check=_ct_subprocess.run([ffmpeg,'-v','error','-xerror','-i',str(path),
                              '-f','null','-'],stdout=_ct_subprocess.DEVNULL,
                             stderr=_ct_subprocess.PIPE,timeout=3600)
    return check.returncode == 0


def _ct_mp4_rgb24(pixels):
    import numpy as np
    rgb = np.asarray(pixels, dtype=np.float32)
    if rgb.ndim != 3 or rgb.shape[-1] != 3 or not np.isfinite(rgb).all():
        raise CTError('invalid RGB tensor for MP4')
    return np.rint((np.clip(rgb, -1, 1).astype(np.float64)*0.5+0.5)*255).astype('u1').tobytes()


def _ct_mp4_rgb48_rows(pixels):
    """Stream PNG-equivalent RGB16 little-endian rows without another lossy stage."""
    import numpy as np
    rgb=np.asarray(pixels,dtype=np.float32)
    if rgb.ndim != 3 or rgb.shape[-1] != 3 or not np.isfinite(rgb).all():
        raise CTError('invalid RGB tensor for 16-bit video')
    for row in rgb:
        yield np.rint((np.clip(row,-1,1).astype(np.float64)*0.5+0.5)*65535).astype('<u2').tobytes()


def _ct_ffmpeg_encode_mp4(ffmpeg, temp, clip, rows, index, conn, png_pattern,
                           audio_path, owner, progress=None, video_format="mp4", video_quality="default"):
    """Encode from existing PNGs or pipe decoded DB RGB frames to stdin.

    stderr goes to a temporary handle, never PIPE, preventing a stalled
    encoder from deadlocking when output warnings exceed OS pipe capacity.
    """
    fps = format(clip['fps'], '.12g')
    width,height=clip['width'],clip['height']
    spec=_ct_video_profile(video_format, video_quality)
    cmd=[ffmpeg,'-hide_banner','-nostdin','-loglevel','error','-y']
    if png_pattern:
        cmd+=['-framerate',fps,'-start_number','1','-i',png_pattern]
    else:
        cmd+=['-f','rawvideo','-pixel_format','rgb48le' if spec['input_bits']==16 else 'rgb24',
              '-video_size',f'{width}x{height}', '-framerate',fps,'-i','pipe:0']
    if audio_path:
        cmd+=['-i',str(audio_path)]
    cmd+=['-map','0:v:0']
    if audio_path:
        cmd+=['-map','1:a:0']
    cmd+=['-frames:v',str(clip['count']),'-c:v',spec['encoder']]
    if video_quality == 'default' and video_format == 'webm':
        cmd+=['-deadline','good','-cpu-used','3','-crf','28','-b:v','0']
    elif video_quality == 'lossless':
        cmd+=['-level','3','-slicecrc','1']
    else:
        cmd+=['-preset','medium','-crf',spec['crf']]
    cmd+=['-pix_fmt',spec['pixel']]
    if spec['padded']:
        cmd+=['-vf','pad=ceil(iw/2)*2:ceil(ih/2)*2']
    if audio_path:
        cmd+=['-c:a',spec['audio']]
        if video_quality != 'lossless':
            cmd+=['-b:a','192k']
        cmd+=['-af','apad','-t',format(clip['count']/clip['fps'],'.12g')]
    else:
        cmd+=['-an']
    cmd+=['-metadata','comment=ComfyThingsMP4:'+json_text(owner)]
    if video_format in ('mp4','mov'):
        cmd+=['-movflags','+faststart+use_metadata_tags']
    cmd+=['-f','matroska' if video_format=='mkv' else video_format,str(temp)]
    # FFmpeg's machine-readable progress is written to a short-lived file
    # so neither stdout nor stderr can fill up and deadlock the exporter.
    fd, progress_name = _ct_tempfile.mkstemp(prefix='.comfythings-mp4-',
                                             suffix='.progress', dir=str(temp.parent))
    _ct_os.close(fd)
    cmd[1:1] = ['-progress', progress_name, '-stats_period', '0.5']
    def show_encoded_frames(fallback=0):
        if progress is None:
            return
        try:
            data = _ct_Path(progress_name).read_text('utf-8',errors='replace')
            matches = _ct_re.findall(r'(?m)^frame=(\d+)',data)
            count = int(matches[-1]) if matches else 0
        except (OSError,ValueError):
            count=0
        progress.done = max(progress.done, min(clip['count'],count))
        progress.show(getattr(progress,'encoding_label','Encoding %s' % video_format.upper()))
    try:
      with _ct_tempfile.TemporaryFile() as err:
        proc=_ct_subprocess.Popen(cmd,stdin=_ct_subprocess.PIPE if not png_pattern else _ct_subprocess.DEVNULL,
                                  stdout=_ct_subprocess.DEVNULL, stderr=err)
        try:
            if not png_pattern:
                for frame_ordinal, row in enumerate(rows,1):
                    _, lineage, logical, _, _, h = row
                    tensor_id, w, hgt = index[(lineage, logical)]
                    if proc.poll() is not None:
                        raise CTError('FFmpeg exited before receiving all frame data')
                    pixels=_ct_decode_pixels(conn,tensor_id,w,hgt)
                    if spec['input_bits']==16:
                        for raw_row in _ct_mp4_rgb48_rows(pixels):
                            proc.stdin.write(raw_row)
                    else:
                        proc.stdin.write(_ct_mp4_rgb24(pixels))
                    if progress is not None and (frame_ordinal % 3 == 0 or frame_ordinal == clip['count']):
                        show_encoded_frames(frame_ordinal)
                proc.stdin.close()
            import time as _ct_mp4_time
            deadline=_ct_mp4_time.monotonic()+3600
            while True:
                try:
                    code=proc.wait(timeout=0.5)
                    break
                except _ct_subprocess.TimeoutExpired:
                    show_encoded_frames()
                    if _ct_mp4_time.monotonic()>deadline:
                        raise CTError('FFmpeg encoding exceeded one-hour timeout')
            if code == 0 and progress is not None:
                progress.done = clip['count']
                progress.show(getattr(progress,'encoding_label','Encoding %s' % video_format.upper()))
        except BaseException:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            raise
        if code:
            err.seek(0)
            detail=err.read(12_000).decode('utf-8','replace').strip()
            raise CTError('FFmpeg failed (%s): %s' % (code,detail[-2500:]))
    finally:
        _ct_Path(progress_name).unlink(missing_ok=True)


def _ct_export_mp4(conn, index, clip, rows, folder, dbname, utc, no_audio,
                   *, ffmpeg_tools=None, dry_run=False, no_birthtime=False, show_progress=True,
                   destination=None, frame_source_dir=None, video_format='mp4', video_quality='default',
                   filename_prefix='DrawThings', video_status=None):
    """Export a full clip atomically, with deterministic ownership and resume.

    Only the source database is read. Audio intermediates are temporary.
    PNGs are reused only when every frame's provenance and pixels verify.
    """
    if len(rows)!=clip['count'] or [r[5]['index_in_a_clip'] for r in rows]!=list(range(clip['count'])):
        raise CTError('clip %s is missing ordered frames; MP4 export requires a complete clip'
                      % clip['id'])
    if dry_run:
        return 'planned'
    if not ffmpeg_tools:
        ffmpeg_tools=_ct_ffmpeg_tools(video_format, video_quality)
    ffmpeg,probe=ffmpeg_tools
    desc=None
    if clip['audio_id'] and not no_audio:
        try:
            desc=_ct_audio_tensor_descriptor(conn,clip)
        except Exception as exc:
            # Still export a usable silent MP4. Never invent a sample rate.
            print('    WARNING: clip %s audio cannot be decoded; making silent MP4: %s' %
                  (clip['id'],exc),file=_ct_sys.stderr)
    spec=_ct_video_profile(video_format, video_quality)
    owner={'source':_CT_MP4_SOURCE,'database_name':dbname,
           'clip_id':clip['id'],'frame_count':clip['count'],
           'fps':clip['fps'],'mp4_width':(clip['width']+1)//2*2 if spec['padded'] else clip['width'],
           'mp4_height':(clip['height']+1)//2*2 if spec['padded'] else clip['height'],
           'audio_present':bool(desc), 'codec':spec['codec']+'+'+spec['audio_codec'] if desc else spec['codec'],
           'container':video_format,
           'input_fingerprint':_ct_mp4_plan_fingerprint(clip,rows,index,desc),
           'encoding_profile':spec['profile']}
    # Preserve established default ownership metadata for safe resumption. Only
    # new profiles claim a pixel-format constraint in container provenance.
    if video_quality != 'default':
        owner['video_pixel_format']=spec['pixel']
        owner['audio_codec']=spec['audio_codec']
    # Final MP4s belong in output_dir/videos/, never inside
    # the per-clip PNG/WAV directory. MP4-only creates no per-clip folders.
    if destination is None:
        raise CTError('internal error: video destination must be explicit')
    dest=destination
    work_dir=dest.parent
    existing_frames_dir=frame_source_dir if frame_source_dir is not None else folder
    if dest.exists() or dest.is_symlink():
        if dest.is_symlink() or not dest.is_file():
            raise CTError('MP4 destination is not a regular file: '+str(dest))
        previous=_ct_mp4_owner(dest,probe)
        if previous is None or previous.get('database_name') != dbname or previous.get('clip_id') != clip['id']:
            raise CTError('unrelated or unverifiable video.mp4 occupies '+str(dest))
        if (_ct_mp4_owner_equivalent(previous,owner) and _ct_mp4_verify(dest,owner,probe) and
                _ct_mp4_decode_ok(ffmpeg,dest)):
            return 'verified'
        repair=True
    else:
        repair=False
    # Previously supported output layouts placed MP4s directly in output_dir or as video.mp4
    # inside individual clip folders, or as a flat MP4 in the clip
    # folder's parent. Reuse and move only *fully* verified same-clip media.
    if video_format == 'mp4' and not repair and not dest.exists():
        base=filename_prefix+'_'+_ct_dt_from_epoch_us(rows[0][0],utc).strftime('%Y%m%d_%H%M%S_%f')
        output_root=dest.parent.parent
        candidates=[existing_frames_dir/_CT_MP4_NAME,
                    output_root/(base+'.mp4')]
        candidates.extend(sorted(output_root.glob(base+'_*.mp4')))
        # Also check video.mp4 in an existing clip directory.
        candidates.extend(sorted(output_root.glob(base+'*/video.mp4')))
        # Some existing layouts use the clip directory for video files.
        for legacy in dict.fromkeys(candidates):
            if legacy==dest or legacy.is_symlink() or not legacy.is_file():
                continue
            old_owner=_ct_mp4_owner(legacy,probe)
            if (_ct_mp4_owner_equivalent(old_owner,owner) and _ct_mp4_verify(legacy,owner,probe)
                    and _ct_mp4_decode_ok(ffmpeg,legacy)):
                dest.parent.mkdir(parents=True,exist_ok=True)
                _ct_os.link(legacy,dest)
                legacy.unlink()
                try:
                    legacy.parent.rmdir()
                except OSError:
                    pass
                return 'verified'
    work_dir.mkdir(parents=True,exist_ok=True)
    # Verify the precision of EVERY cached frame. For 10/16-bit video we
    # decode original FPZIP instead of promoting any 8-bit frame PNG.
    pattern=(_ct_mp4_existing_frames(existing_frames_dir,rows,clip,dbname,utc,filename_prefix,
                                     require_16bit=(spec['input_bits']==16))
             if existing_frames_dir.is_dir() else None)
    audio_path=None
    temp_audio=None
    temp=None
    try:
        audio_path,temp_audio=_ct_mp4_temp_audio(conn,clip,desc,work_dir,dbname,
            source_audio_dir=existing_frames_dir)
        fd,name=_ct_tempfile.mkstemp(prefix='.comfythings-mp4-',suffix='.'+video_format,dir=str(work_dir))
        _ct_os.close(fd)
        temp=_ct_Path(name)
        if video_status is not None:
            video_status.encoding(clip['count'],video_format)
            prog=video_status
        else:
            prog=_CTProgress(clip['count'],enabled=show_progress,show_detail=True,animate=True)
            prog.show(f'Clip {clip["id"]} | Encoding {video_format.upper()}')
        try:
            _ct_ffmpeg_encode_mp4(ffmpeg,temp,clip,rows,index,conn,pattern,audio_path,owner,progress=prog,video_format=video_format,video_quality=video_quality)
        finally:
            if video_status is not None:
                prog.clear()
            else:
                prog.cancel()
        if video_status is not None:
            video_status.stage(f'Verifying {video_format.upper()}')
        if not _ct_mp4_verify(temp,owner,probe):
            raise CTError('new MP4 failed metadata, dimensions, codec, audio or frame-count verification')
        # Full media decode catches damaged bitstreams rather than trusting
        # the container headers alone.
        if not _ct_mp4_decode_ok(ffmpeg,temp):
            raise CTError('new MP4 fails full video/audio decode verification')
        try:
            _ct_set_created_time(temp,rows[0][0],set_birthtime=(
                _ct_sys.platform=='darwin' and not no_birthtime))
        except OSError as exc:
            print('    WARNING: MP4 timestamp update failed: '+str(exc),file=_ct_sys.stderr)
        if repair:
            current=_ct_mp4_owner(dest,probe)
            if not current or current.get('database_name')!=dbname or current.get('clip_id')!=clip['id']:
                raise CTError('previous MP4 ownership changed; refusing replacement')
        if video_status is not None:
            video_status.stage(f'Saving {video_format.upper()}')
        _ct_publish_temp(temp,dest,repair=repair)
        return 'saved'
    finally:
        if temp_audio is not None:
            temp_audio.unlink(missing_ok=True)
        if temp is not None:
            temp.unlink(missing_ok=True)

def ct_main(argv=None):
    p = _ct_argparse.ArgumentParser(
        description='ComfyThings v1.01: Draw Things database -> ComfyUI PNGs, workflows and video (atomic/resumable)')
    p.add_argument('database', type=_ct_Path,
                   help='Draw Things database COPY (extension .sqlite3 optional)')
    p.add_argument('output_dir', type=_ct_Path, nargs='?', default=None,
                   help='destination directory (default: database name, in current directory)')
    # Architecture-aware conversion is the only output policy. No external
    # model-catalog flags or legacy workflow selection; all are obsolete.
    recovery_options = p.add_mutually_exclusive_group()
    recovery_options.add_argument('--recover-empty-slots', dest='recover_empty_slots',
                                  action='store_true', default=True,
                                  help='enable strictly validated empty-ID still-image recovery (default: enabled)')
    recovery_options.add_argument('--no-recover-empty-slots', dest='recover_empty_slots',
                                  action='store_false',
                                  help='disable empty-ID recovery and use standard strict tensor selection')
    p.add_argument('--recovery-only', action='store_true',
                   help='process only strict recovered still histories (no video; unchanged files verified/reused)')
    p.add_argument('--recover-editing-sequences', action='store_true', default=False,
                   help='opt in to strictly guarded editing-sequence still-image recovery (off by default)')
    p.add_argument('--editing-recovery-only', action='store_true',
                   help='export only strict editing-sequence recoveries; automatically enables --recover-editing-sequences')
    p.add_argument('--bit-depth', choices=(8, 16), type=int, default=8,
                   help='RGB PNG precision: 8 (default, smaller files) or 16 (maximum fidelity)')
    p.add_argument('--png-filter', choices=('adaptive', 'none'), default='adaptive',
                   help='lossless scanline filtering: adaptive (default) or none')
    p.add_argument('--compression-level', type=int, default=9,
                   help='zlib compression level 0..9 (default 9)')
    p.add_argument('--dry-run', action='store_true',
                   help='inspect planned exports without writing image files')
    p.add_argument('--no-set-birthtime', action='store_true',
                   help='do not attempt to set macOS Date Created from history')
    p.add_argument('--utc', action='store_true',
                   help='use UTC for filename timestamps (default: local time)')
    p.add_argument('--no-progress', action='store_true',
                   help='disable animated progress, showing concise phases and final summary')
    p.add_argument('--no-integrity-check', action='store_true',
                   help='skip full SQLite PRAGMA quick_check for trusted database copies (default: check)')
    p.add_argument('--verbose', '-v', action='store_true',
                   help='show per-clip and per-image details; disables live progress bars')
    verification = p.add_mutually_exclusive_group()
    verification.add_argument('--verify', dest='verify', action='store_true',
                              help='deep-check the zlib pixel stream (default; retained for compatibility)')
    verification.add_argument('--no-verify', dest='verify', action='store_false',
                              help='skip the optional deep pixel-stream check for newly exported PNGs')
    p.set_defaults(verify=True)
    video = p.add_argument_group('Video', 'Video files are encoded by default; video workflow PNGs are automatically included.')
    video.add_argument('--images-only', action='store_true',
                       help='export ordinary still-image PNGs only; suppress ALL video-related exports')
    video.add_argument('--videos-only', '--no-images', dest='videos_only', action='store_true',
                       help='suppress ordinary still images only; other video outputs retain their defaults and opt-ins')
    video.add_argument('--no-videos', action='store_true',
                       help='suppress encoded video files and default workflow PNGs; --video-workflows explicitly overrides the latter')
    workflow_flags = video.add_mutually_exclusive_group()
    workflow_flags.add_argument('--video-workflows', dest='video_workflows', action='store_const', const=True,
                       help='explicitly export first-frame ComfyUI video workflow PNGs')
    workflow_flags.add_argument('--no-video-workflows', dest='video_workflows', action='store_const', const=False,
                       help='suppress separate video workflow PNGs')
    video.add_argument('--video-workflows-only', action='store_true',
                       help='export only video workflow PNGs: no ordinary images, videos, WAVs or other frames')
    video.add_argument('--video-export-frames', action='store_true',
                       help='also export every video frame as PNG; the first numbered frame includes its video workflow')
    video.add_argument('--video-export-audio', action='store_true',
                       help='also export a clip-named standalone WAV (does not affect encoded-video audio)')
    video.add_argument('--video-format', choices=('mp4','mov','mkv','webm'), default=None,
                       help='video container (default: mp4; mkv for lossless); WebM defaults to VP9/Opus')
    video.add_argument('--video-quality', choices=_CT_VIDEO_QUALITIES, default='default',
                       help='default=H.264 CRF18; high=H.264 CRF14; professional=10-bit HEVC; lossless=16-bit FFV1/float PCM MKV')
    p.set_defaults(video_workflows=None)
    p.add_argument('--filename-prefix', default='DrawThings', metavar='PREFIX',
                   help='filename and clip-folder stem prefix (default: DrawThings), e.g. ExamplePrefix')
    p.add_argument('--limit', type=int, default=0,
                   help='only export first N generated images (0 = all)')
    p.add_argument('--version', action='version', version='ComfyThings ' + CT_VERSION)
    a = p.parse_args(argv)
    try:
        a.filename_prefix = _ct_filename_prefix(a.filename_prefix)
    except ValueError as exc:
        p.error('--filename-prefix: ' + str(exc))
    if a.video_format is None:
        a.video_format = 'mkv' if a.video_quality == 'lossless' else 'mp4'
    try:
        _ct_video_profile(a.video_format, a.video_quality)
    except CTError as exc:
        p.error(str(exc))
    # Draw Things project names may contain spaces, dots or Unicode. Append
    # only the database extension; do not rename or modify the source DB.
    if a.database.suffix.casefold() != '.sqlite3':
        a.database = _ct_Path(str(a.database) + '.sqlite3')
    # Destination defaults to the database's filename stem in the current
    # working directory, never next to the live Draw Things app container.
    if a.output_dir is None:
        a.output_dir = _ct_Path(a.database.stem)
    if a.limit < 0:
        p.error('--limit must be >= 0')
    if a.editing_recovery_only:
        if a.recovery_only:
            p.error('--editing-recovery-only conflicts with --recovery-only')
        if (a.videos_only or a.video_workflows_only or a.video_export_frames or
                a.video_export_audio or a.video_workflows is True):
            p.error('--editing-recovery-only conflicts with video export options')
        a.recover_editing_sequences = True
        a.images_only = True
    if a.recovery_only:
        if not a.recover_empty_slots:
            p.error('--recovery-only cannot be combined with --no-recover-empty-slots')
        if (a.videos_only or a.video_workflows_only or a.video_export_frames or
                a.video_export_audio or a.video_workflows is True):
            p.error('--recovery-only conflicts with video export options')
        a.images_only = True
    if a.images_only and (a.videos_only or a.video_workflows_only or a.video_export_frames
                          or a.video_export_audio or a.video_workflows is True):
        p.error('--images-only conflicts with explicit video export options')
    if a.videos_only and a.images_only:
        p.error('--videos-only cannot be combined with --images-only')
    if a.video_workflows_only and (a.video_export_frames or a.video_export_audio or
                                   a.video_workflows is False or a.images_only):
        p.error('--video-workflows-only cannot be combined with frames, WAVs, --images-only or --no-video-workflows')
    a.export_mp4 = not (a.no_videos or a.images_only or a.video_workflows_only)
    a.export_frames = a.video_export_frames and not a.images_only and not a.video_workflows_only
    a.export_audio = a.video_export_audio and not a.images_only and not a.video_workflows_only
    a.export_workflows = (not a.images_only and
                          (a.video_workflows_only or a.video_workflows is True or
                           (a.video_workflows is None and not a.no_videos)))
    if a.limit and a.export_mp4:
        p.error('--limit cannot be combined with encoded video export; clips must be complete (use --no-videos)')
    if not 0 <= a.compression_level <= 9:
        p.error('--compression-level must be between 0 and 9')
    if a.database.resolve() == a.output_dir.resolve():
        p.error('output directory cannot be the database file')
    if a.output_dir.exists() and not a.output_dir.is_dir():
        p.error('output path exists but is not a directory')
    # Print before SQLite quick_check and tensor/history scanning, which can
    # take a noticeable amount of time on large Draw Things databases.
    # flush=True ensures the user immediately sees that the process started.
    print('\n' + _ct_console_header(CT_VERSION), flush=True)
    try:
        # Fail FAST before SQLite scan, any PNG, any output directory, and
        # even before the destination lock file if MP4 tools are missing.
        # Dry-run remains an inspection-only operation with no FFmpeg dependency.
        ffmpeg_tools_early = (_ct_ffmpeg_tools(a.video_format, a.video_quality)
                              if a.export_mp4 and not a.dry_run else None)
        print('\nPreparing: ' + a.database.name, flush=True)
        prep = _CTPreparation(enabled=not (a.no_progress or a.verbose))
        if a.no_integrity_check:
            prep.begin('Opening database (integrity check skipped by request)')
            conn = _ct_read_only_connection(a.database, check_integrity=False)
        else:
            prep.begin('Checking SQLite database integrity')
            conn = _ct_read_only_connection(a.database)
        prep.finish()
        try:
            # The COUNT(*) queries are read-only and avoid loading tensor BLOBs;
            # show an activity spinner while SQLite computes the record totals.
            prep.begin('Counting database records')
            def _prepare_count(table):
                try:
                    return conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                except _ct_sqlite3.OperationalError as error:
                    # An injected/mock connection can implement the scanning
                    # methods without exposing the source schema. In that case
                    # display elapsed time rather than a fabricated percentage.
                    if 'no such table' in str(error):
                        return None
                    raise
            tensor_count=_prepare_count('tensordata')
            history_count=_prepare_count('tensorhistorynode')
            prep.finish()
            prep.begin('Indexing image tensors',total=tensor_count)
            index = _ct_tensor_index(conn,progress=prep)
            prep.finish()
            prep.begin('Scanning generation histories',total=history_count)
            all_records, damaged_history = _ct_collect_history(conn,progress=prep)
            prep.finish()
            video_requested = (a.export_mp4 or a.export_frames or a.export_audio or a.export_workflows)
            clips={}
            if video_requested:
                prep.begin('Reading video clip records')
                clips=_ct_read_clips(conn)
                prep.finish()
            prep.begin('Validating clip histories')
            clip_groups, video_warnings = (_ct_preflight_video(all_records, clips)
                                           if video_requested else ({}, []))
            prep.finish()
            resolved_tensor_clips={}
            if video_requested and clip_groups:
                prep.begin('Resolving video frame tensors',total=sum(len(rows) for rows in clip_groups.values()))
                resolved_tensor_clips=_ct_resolve_video_tensor_streams(
                    conn, index, clip_groups, clips, progress=prep)
                prep.finish()
            resolved_still_images={}
            placeholder_resolved=set()
            shared_placeholder_resolved=set()
            editing_append_resolved=set()
            if not (a.videos_only or a.video_workflows_only):
                still_ambiguous_count = sum(
                    r[5]['clip_id'] == -1 and
                    isinstance(index.get((r[1], r[2])), CTError) and
                    str(index[(r[1], r[2])]).startswith('ambiguous image tensor references')
                    for r in all_records)
                if still_ambiguous_count:
                    prep.begin('Resolving still-image tensor references', total=still_ambiguous_count)
                    resolved_still_images = _ct_resolve_still_tensor_references(
                        conn, index, all_records, progress=prep,
                        allow_empty_slot=a.recover_empty_slots,
                        placeholder_resolved=placeholder_resolved,
                        shared_placeholder_resolved=shared_placeholder_resolved,
                        allow_editing_append=a.recover_editing_sequences,
                        editing_append_resolved=editing_append_resolved)
                    prep.finish()
            selected_clips = set(clip_groups)
            records=[] if (a.videos_only or a.video_workflows_only) else [
                (r, 'image') for r in all_records if r[5]['clip_id'] == -1]
            if a.export_frames:
                records.extend((r, 'frame') for r in all_records if r[5]['clip_id'] != -1)
            if a.export_workflows:
                records.extend((rows[0], 'workflow') for rows in clip_groups.values()
                               if rows and rows[0][5]['index_in_a_clip'] == 0)
            records.sort(key=lambda rr: (rr[0][0], rr[0][1], rr[0][2],
                                          {'workflow':0,'frame':1,'image':2}[rr[1]]))
            if a.recovery_only:
                records=[(r,kind) for r,kind in records if
                         kind == 'image' and (r[1],r[2]) in placeholder_resolved]
            if a.editing_recovery_only:
                records=[(r,kind) for r,kind in records if
                         kind == 'image' and (r[1],r[2]) in editing_append_resolved]
            if a.limit:
                records=records[:a.limit]
                selected_clips={r[5]['clip_id'] for r,typ in records if r[5]['clip_id'] != -1}
            # Videos are also selected when MP4s or standalone WAVs are requested.
            # --limit only applies to non-video/diagnostic exports.
            # Display source database without its containing path; retains the
            # full path in --verbose diagnostic output below.
            action = 'Planning' if a.dry_run else 'Exporting'
            print(f'{action}: {a.database.name}')
            filtering = 'Adaptive filtering' if a.png_filter == 'adaptive' else 'No filtering'
            if not a.videos_only:
                print(f'PNG: {a.bit_depth}-bit RGB, {filtering}, zlib level {a.compression_level}')
            if a.export_mp4:
                print('Video: %s (%s) with embedded audio when available (FFmpeg)' % (
                    a.video_format.upper(), 'VP9/Opus' if a.video_format=='webm' else 'FFV1/PCM float32 16-bit RGB' if a.video_quality=='lossless' else 'HEVC 10-bit/AAC' if a.video_quality=='professional' else 'H.264 CRF14/AAC' if a.video_quality=='high' else 'H.264/AAC'))
            print()
            if placeholder_resolved:
                print('    Empty-slot still recoveries identified: %d (validated narrow rule)' %
                      len(placeholder_resolved))
                if shared_placeholder_resolved:
                    print('    Including later non-generated tensor reuse: %d image(s)' %
                          len(shared_placeholder_resolved))
                print('    Other ambiguous histories remain conservatively unresolved.')
            if a.recover_editing_sequences:
                print('    Editing-sequence recoveries (opt-in): %d image(s)' %
                      len(editing_append_resolved))
                if a.verbose and editing_append_resolved:
                    print('    Editing-sequence history keys: ' + ', '.join(
                        '%s:%s' % key for key in sorted(editing_append_resolved)))
            if resolved_still_images:
                print('    Still-image tensor references resolved: %d image(s) in %d lineage(s)' % (
                    len(resolved_still_images), len({lin for lin, _ in resolved_still_images})))
                if a.verbose:
                    print('    Resolved still-image histories: ' + ', '.join(
                        '%s:%s' % pair for pair in sorted(resolved_still_images)))
                print()
            if resolved_tensor_clips:
                print('    Video tensor references resolved: %d frame(s) in %d clip(s)' % (
                    sum(resolved_tensor_clips.values()), len(resolved_tensor_clips)))
                if a.verbose:
                    print('    Resolved clip IDs: ' + ', '.join(map(str, sorted(resolved_tensor_clips))))
                print()  # Separate tensor-resolution notice from export status.
            if a.verbose:
                print(f'    Generation histories selected: {len(records)}')
                print('    Filename normalization: off')
                print('    Workflow policy: architecture-aware connected best-effort reconstruction')
                print(f'    Database: {a.database}')
                print(f'    Output directory: {a.output_dir}')
            if not a.dry_run:
                a.output_dir.mkdir(parents=True, exist_ok=True)
            from contextlib import nullcontext
            run_lock = nullcontext() if a.dry_run else _ct_output_lock(a.output_dir)
            with run_lock:
                if not a.dry_run:
                    old_staging = _ct_cleanup_abandoned_staging(a.output_dir)
                    old_staging += _ct_cleanup_abandoned_mp4(a.output_dir)
                    if old_staging:
                        print('    Recovered from previous interruption: removed %d abandoned staging file(s)'
                              % old_staging)
                exported, skipped, repaired, refreshed, failed = 0, 0, 0, 0, len(damaged_history)
                images_exported = 0
                frames_exported = 0
                workflows_exported = 0
                # Unique clips with successfully created, planned, or reused
                # workflow-bearing first-frame PNGs (no double counting when
                # both the separate workflow PNG and full frames are requested).
                video_workflows_by_clip = {}
                videos_exported = set()
                noticed = 0
                lines = a.verbose or a.no_progress
                progress = _CTProgress(len(records), enabled=bool(records) and not (a.no_progress or a.verbose),
                                       planned=a.dry_run, animate=not a.dry_run)
                progress.errors = len(damaged_history)
                claimed = set()
                claimed_dirs = set()
                claimed_legacy_dirs=set()
                video_dirs={}
                split_dirs={}
                videos_root=a.output_dir/'videos'
                resolver = _CTModelResolver()
                mp4_destinations={}
                claimed_mp4s=set()
                for cid in sorted(selected_clips, key=lambda c: (clip_groups[c][0][0], c)):
                    first=clip_groups[cid][0]
                    dirname=a.filename_prefix+'_'+_ct_dt_from_epoch_us(first[0],a.utc).strftime('%Y%m%d_%H%M%S_%f')
                    current, splits = _ct_choose_video_directory(
                        videos_root,clips[cid],dirname,a.database.name,claimed_dirs)
                    previous, old_splits = _ct_choose_video_directory(
                        a.output_dir,clips[cid],dirname,a.database.name,claimed_legacy_dirs)
                    prev_owned=(previous.is_dir() and
                                _ct_video_directory_owner(previous,a.database.name,cid) is not None)
                    current_owned=(current.is_dir() and
                                 _ct_video_directory_owner(current,a.database.name,cid) is not None)
                    # Relocate an owned clip directory left at the old root
                    # into videos/, even on MP4-only runs. This does not create
                    # any *new* per-clip export; it preserves already exported
                    # PNGs/WAVs and keeps legacy artifacts out of the root.
                    if (a.filename_prefix == 'DrawThings' and prev_owned and
                            not current_owned and not current.exists() and not a.dry_run):
                        videos_root.mkdir(parents=True,exist_ok=True)
                        previous.rename(current)  # atomic on same filesystem
                        current_owned=True
                        prev_owned=False
                    if a.export_frames or a.export_audio or a.export_workflows:
                        video_dirs[cid]=current
                        split_dirs[cid]=list(splits)+( [previous]+old_splits if prev_owned else [] )
                    else:
                        # Existing PNG/WAV data is optional as an MP4 source.
                        video_dirs[cid]=current if current_owned else previous if prev_owned else current
                        split_dirs[cid]=[]
                    if a.export_mp4:
                        mp4_destinations[cid]=_ct_choose_root_mp4_path(
                            videos_root,dirname,a.database.name,cid,claimed_mp4s, video_format=a.video_format,
                            encoding_profile=_ct_video_profile(a.video_format,a.video_quality)['profile'])
                for cid in sorted(selected_clips):
                    if split_dirs[cid]:
                        progress.message('    Reconciling prior split folders for clip %s...' % cid)
                        moved,dup_frames,dup_audio=_ct_reunite_video_frames(
                            video_dirs[cid],split_dirs[cid],a.database.name,cid,dry_run=a.dry_run)
                        progress.message('    %s %d existing frame(s) into the canonical clip folder; '
                                         '%s %d redundant PNG(s), %d redundant WAV(s)' % (
                            'Would move' if a.dry_run else 'Moved',moved,
                            'would remove' if a.dry_run else 'removed',dup_frames,dup_audio))
                # An MP4-only rerun still needs to finish layout migration:
                # historic audio.wav must become <clip-stem>.wav even though
                # standalone audio was NOT requested this time. Do not load
                # audio tensors or touch any non-ComfyThings-owned WAV.
                for cid in sorted(selected_clips):
                    folder = video_dirs[cid]
                    if folder.is_dir() and not folder.is_symlink():
                        status = _ct_normalize_legacy_video_audio(
                            folder, a.database.name, cid, dry_run=a.dry_run)
                        if status != 'unchanged':
                            progress.message('    Clip %s: %s legacy audio.wav -> %s.wav' % (
                                cid, status.replace('-', ' '), folder.name))
                if a.export_workflows:
                    for cid in sorted(selected_clips):
                        if a.filename_prefix != 'DrawThings':
                            continue  # no legacy prefix migration under custom naming
                        status=_ct_migrate_old_workflow_first_frame(
                            videos_root, video_dirs[cid], a.database.name, cid,
                            clip_groups[cid][0], dry_run=a.dry_run)
                        if status != 'none':
                            progress.message('    Clip %s: %s legacy first-frame PNG as workflow PNG' % (cid,status))
                diagnostics=[]
                if a.verbose:
                    for warning in video_warnings:
                        progress.message('    WARNING: ' + warning)
                    for lin,logical,reason in damaged_history:
                        progress.message(f'    ERROR: invalid history record lineage={lin} '
                                         f'logical_time={logical}: {reason}',error=True)
                for i, (row, output_kind) in enumerate(records, 1):
                    unix_us, lineage, logical, ts_kind, blob, h = row
                    try:
                        stamp = _ct_dt_from_epoch_us(unix_us, a.utc)
                        stem = a.filename_prefix + '_' + stamp.strftime('%Y%m%d_%H%M%S_%f')
                        record_key = {
                            'database_name': a.database.name,
                            'lineage': lineage,
                            'logical_time': logical,
                            'wall_clock_raw': h['wall_clock'],
                            'seed': h['seed'],
                        }
                        tensor = index.get((lineage, logical))
                        if tensor is None:
                            raise CTError('generated-image record has no linked tensor')
                        if isinstance(tensor, Exception):
                            raise tensor
                        tensor_id, width, height = tensor
                        prompt, recovered = _ct_missing_prompt_recovery(conn, h)
                        meta = _ct_history_metadata(h, prompt, h['negative_text_prompt'] or '',
                                                    recovered_prompt=recovered)
                        cid = h['clip_id']
                        clip = clips[cid] if cid != -1 else None
                        if clip is not None:
                            if (width, height) != (clip['width'], clip['height']):
                                raise CTError('video frame dimensions do not match parent clip')
                            # These describe DIFFERENT things in Draw Things:
                            # the generation-setting num_frames vs. the stored
                            # parent clip's actual frame count. In particular,
                            # complete 297-frame clips may carry num_frames=304.
                            # Frame indexes and bounds are already enforced by
                            # _ct_preflight_video, and source numFrames is kept
                            # unmodified in DT metadata and raw FlatBuffers.
                            resolution = resolver.resolve(str(meta.get('model') or ''))
                            workflow, api, notes = _ct_video_workflow(
                                meta, clip, h['index_in_a_clip'], resolution=resolution,
                                reconstruct=(output_kind == 'workflow' or h['index_in_a_clip'] == 0))
                        else:
                            resolution = resolver.resolve(str(meta.get('model') or ''))
                            workflow, api, notes, architecture_status, architecture_family = _ct_architecture_graph(
                                meta, (width, height), resolution)
                        # Classify the *actual* generated graph, not merely the
                        # architecture label: unknown or unsupported video
                        # models generate informational Note-only PNGs.
                        video_workflow_kind = None
                        if (clip is not None and h['index_in_a_clip'] == 0
                                and output_kind in ('workflow', 'frame')):
                            node_types = {n.get('type') for n in workflow.get('nodes', [])}
                            video_workflow_kind = ('connected' if {'SaveVideo', 'CreateVideo'} <= node_types
                                                   and bool(workflow.get('links')) else 'informational')
                        _ct_add_family_to_note(workflow, meta, resolution)
                        # Workflow layout polishing runs after the sampling graph
                        # is selected and before workflow fingerprint / PNG write.
                        workflow, api = _ct_polish_workflow(
                            workflow, api, architecture_aware=(clip is None))
                        workflow, api = _ct_apply_filename_prefix(workflow, api, a.filename_prefix)
                        if notes:
                            noticed += 1
                        provenance = {
                            'source': 'Draw Things SQLite project history',
                            **record_key,
                            'wall_clock_unit': ts_kind,
                            'wall_clock_unix_microseconds': unix_us,
                            'wall_clock_utc': _ct_dt_from_epoch_us(unix_us, True).isoformat(),
                            'filename_timezone': 'UTC' if a.utc else 'system local time',
                            'tensor_id': tensor_id,
                            'recovered_prompt': recovered,
                            'image_encoding': f'PNG {a.bit_depth}-bit RGB; normalized FPZIP float -> nearest uint{a.bit_depth}',
                            'ckpt_to_safetensors': False,
                            'png_filter': a.png_filter,
                            'png_compression_level': a.compression_level,
                            'metadata_origin': 'reconstructed from database, not original Draw Things PNG XMP',
                            'workflow_engine': ('ComfyThings video metadata + LTX-2.3 graph' if clip is not None
                                                else 'Draw Things to ComfyUI converter v' + VERSION),
                            'workflow_policy': (_CT_WORKFLOW_LAYOUT_REVISION + '-video' if clip is not None else _CT_WORKFLOW_LAYOUT_REVISION),
                            'workflow_fingerprint': hashlib.sha256(
                                (json_text(workflow) + '\0' + json_text(api)).encode('utf-8')
                            ).hexdigest(),
                        }
                        if clip is None:
                            if (lineage, logical) in editing_append_resolved:
                                # Record the exact rule used for a recovered association.
                                
                                provenance['tensor_association'] = (
                                    'strict_immediate_append_successor')
                            if (lineage, logical) in placeholder_resolved:
                                # Stable recovery classification supports
                                # fully verified, byte-unchanged resumption.
                                provenance['tensor_association'] = (
                                    'empty_id_slot0_shared_later_nongenerated_references'
                                    if (lineage, logical) in shared_placeholder_resolved
                                    else 'experimental_empty_id_slot0_unique_slot1_requires_visual_confirmation')
                            provenance.update({
                                'workflow_status': architecture_status,
                                'workflow_family': architecture_family,
                                'model_identification': resolution['status'],
                                'model_family_source': resolution['source'],
                                'model_family_label': resolution['family_label'],
                            })
                        if clip is not None:
                            frame_index = h['index_in_a_clip']
                            provenance.update({
                                'content_type': 'video_workflow' if output_kind == 'workflow' else 'video_frame',
                                'clip_id': cid,
                                'frame_index': frame_index,
                                'frame_number': frame_index + 1,
                                'frame_count': clip['count'],
                                'clip_fps': clip['fps'],
                                'clip_width': clip['width'],
                                'clip_height': clip['height'],
                                'clip_audio_tensor_id': clip['audio_id'],
                                'clip_start_wall_clock_us': clip_groups[cid][0][0],
                                'video_workflow_status': ('ltx2.3_reconstructed_unverified' if _ct_is_ltx23(meta, resolution) and (resolution.get('spec') or {}).get('text_encoder') and h['index_in_a_clip'] == 0
                                                          else 'informational_only'),
                                'model_identification': resolution['status'],
                                'model_family_source': resolution['source'],
                                'model_family_label': resolution['family_label'],
                            })
                            base_stamp = a.filename_prefix + '_' + _ct_dt_from_epoch_us(
                                clip_groups[cid][0][0], a.utc).strftime('%Y%m%d_%H%M%S_%f')
                            frame_stem = '%s_%06d' % (base_stamp, frame_index + 1)
                            target_dir = video_dirs[cid]
                            if output_kind == 'workflow':
                                candidate = target_dir / (target_dir.name + '_workflow.png')
                                claimed.add(candidate)
                                exists = candidate.exists()
                                if exists:
                                    old = _ct_provenance_in_file(candidate)
                                    if old is None or any(old.get(k) != v for k, v in record_key.items()):
                                        raise CTError('unrelated file occupies video workflow destination: '+str(candidate))
                            else:
                                candidate = target_dir / (frame_stem + '.png')
                                if candidate in claimed:
                                    raise CTError('duplicate video frame filename: '+str(candidate))
                                claimed.add(candidate)
                                exists = candidate.exists()
                                if exists:
                                    old = _ct_provenance_in_file(candidate)
                                    if old is None or any(old.get(k) != v for k, v in record_key.items()):
                                        raise CTError('unrelated file occupies video frame destination: '+str(candidate))
                        else:
                            candidate, exists = _ct_choose_path(a.output_dir, stem, record_key, claimed)
                        shown = (candidate.relative_to(a.output_dir) if clip is not None
                                 else candidate.name)
                        label = f'[{i:03}/{len(records)}] seed={h["seed"]} {width}x{height} -> {shown}'
                        if recovered:
                            label += ' [prompt recovered]'
                        if ts_kind != 'microseconds':
                            label += ' [legacy-seconds history timestamp]'
                        progress.show(f'seed {h["seed"]}: checking')
                        repair = False
                        metadata_refresh = False
                        if exists and not a.dry_run:
                            old = _ct_provenance_in_file(candidate) or {}
                            if (resolution['source'] == 'Civitai lookup' and
                                    resolution['status'] in ('error','ambiguous','unknown') and
                                    _ct_resume_previous_civitai_export(
                                        candidate,old,provenance,(width,height),
                                        a.bit_depth,meta,clip)):
                                if lines:
                                    print(label + ' [previously exported; verified; prior Civitai result retained]')
                                skipped += 1
                                if video_workflow_kind:
                                    video_workflows_by_clip[cid] = video_workflow_kind
                                progress.advance('reused',f'seed {h["seed"]}: verified')
                                continue
                            _ct_check_settings(old, provenance)
                            try:
                                # Always deeply validate existing files on resume, even
                                # without --verify; a valid provenance header is not enough.
                                _ct_validate_export(candidate, provenance, (width, height),
                                                    a.bit_depth, meta, workflow, api, deep_pixels=True,
                                                    clip_info=clip)
                            except Exception as exc:
                                repair = True
                                metadata_refresh = _ct_is_workflow_metadata_refresh(
                                    candidate, old, provenance, (width, height),
                                    a.bit_depth, meta, clip, blob)
                                if metadata_refresh:
                                    progress.message(
                                        '    Updating outdated ComfyUI workflow metadata '
                                        '(existing PNG pixels and source verified)', error=False)
                                else:
                                    progress.message(
                                        '    WARNING: previous CT export failed validation '
                                        'and will be repaired: ' + str(exc), error=True)
                            else:
                                if lines:
                                    print(label + ' [previously exported; verified]')
                                    if a.verbose and notes:
                                        progress.message('    Workflow notes: ' + '; '.join(_ct_explain_note(n) for n in notes))
                                skipped += 1
                                if video_workflow_kind:
                                    video_workflows_by_clip[cid] = video_workflow_kind
                                progress.advance('reused', f'seed {h["seed"]}: verified')
                                continue
                        if lines:
                            print(label + (' [preview]' if a.dry_run else ' [repair]' if repair else ''))
                        if a.dry_run:
                            if a.verbose and notes:
                                progress.message('    Workflow notes: ' + '; '.join(_ct_explain_note(n) for n in notes))
                            exported += 1
                            if video_workflow_kind:
                                video_workflows_by_clip[cid] = video_workflow_kind
                            if output_kind == 'workflow':
                                workflows_exported += 1
                            elif output_kind == 'frame':
                                frames_exported += 1
                                videos_exported.add(cid)
                            else:
                                images_exported += 1
                            progress.advance('saved', f'seed {h["seed"]}: planned')
                            continue
                        if clip is not None:
                            candidate.parent.mkdir(parents=True, exist_ok=True)
                        fd, raw_temp = tempfile.mkstemp(prefix='.comfythings-',
                                                         suffix='.ctpart', dir=str(a.output_dir))
                        temp = _ct_Path(raw_temp)
                        try:
                            # Exclusive mkstemp reserves this process's temporary
                            # pathname; the exporter writes only via its owned fd.
                            progress.show(f'seed {h["seed"]}: decoding / compressing')
                            _ct_write_rgb_png(temp, _ct_decode_pixels(conn, tensor_id, width, height),
                                              meta, workflow, api, blob, provenance, a.bit_depth,
                                              a.png_filter, a.compression_level, output_fd=fd,
                                              clip_info=clip)
                            fd = None  # _ct_write_rgb_png owns and closes it
                            # At minimum check CRCs and complete source metadata before
                            # making the destination filename visible to other programs.
                            progress.show(f'seed {h["seed"]}: validating')
                            _ct_validate_export(temp, provenance, (width, height), a.bit_depth,
                                                meta, workflow, api, deep_pixels=a.verify,
                                                clip_info=clip)
                            if repair:
                                # Avoid ever replacing a different file even if somebody
                                # manipulated the directory outside our CT lock.
                                current = _ct_provenance_in_file(candidate) or {}
                                if not all(current.get(k) == v for k, v in record_key.items()):
                                    raise CTError('damaged export changed during repair; refusing replacement')
                            # Verify the bytes first; file reads during verification
                            # may advance the Windows Accessed date. Set all source
                            # timestamps on the verified temp file immediately before
                            # publishing it to the final path.
                            try:
                                _ct_set_created_time(temp, unix_us, set_birthtime=(
                                    _ct_sys.platform == 'darwin' and not a.no_set_birthtime))
                            except OSError as exc:
                                progress.message('    WARNING: timestamp update failed: ' + str(exc),
                                                 error=True)
                            _ct_publish_temp(temp, candidate, repair=repair)
                        finally:
                            if fd is not None:
                                # fdopen closes the descriptor even if PNG
                                # encoding raises; avoid masking that error.
                                try:
                                    _ct_os.close(fd)
                                except OSError:
                                    pass
                            try:
                                temp.unlink(missing_ok=True)
                            except OSError:
                                pass
                        exported += 1
                        if video_workflow_kind:
                            video_workflows_by_clip[cid] = video_workflow_kind
                        if output_kind == 'workflow':
                            workflows_exported += 1
                        elif output_kind == 'frame':
                            frames_exported += 1
                            videos_exported.add(cid)
                        else:
                            images_exported += 1
                        if repair:
                            if metadata_refresh:
                                refreshed += 1
                            else:
                                repaired += 1
                        if a.verbose and notes:
                            progress.message('    Workflow notes: ' + '; '.join(_ct_explain_note(n) for n in notes))
                        progress.advance('saved', f'seed {h["seed"]}: exported')
                    except Exception as exc:
                        failed += 1
                        diagnostics.append((f'Image lineage={lineage} logical_time={logical}',str(exc)))
                        if a.verbose:
                            progress.message(f'    ERROR: {exc}', error=True)
                        progress.advance('error', 'Failed image')
                progress.finish()
                # An audio tensor belongs to an entire clip, not to an individual
                # frame. Process each selected clip once, even when all PNG frames
                # were previously exported and verified. --limit may select only
                # part of a clip; the soundtrack remains the whole clip's audio.
                audio_stats={'saved':0,'repaired':0,'verified':0,'planned':0,
                             'no-audio':0,'failed':0}
                if a.export_audio:
                    for cid in sorted(selected_clips):
                        clip=clips[cid]
                        try:
                            state=_ct_export_audio(conn,clip,video_dirs[cid],a.database.name,
                                clip_groups[cid][0][0],dry_run=a.dry_run,
                                no_birthtime=a.no_set_birthtime)
                            audio_stats[state]+=1
                            if a.verbose:
                                print(f'    Clip {cid}: audio {state} '
                                      f'({video_dirs[cid].relative_to(a.output_dir)}/{video_dirs[cid].name}.wav)')
                        except Exception as exc:
                            audio_stats['failed']+=1
                            diagnostics.append((f'Clip {cid} audio',str(exc)))
                            if a.verbose:
                                print(f'    ERROR: clip {cid} audio extraction: {exc}',file=_ct_sys.stderr)
                mp4_stats = {'saved': 0, 'verified': 0, 'planned': 0, 'failed': 0}
                if a.export_mp4:
                    ffmpeg_tools = ffmpeg_tools_early
                    clip_ids=sorted(selected_clips)
                    video_status=_CTVideoStatus(enabled=not (a.no_progress or a.verbose))
                    if clip_ids and not a.verbose:
                        print(f'Video export: {len(clip_ids)} clip(s)',flush=True)
                        print(flush=True)  # Keep the live clip progress on its own line.
                    for ordinal, cid in enumerate(clip_ids,1):
                        clip = clips[cid]
                        video_status.begin(cid,ordinal,len(clip_ids),a.video_format) if not (a.no_progress or a.verbose) else None
                        try:
                            if len(clip_groups[cid]) != clip['count']:
                                raise CTError('clip %s: incomplete frame history (%s/%s); '
                                              'cannot produce a complete MP4' % (
                                                  cid, len(clip_groups[cid]), clip['count']))
                            if a.export_frames and any(
                                    r[5]['clip_id'] == cid and
                                    (video_dirs[cid] / (a.filename_prefix + '_' +
                                     _ct_dt_from_epoch_us(clip_groups[cid][0][0], a.utc).strftime('%Y%m%d_%H%M%S_%f') +
                                     '_%06d.png' % (r[5]['index_in_a_clip'] + 1))).exists() is False
                                    for r in clip_groups[cid]):
                                raise CTError('clip %s: missing PNGs after frames export; MP4 skipped' % cid)
                            if a.verbose:
                                print(f'    Clip {cid}: ' + ('planning video' if a.dry_run else 'checking/encoding video...'),flush=True)
                            state = _ct_export_mp4(conn, index, clip, clip_groups[cid],
                                video_dirs[cid], a.database.name, a.utc, False,
                                ffmpeg_tools=ffmpeg_tools, dry_run=a.dry_run,
                                no_birthtime=a.no_set_birthtime,
                                show_progress=not (a.no_progress or a.verbose),
                                destination=mp4_destinations.get(cid),
                                frame_source_dir=video_dirs[cid], video_format=a.video_format,
                                video_quality=a.video_quality, filename_prefix=a.filename_prefix,
                                video_status=video_status if not (a.no_progress or a.verbose) else None)
                            mp4_stats[state] += 1
                            if a.verbose:
                                print(f'    Clip {cid}: {a.video_format.upper()} {state}')
                        except Exception as exc:
                            mp4_stats['failed'] += 1
                            diagnostics.append((f'Clip {cid} {a.video_format.upper()}',str(exc)))
                            if a.verbose:
                                print(f'    ERROR: clip {cid} video export: {exc}', file=_ct_sys.stderr)
                        finally:
                            video_status.stop()
                print()
                verb = 'planned' if a.dry_run else 'exported'
                if images_exported:
                    print(f'Images {verb}: {images_exported}')
                if workflows_exported:
                    print(f'Video workflow PNGs {verb}: {workflows_exported}')
                if video_workflows_by_clip:
                    connected = sum(v == 'connected' for v in video_workflows_by_clip.values())
                    informational = sum(v == 'informational' for v in video_workflows_by_clip.values())
                    print(f'Video workflows represented: {len(video_workflows_by_clip)} clip(s)')
                    print(f'    Connected generation graphs: {connected}')
                    print(f'    Informational-only workflows: {informational}')
                if frames_exported:
                    print(f'Video frames {verb}: {frames_exported} '
                          f'({len(videos_exported)} clip(s))')
                audio_exported = audio_stats['planned'] if a.dry_run else (audio_stats['saved'] + audio_stats['repaired'])
                if audio_exported:
                    print(f'Audio files {verb}: {audio_exported}')
                if mp4_stats['saved'] or mp4_stats['planned']:
                    print(f'Video files {verb}: {mp4_stats["planned"] if a.dry_run else mp4_stats["saved"]}')
                if mp4_stats['verified']:
                    print(f'Previously exported MP4s reused: {mp4_stats["verified"]}')
                already = skipped + audio_stats['verified']
                if already:
                    print(f'Previously exported files skipped: {already}')
                if refreshed:
                    print(f'Outdated workflow PNGs updated: {refreshed}')
                fixed = repaired + audio_stats['repaired']
                if fixed:
                    print(f'Previous exports repaired after validation failure: {fixed}')
                total_failed = failed + audio_stats['failed'] + mp4_stats['failed']
                if total_failed:
                    print(f'Errors: {total_failed}')
                _ct_diagnostic_report(video_warnings if not a.images_only else [],
                                      damaged_history,diagnostics,verbose=a.verbose)
                if a.verbose:
                    print(f'    MP4 details: {mp4_stats}')
                    print(f'    PNG histories processed: {len(records)}')
                    print(f'    PNGs {verb}: {exported} (workflow updates: {refreshed}; validation repairs: {repaired})')
                    print(f'    PNGs verified/reused: {skipped}')
                    print('    Audio details: ' + ', '.join(f'{name}={audio_stats[name]}'
                          for name in ('saved','repaired','verified','planned','no-audio','failed')))
                    if noticed:
                        print(f'    Workflow notes embedded in {noticed} PNG(s)')
                # Report the full destination after the export summary. A dry run
                # must not imply that files were written or create the directory.
                print()
                if a.dry_run:
                    print(f'Would export to: {a.output_dir.resolve()}')
                else:
                    print(f'Exported to: {a.output_dir.resolve()}')
                return 1 if total_failed else 0
        finally:
            conn.close()
    except KeyboardInterrupt:
        if 'progress' in locals():
            progress.cancel()
        if 'prep' in locals():
            prep.stop()
        if 'video_status' in locals():
            video_status.stop()
        print('\nComfyThings interrupted; completed PNGs remain safe. Rerun the same command to resume.',
              file=_ct_sys.stderr)
        return 130
    except Exception as exc:
        if 'progress' in locals():
            progress.cancel()
        if 'prep' in locals():
            prep.stop()
        if 'video_status' in locals():
            video_status.stop()
        print(f'ComfyThings ERROR: {exc}', file=_ct_sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(ct_main())
