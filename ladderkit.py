#!/usr/bin/env python3
"""ladderkit: offline video encoding-ladder planner.

Reads one plain-text plan file (source format, duration, segment and GOP
lengths, and one line per rendition) and reports exact file sizes, bits per
pixel, VBV buffers, HLS segment alignment, bandwidth fit, file-budget
bitrates, ffmpeg commands and an HLS master playlist. Standard library only.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from pathlib import Path

__version__ = "1.0.0"

MB = 10 ** 6
GIB = 2 ** 30
MAXRATE_FACTOR = Fraction(107, 100)
BUFSIZE_FACTOR = 2
DEFAULT_HEADROOM = Fraction(5, 4)
DEFAULT_SEGMENT = Fraction(6)
DEFAULT_GOP = Fraction(2)
DEFAULT_AUDIO = Fraction(128)
DEFAULT_ASPECT = (16, 9)
BPP_LOW = Fraction(3, 100)
BPP_HIGH = Fraction(6, 10)
MAX_RUNG_RATIO = Fraction(5, 2)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

KEYS = {"title", "source", "duration", "segment", "gop", "audio", "aspect", "headroom"}
RUNG_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
CLOCK_RE = re.compile(r"(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)")
SPAN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(s|sec|m|min|h)?")
KBPS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(k|kbps|m|mbps)?")

EXAMPLE = """\
# ladderkit plan: one file per title.
title: Harbour walk-through
source: 2160 50
duration: 1:12:30
segment: 6 s
gop: 2 s
audio: 128 kbps
aspect: 16:9
headroom: 1.25

