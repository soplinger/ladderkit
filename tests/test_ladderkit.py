import contextlib
import io
import json
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ladderkit as lk  # noqa: E402

EXAMPLES = ROOT / "examples"
HARBOUR = EXAMPLES / "harbour.txt"

WARN_PLAN = """\
source: 720 30
duration: 60
segment: 6
gop: 4
rung 720p 720 30 2000
rung 480p 480 30 2000
rung 240p 240 30 100
"""


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = lk.main(argv)
    return code, out.getvalue(), err.getvalue()


def plan_from(text):
    plan, issues = lk.parse_plan(text)
    assert plan is not None, issues
    return plan


class ParsingTests(unittest.TestCase):
    def test_duration_forms(self):
        self.assertEqual(lk.parse_duration("1:12:30"), 4350)
        self.assertEqual(lk.parse_duration("4:30"), 270)
        self.assertEqual(lk.parse_duration("90"), 90)
        self.assertEqual(lk.parse_duration("2.5 m"), 150)
        self.assertEqual(lk.parse_duration("1 h"), 3600)

    def test_duration_rejects_garbage(self):
        with self.assertRaises(ValueError):
            lk.parse_duration("soon")

    def test_bitrate_forms(self):
        self.assertEqual(lk.parse_kbps("3000"), 3000)
        self.assertEqual(lk.parse_kbps("3 Mbps"), 3000)
        self.assertEqual(lk.parse_kbps("2.5m"), 2500)
        self.assertEqual(lk.parse_kbps("128 kbps"), 128)
        with self.assertRaises(ValueError):
            lk.parse_kbps("loud")

    def test_example_plan_parses_cleanly(self):
        plan, issues = lk.parse_plan(HARBOUR.read_text(encoding="utf-8"))
        self.assertEqual(issues, [])
        self.assertEqual(plan.title, "Harbour walk-through")
        self.assertEqual(plan.duration, 4350)
        self.assertEqual([r.name for r in plan.rungs], ["1080p", "720p", "480p", "360p"])

    def test_errors_carry_line_numbers(self):
        text = (
            "title: x\n"
            "source: 1080 25\n"
            "duration: 60\n"
            "bogus: 1\n"
            "rung 720p 720 30 3000\n"
            "rung 720p 480 30 1400\n"
        )
        plan, issues = lk.parse_plan(text)
        self.assertIsNone(plan)
        found = {(i.line, i.message) for i in issues}
        self.assertTrue(any(line == 4 and "unrecognised line" in msg for line, msg in found))
        self.assertTrue(any(line == 6 and "duplicate rung name" in msg for line, msg in found))

    def test_missing_required_key_is_reported(self):
        plan, issues = lk.parse_plan("source: 1080 25\nrung 720p 720 30 3000\n")
        self.assertIsNone(plan)
        self.assertTrue(any(i.line is None and "'duration'" in i.message for i in issues))

    def test_odd_height_and_gop_longer_than_segment_are_errors(self):
        plan, issues = lk.parse_plan("source: 1080 25\nduration: 60\nrung 719p 719 25 3000\n")
        self.assertIsNone(plan)
        self.assertTrue(any("even" in i.message for i in issues))
        plan, issues = lk.parse_plan("source: 1080 25\nduration: 60\nsegment: 2\ngop: 4\nrung 720p 720 25 3000\n")
        self.assertIsNone(plan)
        self.assertTrue(any("gop cannot be longer" in i.message for i in issues))


