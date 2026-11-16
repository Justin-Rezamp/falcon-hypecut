"""XbotGo Falcon .mark parsing and the marks-only plan (runner rules)."""

from __future__ import annotations

import json

import numpy as np
import pytest

from hypecut.marks import (
    MarkFileError,
    find_mark_file,
    fmt_clock,
    parse_marks,
    plan_from_marks,
    read_marks,
    slug_clock,
)
from hypecut.signals.marks import mark_bumps

FALCON_SAMPLE = [
    {"type": "start", "time": 0},
    {"type": "click", "time": 549.0},
    {"type": "click", "time": 1210.5},
]


# --------------------------------------------------------------- parsing


def test_only_clicks_count_and_start_is_ignored():
    marks = parse_marks(FALCON_SAMPLE)
    assert marks.clicks == [549.0, 1210.5]
    assert marks.ignored == []


def test_clicks_are_sorted_and_deduplicated():
    data = [{"type": "click", "time": t} for t in (30, 10, 30.0, "20")]
    assert parse_marks(data).clicks == [10.0, 20.0, 30.0]


def test_bad_clicks_are_reported_not_invented():
    data = [
        {"type": "click"},
        {"type": "click", "time": "soon"},
        {"type": "click", "time": -4},
        {"type": "click", "time": float("nan")},
        {"type": "click", "time": 9999},
        "garbage",
        {"type": "click", "time": 12},
    ]
    marks = parse_marks(data, duration=600)
    assert marks.clicks == [12.0]
    assert len(marks.ignored) == 6
    assert any("after the video ends" in why for why in marks.ignored)


def test_wrapper_object_is_tolerated_and_other_shapes_are_not():
    assert parse_marks({"marks": FALCON_SAMPLE}).clicks == [549.0, 1210.5]
    with pytest.raises(MarkFileError):
        parse_marks({"type": "click", "time": 3})
    with pytest.raises(MarkFileError):
        parse_marks("nope")


def test_read_marks_reports_broken_json(tmp_path):
    bad = tmp_path / "game.mark"
    bad.write_text("[{type: click}", encoding="utf-8")
    with pytest.raises(MarkFileError, match="not valid JSON"):
        read_marks(bad)


def test_read_marks_handles_a_bom(tmp_path):
    path = tmp_path / "game.mark"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(FALCON_SAMPLE).encode())
    assert read_marks(path).clicks == [549.0, 1210.5]


@pytest.mark.parametrize("name", ["game.mark", "game.mp4.mark", "GAME.MARK"])
def test_sibling_mark_file_is_found(tmp_path, name):
    video = tmp_path / "game.mp4"
    video.write_bytes(b"")
    (tmp_path / name).write_text("[]", encoding="utf-8")
    found = find_mark_file(video)
    assert found is not None and found.name == name


def test_no_sibling_means_none(tmp_path):
    video = tmp_path / "game.mp4"
    video.write_bytes(b"")
    assert find_mark_file(video) is None


# ------------------------------------------------------------- planning


def test_runner_pad_and_naming():
    (clip,) = plan_from_marks([549.0], duration=3000)
    assert (clip.id, clip.filename) == ("clip01", "clip01_mark_9-09.mp4")
    assert (clip.start, clip.end) == (539.0, 559.0)
    assert clip.kind == "mark" and clip.marks == [549.0]


def test_windows_within_two_seconds_merge():
    # 100 -> [90,110]; 122 -> [112,132]: a 2 s gap merges (runner: start <= end + 2).
    merged = plan_from_marks([100, 122], duration=3000)
    assert len(merged) == 1
    assert merged[0].filename == "clip01_mark_1-40_merged.mp4"
    assert (merged[0].start, merged[0].end) == (90.0, 132.0)
    assert merged[0].marks == [100.0, 122.0]
    # 100 -> [90,110]; 123 -> [113,133]: a 3 s gap stays apart.
    apart = plan_from_marks([100, 123], duration=3000)
    assert [c.filename for c in apart] == ["clip01_mark_1-40.mp4", "clip02_mark_2-03.mp4"]


def test_overlaps_chain_into_one_clip():
    clips = plan_from_marks([60, 70, 80, 300], duration=3000)
    assert [c.filename for c in clips] == ["clip01_mark_1-00_merged.mp4", "clip02_mark_5-00.mp4"]
    assert clips[0].marks == [60.0, 70.0, 80.0]
    assert clips[0].note == "1:00 + 1:10 + 1:20"


def test_windows_clamp_to_the_video():
    clips = plan_from_marks([3, 595], duration=600)
    assert (clips[0].start, clips[0].end) == (0.0, 13.0)
    assert (clips[1].start, clips[1].end) == (585.0, 600.0)


def test_custom_pad_and_gap():
    clips = plan_from_marks([100, 130], duration=3000, before=15, after=5, merge_gap=0)
    assert [(c.start, c.end) for c in clips] == [(85.0, 105.0), (115.0, 135.0)]
    clips = plan_from_marks([100, 130], duration=3000, before=15, after=5, merge_gap=10)
    assert len(clips) == 1


def test_clip_ids_follow_time_order():
    clips = plan_from_marks([900, 100, 500], duration=3000)
    assert [c.id for c in clips] == ["clip01", "clip02", "clip03"]
    assert [c.marks[0] for c in clips] == [100.0, 500.0, 900.0]


@pytest.mark.parametrize(
    ("seconds", "slug"),
    [(549, "9-09"), (5, "0-05"), (600, "10-00"), (3725, "1-02-05"), (59.6, "1-00"), (61.4, "1-01")],
)
def test_slug_clock(seconds, slug):
    assert slug_clock(seconds) == slug


def test_fmt_clock_is_for_people():
    assert fmt_clock(549) == "9:09"
    assert fmt_clock(549.5) == "9:09.5"
    assert fmt_clock(3725) == "1:02:05"


def test_round_trip_through_dict():
    (clip,) = plan_from_marks([549.0], duration=3000)
    from hypecut.marks import FalconClip

    again = FalconClip.from_dict(clip.to_dict())
    assert again.filename == clip.filename and again.start == clip.start


# ---------------------------------------------------------------- signal


def test_mark_bump_leans_before_the_tap():
    times = np.arange(0, 60, 0.1)
    bump = mark_bumps(times, [30.0], lead=8, trail=2)
    assert bump[np.searchsorted(times, 30.0)] == pytest.approx(1.0)
    assert bump[np.searchsorted(times, 26.0)] > 0.4  # 4 s before: still strong
    assert bump[np.searchsorted(times, 33.0)] == 0.0  # 3 s after: gone
    assert bump[np.searchsorted(times, 10.0)] == 0.0
