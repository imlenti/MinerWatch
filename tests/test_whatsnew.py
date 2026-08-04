# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the What's-new changelog extraction (backend/whatsnew.py).

Covers the parsing convention (bold bullet leads → dialog highlights),
section isolation between versions, the cap, the single-note release
that is shown in full, the fallback path of get_whatsnew, and — as a
release-time guard — that the *real* CHANGELOG.md still follows the
convention for the version it ships.

Runs under pytest, or standalone: ``python tests/test_whatsnew.py``.
"""
from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backend import whatsnew  # noqa: E402

SAMPLE = """# Changelog

## [Unreleased]

## [2.0.0] — 2026-01-02

### Added

- **First feature — with a dash subtitle.** Long detail sentence one. And a
  second sentence that must not appear.
- plain bullet without a bold lead, must be skipped
- **Second feature.** {long}
- **Third.** Short.
- **Fourth.** Body four.
- **Fifth never shows.** Beyond the cap.

## [1.9.0] — 2025-12-01

### Fixed

- **Old version entry.** Should not leak into 2.0.0.
""".format(long="word " * 80)


def test_extracts_bold_leads_and_first_sentence():
    items = whatsnew.parse_changelog_highlights(SAMPLE, "2.0.0")
    titles = [i["title"] for i in items]
    assert titles[0] == "First feature — with a dash subtitle"
    assert items[0]["body"] == "Long detail sentence one."
    assert "second sentence" not in items[0]["body"]
    assert "Old version entry" not in titles


def test_skips_plain_bullets_caps_at_four_and_truncates():
    items = whatsnew.parse_changelog_highlights(SAMPLE, "2.0.0")
    assert len(items) == whatsnew.MAX_HIGHLIGHTS == 4
    assert all(i["title"] for i in items)
    long_body = items[1]["body"]
    assert len(long_body) <= whatsnew.MAX_BODY_CHARS
    assert long_body.endswith("…")


def test_missing_version_returns_empty():
    assert whatsnew.parse_changelog_highlights(SAMPLE, "3.3.3") == []
    assert whatsnew.parse_changelog_highlights("", "2.0.0") == []


# ---------- single-note release: shown in full ----------
# The teaser rules keep a multi-item dialog from becoming the changelog.
# With one item there is nothing to keep short, and a release whose only
# note is an announcement has to be readable without leaving the app.

SINGLE = """# Changelog

## [3.0.0] — 2026-08-04

- **A short lead.** First sentence of the notice. Second sentence that must
  survive. And a third one, well past the {cap}-character teaser cap, so the
  truncation path would definitely have chopped it. {filler}

## [2.0.0] — 2026-01-02

- **Older entry.** Must not leak.
""".format(cap=whatsnew.MAX_BODY_CHARS, filler="word " * 60)


def test_single_note_release_keeps_the_whole_detail():
    items = whatsnew.parse_changelog_highlights(SINGLE, "3.0.0")
    assert len(items) == 1
    body = items[0]["body"]
    assert body.startswith("First sentence of the notice.")
    assert "Second sentence that must survive." in body
    assert "third one" in body
    assert not body.endswith("…")
    assert len(body) > whatsnew.MAX_BODY_CHARS


def test_single_note_body_is_flattened_to_one_paragraph():
    body = whatsnew.parse_changelog_highlights(SINGLE, "3.0.0")[0]["body"]
    assert "\n" not in body
    assert "  " not in body


def test_multi_note_release_still_gets_teasers():
    """The full-body rule must not leak into ordinary releases."""
    items = whatsnew.parse_changelog_highlights(SAMPLE, "2.0.0")
    assert len(items) > 1
    assert items[0]["body"] == "Long detail sentence one."


def test_get_whatsnew_falls_back_when_changelog_absent(tmp_path=None):
    import tempfile
    original_root = whatsnew.ROOT_DIR
    original_updater = whatsnew.updater
    whatsnew._cache = None
    try:
        whatsnew.ROOT_DIR = pathlib.Path(tempfile.mkdtemp(prefix="mw-nochangelog-"))
        whatsnew.updater = SimpleNamespace(read_version=lambda: "9.9.9")
        out = whatsnew.get_whatsnew()
        assert out["version"] == "9.9.9"
        assert out["highlights"] == [{"title": "Bug fixes and improvements", "body": ""}]
    finally:
        whatsnew.ROOT_DIR = original_root
        whatsnew.updater = original_updater
        whatsnew._cache = None


def test_real_changelog_keeps_the_convention_for_1_11_0():
    """Release-time guard: the section we are about to ship must yield
    real highlights, with the Umbrel widgets headline first."""
    text = (pathlib.Path(__file__).resolve().parents[1] / "CHANGELOG.md").read_text(
        encoding="utf-8"
    )
    items = whatsnew.parse_changelog_highlights(text, "1.11.0")
    assert len(items) == 4
    assert items[0]["title"] == "Umbrel desktop widgets"
    assert all(i["body"] for i in items)


def test_real_changelog_shows_the_1_19_6_notice_in_full():
    """The 1.19.6 dialog carries an announcement, not a feature list, so
    the whole thing has to reach the user — no first-sentence teaser and
    no truncation."""
    text = (pathlib.Path(__file__).resolve().parents[1] / "CHANGELOG.md").read_text(
        encoding="utf-8"
    )
    items = whatsnew.parse_changelog_highlights(text, "1.19.6")
    assert len(items) == 1
    assert items[0]["title"].startswith("IMPORTANT")
    body = items[0]["body"]
    assert body.startswith("No donations have come in")
    assert body.endswith("please give this a thought.")
    assert not body.endswith("…")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)