class MathTests(unittest.TestCase):
    def test_width_follows_aspect_and_stays_even(self):
        plan = plan_from(HARBOUR.read_text(encoding="utf-8"))
        widths = {r.name: lk.rung_width(plan, r) for r in plan.rungs}
        self.assertEqual(widths["1080p"], 1920)
        self.assertEqual(widths["480p"], 854)
        self.assertEqual(widths["360p"], 640)

    def test_size_matches_hand_calculation(self):
        plan = plan_from("source: 720 30\nduration: 60\nrung 720p 720 30 1000\n")
        rung = plan.rungs[0]
        self.assertEqual(lk.size_bytes(plan, rung), 8460000)

    def test_bits_per_pixel_is_exact(self):
        plan = plan_from("source: 720 30\nduration: 60\nrung 720p 720 30 3000\n")
        bpp = lk.bits_per_pixel(plan, plan.rungs[0])
        self.assertEqual(bpp, Fraction(3000000, 1280 * 720 * 30))

    def test_vbv_buffers(self):
        plan = plan_from("source: 720 30\nduration: 60\nrung 720p 720 30 3000\n")
        self.assertEqual(lk.vbv(plan.rungs[0]), (3210, 6000))

    def test_segment_count_and_last_segment(self):
        plan = plan_from("source: 720 30\nduration: 60\nsegment: 6\nrung 720p 720 30 3000\n")
        self.assertEqual(lk.segment_count(plan), 10)
        plan = plan_from("source: 720 30\nduration: 61\nsegment: 6\nrung 720p 720 30 3000\n")
        self.assertEqual(lk.segment_count(plan), 11)
        self.assertEqual(plan.duration - 10 * plan.segment, 1)

    def test_budget_bitrate_subtracts_audio(self):
        plan = plan_from("source: 720 30\nduration: 60\nrung 720p 720 30 3000\n")
        self.assertEqual(lk.video_kbps_for_budget(plan, Fraction("8.46")), 1000)

    def test_ffmpeg_command_carries_rate_control_and_gop(self):
        plan = plan_from(HARBOUR.read_text(encoding="utf-8"))
        rung = next(r for r in plan.rungs if r.name == "720p")
        command = lk.ffmpeg_command(plan, rung)
        self.assertIn("-b:v 3000k -maxrate 3210k -bufsize 6000k", command)
        self.assertIn("-g 60 -keyint_min 60", command)
        self.assertIn("-vf scale=1280:720", command)

    def test_master_playlist_bandwidth_values(self):
        plan = plan_from(HARBOUR.read_text(encoding="utf-8"))
        playlist = lk.master_playlist(plan)
        self.assertTrue(playlist.startswith("#EXTM3U"))
        self.assertIn("BANDWIDTH=3338000,AVERAGE-BANDWIDTH=3128000", playlist)
        self.assertIn("RESOLUTION=1280x720", playlist)


class LintTests(unittest.TestCase):
    def test_example_has_no_warnings(self):
        plan = plan_from(HARBOUR.read_text(encoding="utf-8"))
        self.assertEqual(lk.lint(plan), [])

    def test_lint_flags_alignment_order_and_gaps(self):
        messages = [w.message for w in lk.lint(plan_from(WARN_PLAN))]
        self.assertTrue(any("not a whole number of GOPs" in m for m in messages))
        self.assertTrue(any("720p (2000 kbps) is not above 480p" in m for m in messages))
        self.assertTrue(any("gap between 240p and 480p" in m for m in messages))

    def test_lint_flags_rung_taller_than_source(self):
        plan = plan_from("source: 720 30\nduration: 60\nrung 1080p 1080 30 6000\n")
        messages = [w.message for w in lk.lint(plan)]
        self.assertTrue(any("is taller than the 720p source" in m for m in messages))


class CliTests(unittest.TestCase):
    def test_check_passes_on_example(self):
        code, out, _ = run(["check", str(HARBOUR)])
        self.assertEqual(code, 0)
        self.assertIn("ladder OK", out)

    def test_check_strict_fails_on_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "warn.txt"
            path.write_text(WARN_PLAN, encoding="utf-8")
            self.assertEqual(run(["check", str(path)])[0], 0)
            self.assertEqual(run(["check", "--strict", str(path)])[0], 1)

    def test_check_fails_on_errors_with_line_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.txt"
            path.write_text("source: 1080 25\nduration: 60\nbogus: 1\nrung 720p 720 30 3000\n", encoding="utf-8")
            code, out, _ = run(["check", str(path)])
            self.assertEqual(code, 1)
            self.assertIn(f"{path}:3: error: unrecognised line", out)

    def test_missing_file_is_usage_error(self):
        code, _, err = run(["sizes", str(ROOT / "no-such-plan.txt")])
        self.assertEqual(code, 2)
        self.assertIn("cannot read", err)

    def test_json_output_round_trips(self):
        code, out, _ = run(["sizes", str(HARBOUR), "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(len(data["rows"]), 4)
        self.assertIsInstance(data["total_mb"], float)

    def test_bandwidth_picks_highest_fitting_rung(self):
        code, out, _ = run(["bandwidth", str(HARBOUR), "--mbps", "5", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["best"], "720p")

    def test_bandwidth_rejects_zero(self):
        code, _, err = run(["bandwidth", str(HARBOUR), "--mbps", "0"])
        self.assertEqual(code, 2)
        self.assertIn("greater than zero", err)

    def test_target_rounds_down_to_stay_under_budget(self):
        code, out, _ = run(["target", str(HARBOUR), "--size-mb", "500", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["ffmpeg_bitrate_k"], 791)

    def test_ffmpeg_prints_one_line_per_rung(self):
        code, out, _ = run(["ffmpeg", str(HARBOUR)])
        lines = out.strip().splitlines()
        self.assertEqual(code, 0)
        self.assertEqual(len(lines), 4)
        self.assertTrue(all(line.startswith("ffmpeg -i source.mp4") for line in lines))

    def test_example_command_prints_starter_plan(self):
        code, out, _ = run(["example"])
        self.assertEqual(code, 0)
        self.assertIn("rung 720p 720 30 3000", out)


if __name__ == "__main__":
    unittest.main()
