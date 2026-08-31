"""Tests for the probe's measurement helpers.

The sweep line and the point-in-time count both hinge on one convention, fixed
in CLAUDE.md §6: intervals are **half-open**, `[start, end)`. A class ending at
11:00 does not occupy the building at 11:00. Every test here exists to pin that
down, because it is the likeliest silent bug in the whole project.

Rows are (building, day, start_min, end_min, enrolment).
"""

from __future__ import annotations

from vacant_scraper.probe import activity_at, marker_id, peak_concurrent


# --- map marker extraction ------------------------------------------------


def test_marker_id_takes_the_fragment_not_the_query():
    """?id= is the campus and is the same everywhere; #!m/ is the building."""
    assert marker_id("https://map.utoronto.ca/?id=1809#!m/494547") == "494547"
    assert marker_id("https://map.utoronto.ca/?id=1809#!m/494490") == "494490"


def test_marker_id_handles_missing_or_malformed_urls():
    assert marker_id(None) is None
    assert marker_id("") is None
    assert marker_id("https://map.utoronto.ca/?id=1809") is None


# --- peak concurrency -----------------------------------------------------


def test_touching_intervals_do_not_overlap():
    """10:00-11:00 and 11:00-12:00 is a peak of 1, not 2.

    This is the case that would silently inflate every building's capacity and
    make everywhere look quieter than it is.
    """
    rows = [
        ("BA", 1, 600, 660, 100),
        ("BA", 1, 660, 720, 100),
    ]
    assert peak_concurrent(rows) == {"BA": 1}


def test_genuinely_overlapping_intervals_stack():
    rows = [
        ("BA", 1, 600, 720, 10),
        ("BA", 1, 630, 690, 10),
        ("BA", 1, 660, 780, 10),
    ]
    # At 660: first (600-720) and second (630-690) and third all overlap.
    assert peak_concurrent(rows) == {"BA": 3}


def test_peak_is_the_max_across_the_whole_week_not_one_day():
    rows = [
        ("BA", 1, 600, 660, 0),                        # Monday: 1
        ("BA", 3, 600, 660, 0), ("BA", 3, 610, 670, 0),  # Wednesday: 2
    ]
    assert peak_concurrent(rows) == {"BA": 2}


def test_buildings_are_counted_independently():
    rows = [
        ("BA", 1, 600, 660, 0), ("BA", 1, 610, 670, 0),
        ("GB", 1, 600, 660, 0),
    ]
    assert peak_concurrent(rows) == {"BA": 2, "GB": 1}


def test_zero_length_and_inverted_meetings_are_ignored():
    """Defensive: bad data must not corrupt the denominator."""
    rows = [
        ("BA", 1, 600, 600, 0),   # zero length
        ("BA", 1, 700, 650, 0),   # end before start
        ("BA", 1, 800, 860, 0),   # the only real one
    ]
    assert peak_concurrent(rows) == {"BA": 1}


def test_no_rows_gives_no_peaks():
    assert peak_concurrent([]) == {}


# --- point-in-time activity ----------------------------------------------


def test_query_on_a_start_minute_counts():
    rows = [("BA", 1, 600, 660, 50)]
    classes, people = activity_at(rows, day=1, minute=600)
    assert classes == {"BA": 1}
    assert people == {"BA": 50}


def test_query_on_an_end_minute_does_not_count():
    """The half-open convention, from the other side."""
    rows = [("BA", 1, 600, 660, 50)]
    classes, people = activity_at(rows, day=1, minute=660)
    assert classes == {}
    assert people == {}


def test_wrong_day_does_not_count():
    rows = [("BA", 1, 600, 660, 50)]
    assert activity_at(rows, day=2, minute=630) == ({}, {})


def test_people_sums_across_concurrent_meetings():
    rows = [
        ("BA", 1, 600, 660, 250),
        ("BA", 1, 610, 670, 40),
        ("GB", 1, 600, 660, 15),
    ]
    classes, people = activity_at(rows, day=1, minute=630)
    assert classes == {"BA": 2, "GB": 1}
    assert people == {"BA": 290, "GB": 15}


def test_missing_enrolment_counts_as_zero_people_but_still_a_class():
    """A class with no enrolment figure still occupies a room."""
    rows = [("BA", 1, 600, 660, 0)]
    classes, people = activity_at(rows, day=1, minute=630)
    assert classes == {"BA": 1}
    assert people == {"BA": 0}
