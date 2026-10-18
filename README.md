# ladderkit

**Plan your video encoding ladder offline: exact file sizes, bits per pixel, HLS segment alignment, bandwidth fit and ffmpeg commands, all from one plain-text file.**

ladderkit reads a small plain-text plan (source format, duration, segment and GOP lengths, and one line per rendition) and tells you whether the ladder is sane before you spend hours encoding. It is a single Python file that uses only the standard library. It makes no network calls, needs no accounts or API keys, and writes nothing except what you ask it to print.

## Features

- **Plain-text plans** with `key: value` settings and `rung NAME HEIGHT FPS KBPS` lines, including comments and friendly units (`1:12:30`, `6 s`, `3.5 Mbps`, `16:9`).
- **Line-numbered errors**: unknown keys, duplicate keys, duplicate rung names, bad numbers, odd heights and impossible GOP/segment pairs are reported as `file:line: error: ...`.
- **Ladder lint** with warnings for rungs taller or faster than the source, bitrates that do not rise with height, rung gaps wider than 2.5x, starved or wasteful bits per pixel, and segments that are not a whole number of GOPs.
- **CI gate**: `check` exits `0` when the plan is clean, `1` on errors, and `1` on warnings too with `--strict`.
- **Exact file sizes** per rung and for the whole ladder, in decimal MB and GiB, computed with exact fractions.
- **Bits per pixel** for every rung, with `ok`, `starved` and `wasteful` verdicts.
- **VBV buffers**: `maxrate` (107% of target) and `bufsize` (2x target) for every rung.
- **Segment and GOP report**: segment count, last-segment length, keyframe frames per GOP and whether HLS cuts land on keyframes.
- **Bandwidth fit**: the highest rung whose needed bitrate, including your headroom factor, fits a given Mbps connection.
- **File-budget solver**: the video bitrate that keeps a whole encode under a size budget, rounded down so the file stays under it.
- **ffmpeg commands**: one ready-to-run HLS encode line per rung with `-b:v`, `-maxrate`, `-bufsize`, scaling, fixed GOP and `-hls_time`.
- **HLS master playlist** text with peak `BANDWIDTH`, `AVERAGE-BANDWIDTH`, `RESOLUTION`, `FRAME-RATE` and `CODECS`.
- **JSON output** via `--json` on every report command, for scripts and dashboards.
- **Starter plan** printed by `example`, so the first run needs no file to write by hand.

## Quick start

Print a starter plan, save it, and check it:

```sh
python3 ladderkit.py example > harbour.txt
python3 ladderkit.py check harbour.txt
```

A sample plan is included in `examples/harbour.txt`. The plan looks like this:

```text
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
```

Then ask the questions you care about:

```sh
python3 ladderkit.py sizes examples/harbour.txt            # file size per rung and total
python3 ladderkit.py bpp examples/harbour.txt              # bits per pixel per rung
python3 ladderkit.py segments examples/harbour.txt         # HLS segment and GOP alignment
python3 ladderkit.py bandwidth examples/harbour.txt --mbps 5
python3 ladderkit.py target examples/harbour.txt --size-mb 500
python3 ladderkit.py ffmpeg examples/harbour.txt --input talk.mov > encode.sh
python3 ladderkit.py master examples/harbour.txt > master.m3u8
```

Add `--json` to `sizes`, `bpp`, `vbv`, `segments`, `bandwidth`, `target` or `check` for machine-readable output.

A deliberately broken plan shows the error format:

```sh
python3 ladderkit.py check examples/broken.txt
```

## Plan reference

| Key | Default | Meaning |
| --- | --- | --- |
| `title` | `Untitled` | Label for the plan |
| `source` | required | `HEIGHT FPS` of the master file, e.g. `2160 50` |
| `duration` | required | Length of the programme: `4350`, `72.5 m`, `1:12:30` |
| `segment` | `6 s` | HLS segment length |
| `gop` | `2 s` | Keyframe interval; must not exceed `segment` |
| `audio` | `128 kbps` | Audio bitrate, added to every rung's size |
| `aspect` | `16:9` | Used to derive even widths |
| `headroom` | `1.25` | Bandwidth safety factor, at least 1 |

Rungs are written as `rung NAME HEIGHT FPS KBPS`, for example `rung 720p 720 30 3000`. Names may use letters, digits, `-` and `_`.

## How to run

ladderkit needs Python 3.8 or newer and nothing else.

```sh
python3 ladderkit.py --help
python3 ladderkit.py COMMAND --help
```

Exit codes:

| Code | Meaning |
| --- | --- |
| `0` | Success, or a clean `check` |
| `1` | The plan has errors, or `check --strict` found warnings |
| `2` | Usage error: unreadable file, bad `--mbps` or `--size-mb` value |

## How to test

The tests use the standard `unittest` module, so they run without installing anything:

```sh
python3 -m unittest discover -s tests -v
```

If you have pytest installed, it collects the same tests:

```sh
python3 -m pytest -q tests
```

## Project layout

```text
ladderkit.py          the whole tool: parser, maths, lint and CLI
examples/harbour.txt  a clean four-rung plan
examples/broken.txt   a plan with deliberate errors
tests/test_ladderkit.py
```

## Notes on the maths

- Sizes are `(video kbps + audio kbps) x 1000 x duration / 8` bytes, using decimal MB (10^6 bytes) and GiB (2^30 bytes).
- Bits per pixel are `video bits per second / (width x height x fps)`.
- Maximum rate is 107% of the target and buffer size is 2x the target, the common x264 HLS defaults. Adjust them in `ladderkit.py` if your encoder settings differ.
- Bandwidth fit uses `(video kbps + audio kbps) x headroom`.
- Widths are derived from the aspect ratio and rounded to the nearest even number.