rung 1080p 1080 50 6000
rung 720p 720 30 3000
rung 480p 480 30 1400
rung 360p 360 30 800
"""


@dataclass(frozen=True)
class Issue:
    severity: str
    line: int | None
    message: str

    def render(self, path: str) -> str:
        where = f"{path}:{self.line}" if self.line else path
        return f"{where}: {self.severity}: {self.message}"


@dataclass(frozen=True)
class Rung:
    name: str
    line: int
    height: int
    fps: Fraction
    kbps: Fraction


@dataclass
class Plan:
    title: str
    source_height: int
    source_fps: Fraction
    duration: Fraction
    segment: Fraction
    gop: Fraction
    audio_kbps: Fraction
    aspect: tuple
    headroom: Fraction
    rungs: list
    key_lines: dict = field(default_factory=dict)


# ---------------------------------------------------------------- parsing

def parse_number(text: str) -> Fraction:
    try:
        return Fraction(text.strip())
    except (ValueError, ZeroDivisionError):
        raise ValueError(f"not a number: {text.strip()!r}") from None


def parse_duration(text: str) -> Fraction:
    """Accepts H:MM:SS, M:SS, or a number with an optional s, m or h unit."""
    text = text.strip().lower()
    clock = CLOCK_RE.fullmatch(text)
    if clock:
        hours, minutes, seconds = clock.groups()
        return Fraction(int(hours or 0) * 3600 + int(minutes) * 60) + Fraction(seconds)
    span = SPAN_RE.fullmatch(text)
    if not span:
        raise ValueError(f"not a duration: {text!r}")
    value, unit = span.groups()
    scale = {None: 1, "s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600}[unit]
    return Fraction(value) * scale


def parse_kbps(text: str) -> Fraction:
    """Accepts 3000, 3000k, 3000 kbps, 3.5m or 3.5 Mbps (1 M = 1000 kbps)."""
    match = KBPS_RE.fullmatch(text.strip().lower())
    if not match:
        raise ValueError(f"not a bitrate: {text.strip()!r}")
    value, unit = match.groups()
    return Fraction(value) * (1000 if unit in ("m", "mbps") else 1)


def parse_height(text: str) -> int:
    match = re.fullmatch(r"(\d+)p?", text.strip().lower())
    if not match:
        raise ValueError(f"not a height: {text.strip()!r}")
    height = int(match.group(1))
    if height <= 0 or height % 2:
        raise ValueError("height must be a positive even number of lines")
    return height


def parse_source(text: str) -> tuple:
    parts = text.split()
    if len(parts) != 2:
        raise ValueError("expected HEIGHT FPS, e.g. '2160 50'")
    return parse_height(parts[0]), _positive(parse_number(parts[1]))


def parse_aspect(text: str) -> tuple:
    match = re.fullmatch(r"(\d+)\s*[:x/]\s*(\d+)", text.strip())
    if not match:
        raise ValueError("expected WIDTH:HEIGHT, e.g. 16:9")
    width, height = int(match.group(1)), int(match.group(2))
    if width <= 0 or height <= 0:
        raise ValueError("aspect parts must be greater than zero")
    return width, height


def parse_headroom(text: str) -> Fraction:
    value = parse_number(text)
    if value < 1:
        raise ValueError("headroom must be at least 1")
    return value


def _positive(value: Fraction) -> Fraction:
    if value <= 0:
        raise ValueError("must be greater than zero")
    return value


def _parse_rung(line: str, lineno: int) -> Rung:
    parts = line.split()
    if len(parts) != 5:
        raise ValueError("rung needs NAME HEIGHT FPS KBPS, e.g. 'rung 720p 720 30 3000'")
    _, name, height, fps, kbps = parts
    if not RUNG_NAME_RE.fullmatch(name):
        raise ValueError(f"bad rung name {name!r}: use letters, digits, '-' or '_'")
    return Rung(
        name=name,
        line=lineno,
        height=parse_height(height),
        fps=_positive(parse_number(fps)),
        kbps=_positive(parse_kbps(kbps)),
    )


def parse_plan(text: str) -> tuple:
    """Return (plan, issues). The plan is None when any error was found."""
    issues: list = []
    values: dict = {}
    rungs: list = []
    rung_lines: dict = {}

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.split()[0].lower() == "rung":
            try:
                rung = _parse_rung(line, lineno)
            except ValueError as exc:
                issues.append(Issue("error", lineno, str(exc)))
                continue
            if rung.name in rung_lines:
                issues.append(Issue(
                    "error", lineno,
                    f"duplicate rung name '{rung.name}' (first on line {rung_lines[rung.name]})",
                ))
            else:
                rung_lines[rung.name] = lineno
                rungs.append(rung)
            continue
        key, sep, value = line.partition(":")
        key = key.strip().lower()
        if not sep or key not in KEYS:
            issues.append(Issue("error", lineno, f"unrecognised line: {line}"))
        elif key in values:
            issues.append(Issue("error", lineno, f"duplicate key '{key}' (first on line {values[key][1]})"))
        else:
            values[key] = (value.strip(), lineno)

    def read(key, convert, default):
        if key not in values:
            return default
        raw, lineno = values[key]
        try:
            return convert(raw)
        except ValueError as exc:
            issues.append(Issue("error", lineno, f"{key}: {exc}"))
            return default

    for key in ("source", "duration"):
        if key not in values:
            issues.append(Issue("error", None, f"missing required key '{key}'"))
    if not rungs:
        issues.append(Issue("error", None, "no 'rung' lines: add at least one"))

    source_height, source_fps = read("source", parse_source, (1080, Fraction(30)))
    duration = read("duration", lambda t: _positive(parse_duration(t)), Fraction(1))
    segment = read("segment", lambda t: _positive(parse_duration(t)), DEFAULT_SEGMENT)
    gop = read("gop", lambda t: _positive(parse_duration(t)), DEFAULT_GOP)
    audio = read("audio", parse_kbps, DEFAULT_AUDIO)
    aspect = read("aspect", parse_aspect, DEFAULT_ASPECT)
    headroom = read("headroom", parse_headroom, DEFAULT_HEADROOM)

    if gop > segment:
        issues.append(Issue("error", values.get("gop", (None, None))[1], "gop cannot be longer than segment"))

    if any(issue.severity == "error" for issue in issues):
        return None, issues

    plan = Plan(
        title=values["title"][0] if "title" in values else "Untitled",
        source_height=source_height,
        source_fps=source_fps,
        duration=duration,
        segment=segment,
        gop=gop,
        audio_kbps=audio,
        aspect=aspect,
        headroom=headroom,
        rungs=rungs,
        key_lines={key: lineno for key, (_, lineno) in values.items()},
    )
    return plan, issues


# ---------------------------------------------------------------- maths

def rung_width(plan: Plan, rung: Rung) -> int:
    """Width from the aspect ratio, rounded to the nearest even number."""
    aspect_w, aspect_h = plan.aspect
    return 2 * round(Fraction(rung.height * aspect_w, aspect_h) / 2)


def size_bytes(plan: Plan, rung: Rung) -> Fraction:
    return (rung.kbps + plan.audio_kbps) * 1000 * plan.duration / 8


def bits_per_pixel(plan: Plan, rung: Rung) -> Fraction:
    pixels = rung_width(plan, rung) * rung.height * rung.fps
    return rung.kbps * 1000 / pixels


def verdict(bpp: Fraction) -> str:
    if bpp < BPP_LOW:
        return "starved"
    if bpp > BPP_HIGH:
        return "wasteful"
    return "ok"


def vbv(rung: Rung) -> tuple:
    """Return (maxrate, bufsize) in kbps, rounded up."""
    return math.ceil(rung.kbps * MAXRATE_FACTOR), math.ceil(rung.kbps * BUFSIZE_FACTOR)


def segment_count(plan: Plan) -> int:
    return math.ceil(plan.duration / plan.segment)


def video_kbps_for_budget(plan: Plan, size_mb: Fraction) -> Fraction:
    total_kbps = size_mb * MB * 8 / (plan.duration * 1000)
    return total_kbps - plan.audio_kbps


def ffmpeg_command(plan: Plan, rung: Rung, source: str = "source.mp4") -> str:
    maxrate, bufsize = vbv(rung)
    gop = int(plan.gop * rung.fps)
    return " ".join([
        f"ffmpeg -i {source}",
        "-map 0:v:0 -map 0:a:0?",
        "-c:v libx264 -preset slow",
        f"-b:v {math.ceil(rung.kbps)}k -maxrate {maxrate}k -bufsize {bufsize}k",
        f"-vf scale={rung_width(plan, rung)}:{rung.height}",
        f"-r {_plain(rung.fps)} -g {gop} -keyint_min {gop} -sc_threshold 0",
        f"-c:a aac -b:a {math.ceil(plan.audio_kbps)}k",
        f"-f hls -hls_time {_plain(plan.segment)} -hls_playlist_type vod",
        f"-hls_segment_filename {rung.name}_%03d.ts {rung.name}.m3u8",
    ])


def master_playlist(plan: Plan) -> str:
    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    for rung in sorted(plan.rungs, key=lambda r: r.kbps):
        peak = math.ceil((rung.kbps * MAXRATE_FACTOR + plan.audio_kbps) * 1000)
        average = math.ceil((rung.kbps + plan.audio_kbps) * 1000)
        lines.append(
            f"#EXT-X-STREAM-INF:BANDWIDTH={peak},AVERAGE-BANDWIDTH={average},"
            f'RESOLUTION={rung_width(plan, rung)}x{rung.height},'
            f'FRAME-RATE={_plain(rung.fps)},CODECS="avc1.640028,mp4a.40.2"'
        )
        lines.append(f"{rung.name}.m3u8")
    return "\n".join(lines) + "\n"


def lint(plan: Plan) -> list:
    warnings: list = []

    def warn(line, message):
        warnings.append(Issue("warning", line, message))

    for rung in plan.rungs:
        if rung.height > plan.source_height:
            warn(rung.line, f"rung {rung.name} ({rung.height}p) is taller than the {plan.source_height}p source")
        if rung.fps > plan.source_fps:
            warn(rung.line, f"rung {rung.name} runs at {_plain(rung.fps)} fps, faster than the {_plain(plan.source_fps)} fps source")
        if (plan.gop * rung.fps).denominator != 1:
            warn(rung.line, f"rung {rung.name}: a {_plain(plan.gop)} s GOP is a fractional frame count at {_plain(rung.fps)} fps")
        bpp = bits_per_pixel(plan, rung)
        if verdict(bpp) == "starved":
            warn(rung.line, f"rung {rung.name} gets {float(bpp):.4f} bits per pixel (below {float(BPP_LOW)}): likely blocky")
        elif verdict(bpp) == "wasteful":
            warn(rung.line, f"rung {rung.name} gets {float(bpp):.4f} bits per pixel (above {float(BPP_HIGH)}): bits are wasted")

    if (plan.segment / plan.gop).denominator != 1:
        warn(plan.key_lines.get("segment"), "segment is not a whole number of GOPs; HLS cuts will not land on keyframes")

    ordered = sorted(plan.rungs, key=lambda r: r.height)
    for lower, upper in zip(ordered, ordered[1:]):
        if lower.height == upper.height:
            warn(upper.line, f"rungs {lower.name} and {upper.name} share height {upper.height}")
        if upper.kbps <= lower.kbps:
            warn(upper.line, f"{upper.name} ({_plain(upper.kbps)} kbps) is not above {lower.name} ({_plain(lower.kbps)} kbps) despite being taller")
        elif upper.kbps > lower.kbps * MAX_RUNG_RATIO:
            ratio = float(upper.kbps / lower.kbps)
            warn(upper.line, f"gap between {lower.name} and {upper.name} is {ratio:.1f}x; consider a rung in between")
    return warnings


# ---------------------------------------------------------------- output

def _plain(value, places=None):
    """Exact Fraction to int when whole, otherwise to a rounded float."""
    if isinstance(value, Fraction):
        if value.denominator == 1:
            return int(value)
        value = float(value)
    if places is not None and isinstance(value, float):
        return round(value, places)
    return value


def _jsonable(value):
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Fraction):
        return _plain(value)
    return value


def _text(value) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _table(rows: list, columns: list) -> str:
    if not rows:
        return "(no rows)"
    cells = [[header for _, header in columns]]
    cells += [[_text(row[key]) for key, _ in columns] for row in rows]
    widths = [max(len(row[i]) for row in cells) for i in range(len(columns))]
    out = []
    for index, row in enumerate(cells):
        out.append("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())
        if index == 0:
            out.append("  ".join("-" * width for width in widths))
    return "\n".join(out)


def _emit(args, data: dict, text: str) -> int:
    if args.json:
        print(json.dumps(_jsonable(data), indent=2))
    else:
        print(text)
    return EXIT_OK


# ---------------------------------------------------------------- commands

def cmd_check(plan, issues, args) -> int:
    warnings = lint(plan) if plan is not None else []
    everything = sorted(issues + warnings, key=lambda i: (i.line or 0, i.severity))
    errors = [i for i in everything if i.severity == "error"]
    warns = [i for i in everything if i.severity == "warning"]
    data = {
        "path": args.file,
        "rungs": len(plan.rungs) if plan else 0,
        "errors": [asdict(i) for i in errors],
        "warnings": [asdict(i) for i in warns],
    }
    lines = [issue.render(args.file) for issue in everything]
    summary = f"{args.file}: {len(errors)} error(s), {len(warns)} warning(s)"
    if not everything:
        summary = f"{args.file}: ladder OK ({len(plan.rungs)} rungs, no warnings)"
    text = "\n".join(lines + [summary]) if lines else summary
    _emit(args, data, text)
    if errors or (args.strict and warns):
        return EXIT_FAIL
    return EXIT_OK


def cmd_sizes(plan, args) -> int:
    rows = []
    total = Fraction(0)
    for rung in plan.rungs:
        size = size_bytes(plan, rung)
        total += size
        rows.append({
            "rung": rung.name,
            "width": rung_width(plan, rung),
            "height": rung.height,
            "kbps": _plain(rung.kbps),
            "mb": _plain(size / MB, 2),
            "gib": _plain(size / GIB, 3),
        })
    data = {"rows": rows, "total_mb": _plain(total / MB, 2), "total_gib": _plain(total / GIB, 3)}
    columns = [("rung", "rung"), ("width", "width"), ("height", "height"), ("kbps", "kbps"), ("mb", "MB"), ("gib", "GiB")]
    text = f"{_table(rows, columns)}\n\nall rungs together: {data['total_mb']} MB ({data['total_gib']} GiB)"
    return _emit(args, data, text)


def cmd_bpp(plan, args) -> int:
    rows = []
    for rung in plan.rungs:
        bpp = bits_per_pixel(plan, rung)
        rows.append({
            "rung": rung.name,
            "width": rung_width(plan, rung),
            "height": rung.height,
            "fps": _plain(rung.fps),
            "bpp": _plain(float(bpp), 4),
            "verdict": verdict(bpp),
        })
    columns = [("rung", "rung"), ("width", "width"), ("height", "height"), ("fps", "fps"), ("bpp", "bits/pixel"), ("verdict", "verdict")]
    return _emit(args, {"rows": rows}, _table(rows, columns))


def cmd_vbv(plan, args) -> int:
    rows = []
    for rung in plan.rungs:
        maxrate, bufsize = vbv(rung)
        rows.append({"rung": rung.name, "kbps": _plain(rung.kbps), "maxrate": maxrate, "bufsize": bufsize})
    columns = [("rung", "rung"), ("kbps", "kbps"), ("maxrate", "maxrate kbps"), ("bufsize", "bufsize kbps")]
    return _emit(args, {"rows": rows}, _table(rows, columns))


def cmd_segments(plan, args) -> int:
    count = segment_count(plan)
    last = plan.duration - (count - 1) * plan.segment
    aligned = (plan.segment / plan.gop).denominator == 1
    rows = []
    for rung in plan.rungs:
        gop_frames = plan.gop * rung.fps
        segment_frames = plan.segment * rung.fps
        rows.append({
            "rung": rung.name,
            "gop_frames": _plain(gop_frames),
            "segment_frames": _plain(segment_frames),
            "aligned": aligned and gop_frames.denominator == 1 and segment_frames.denominator == 1,
        })
    data = {
        "segment_s": _plain(plan.segment),
        "gop_s": _plain(plan.gop),
        "segments": count,
        "last_segment_s": _plain(last),
        "segment_is_whole_gops": aligned,
        "rows": rows,
    }
    header = [
        f"segments: {count} x {_plain(plan.segment)} s, last segment {_plain(last)} s",
        f"gop {_plain(plan.gop)} s: segment {'is' if aligned else 'is NOT'} a whole number of GOPs",
    ]
    columns = [("rung", "rung"), ("gop_frames", "gop frames"), ("segment_frames", "segment frames"), ("aligned", "aligned")]
    return _emit(args, data, "\n".join(header) + "\n\n" + _table(rows, columns))


def cmd_bandwidth(plan, args) -> int:
    rows = []
    for rung in sorted(plan.rungs, key=lambda r: r.kbps):
        need = (rung.kbps + plan.audio_kbps) * plan.headroom / 1000
        rows.append({"rung": rung.name, "kbps": _plain(rung.kbps), "need_mbps": _plain(float(need), 3), "fits": need <= args.mbps})
    fitting = [row for row in rows if row["fits"]]
    best = max(fitting, key=lambda row: row["kbps"])["rung"] if fitting else None
    data = {"available_mbps": _plain(args.mbps), "rows": rows, "best": best}
    columns = [("rung", "rung"), ("kbps", "kbps"), ("need_mbps", "needs Mbps"), ("fits", "fits")]
    if best:
        summary = f"best rung at {_plain(args.mbps)} Mbps: {best}"
    else:
        lowest = min(rows, key=lambda row: row["need_mbps"])
        summary = f"no rung fits {_plain(args.mbps)} Mbps: the lowest ({lowest['rung']}) needs {lowest['need_mbps']} Mbps"
    return _emit(args, data, f"{_table(rows, columns)}\n\n{summary}")


def cmd_target(plan, args) -> int:
    video = video_kbps_for_budget(plan, args.size_mb)
    if video <= 0:
        print(f"ladderkit: a {_plain(args.size_mb)} MB budget cannot cover the audio track", file=sys.stderr)
        return EXIT_FAIL
    floor = math.floor(video)
    data = {
        "size_mb": _plain(args.size_mb),
        "duration_s": _plain(plan.duration),
        "video_kbps": _plain(video, 1),
        "ffmpeg_bitrate_k": floor,
    }
    text = (
        f"{_plain(args.size_mb)} MB over {_plain(plan.duration)} s allows {_plain(video, 1)} kbps of video\n"
        f"use -b:v {floor}k (rounded down so the file stays under budget)"
    )
    return _emit(args, data, text)


def cmd_ffmpeg(plan, args) -> int:
    for rung in plan.rungs:
        print(ffmpeg_command(plan, rung, args.input))
    return EXIT_OK


def cmd_master(plan, args) -> int:
    print(master_playlist(plan), end="")
    return EXIT_OK


COMMANDS = {
    "sizes": cmd_sizes,
    "bpp": cmd_bpp,
    "vbv": cmd_vbv,
    "segments": cmd_segments,
    "bandwidth": cmd_bandwidth,
    "target": cmd_target,
    "ffmpeg": cmd_ffmpeg,
    "master": cmd_master,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ladderkit",
        description="Offline video encoding-ladder planner driven by one plain-text plan file.",
    )
    parser.add_argument("--version", action="version", version=f"ladderkit {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(name, help_text, json_output=True):
        command = sub.add_parser(name, help=help_text, description=help_text)
        command.add_argument("file", help="plan file")
        if json_output:
            command.add_argument("--json", action="store_true", help="print machine-readable JSON")
        return command

    check = add("check", "lint the plan: line-numbered errors and warnings")
    check.add_argument("--strict", action="store_true", help="treat warnings as failures for CI")
    add("sizes", "exact file size per rung and for the whole ladder")
    add("bpp", "bits per pixel per rung with starved/wasteful verdicts")
    add("vbv", "VBV maxrate and buffer size per rung")
    add("segments", "HLS segment count, last segment and GOP alignment")
    bandwidth = add("bandwidth", "highest rung that fits a given connection")
    bandwidth.add_argument("--mbps", required=True, type=parse_number, help="available bandwidth in Mbps")
    target = add("target", "video bitrate that hits a file-size budget")
    target.add_argument("--size-mb", required=True, type=parse_number, help="file budget in decimal MB")
    ffmpeg = add("ffmpeg", "one ffmpeg HLS encode command per rung", json_output=False)
    ffmpeg.add_argument("--input", default="source.mp4", help="source file name (default: source.mp4)")
    add("master", "HLS master playlist for the ladder", json_output=False)
    sub.add_parser("example", help="print a starter plan to stdout")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "example":
        print(EXAMPLE, end="")
        return EXIT_OK

    try:
        text = Path(args.file).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"ladderkit: cannot read {args.file}: {exc.strerror}", file=sys.stderr)
        return EXIT_USAGE

    plan, issues = parse_plan(text)
    if args.command == "check":
        return cmd_check(plan, issues, args)
    if plan is None:
        for issue in issues:
            print(issue.render(args.file), file=sys.stderr)
        return EXIT_FAIL
    if getattr(args, "mbps", None) is not None and args.mbps <= 0:
        print("ladderkit: --mbps must be greater than zero", file=sys.stderr)
        return EXIT_USAGE
    if getattr(args, "size_mb", None) is not None and args.size_mb <= 0:
        print("ladderkit: --size-mb must be greater than zero", file=sys.stderr)
        return EXIT_USAGE
    return COMMANDS[args.command](plan, args)


if __name__ == "__main__":
    sys.exit(main())
