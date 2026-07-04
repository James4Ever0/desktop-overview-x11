#!/usr/bin/env python3
"""tests/test_timeline_filter.py — pure unit tests for the lane title filter helpers.

Run: python -m tests.test_timeline_filter
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from frontend.views import _parse_lane_filter, _lane_title_matches  # noqa: E402


class _FakeLane:
    def __init__(self, title):
        self.current_title = title


def test_parse_filter_tokens():
    assert _parse_lane_filter("abc def") == ["abc", "def"]
    assert _parse_lane_filter("  ABC   DEF  ") == ["abc", "def"]
    assert _parse_lane_filter("") == []
    assert _parse_lane_filter("   ") == []
    assert _parse_lane_filter("one") == ["one"]


def test_lane_matches_and_semantics():
    lane = _FakeLane("Terminal — refactor : python")
    assert _lane_title_matches(lane, [])
    assert _lane_title_matches(lane, ["terminal"])
    assert _lane_title_matches(lane, ["refactor", "python"])
    assert not _lane_title_matches(lane, ["refactor", "missing"])
    assert not _lane_title_matches(lane, ["nomatch"])


def test_lane_matches_case_insensitive():
    lane = _FakeLane("Firefox — GitHub")
    assert _lane_title_matches(lane, ["firefox"])
    assert _lane_title_matches(lane, ["github"])


def test_lane_empty_title():
    lane = _FakeLane(None)
    assert _lane_title_matches(lane, [])
    assert not _lane_title_matches(lane, ["anything"])


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as exc:
                print(f"  FAIL  {name}: {exc}")
                fails += 1
    print(f"\n{'ALL PASS' if fails == 0 else f'{fails} FAILED'}")
    sys.exit(1 if fails else 0)
