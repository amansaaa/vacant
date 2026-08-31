"""Probe the TTB endpoint and report what it actually returns.

This is the measurement half of Task 3. It samples pages from each division and
counts what the product depends on.

It measures two different things, for two different reasons:

1. **Room-number coverage.** Still tracked, because
   if it ever moves off zero, room-level features become possible again.
2. **Whether the building-level metric is computable.** Enrolment coverage (the
   headcount estimate depends on it) and `peak_concurrent` per building (the
   denominator of `load_pct`). If either were missing, the current design would
   not work either.

The `peak_concurrent` sweep line here is *measurement* code. The production
version lives in the Task 9 loader, which computes it over the full scrape
rather than a sample.

Usage:
    python -m vacant_scraper.probe                 # sample every division
    python -m vacant_scraper.probe --pages 10      # deeper sample
    python -m vacant_scraper.probe --division ARTSC --division APSC
"""

from __future__ import annotations

import argparse
import collections
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

from vacant_scraper.ttb import (
    DIVISIONS,
    SESSIONS_2026_27,
    TTBClient,
)

logger = logging.getLogger("vacant_scraper.probe")

# .../vacant/scraper/src/vacant_scraper/probe.py -> .../vacant
REPO_ROOT = Path(__file__).resolve().parents[3]

# buildingUrl looks like:
#   https://map.utoronto.ca/?id=1809#!m/494547
# The ?id= is the CAMPUS (1809 = St. George) and is identical for every
# building. The per-building identifier is the number after the "#!m/".
MAP_MARKER_RE = re.compile(r"#!m/(\d+)")


def marker_id(building_url: str | None) -> str | None:
    """Extract the per-building map marker id from a buildingUrl."""
    if not building_url:
        return None
    match = MAP_MARKER_RE.search(building_url)
    return match.group(1) if match else None


class Counters:
    """Everything we tally over the sampled courses."""

    def __init__(self) -> None:
        self.courses = 0
        self.sections = 0
        self.cancelled_sections = 0
        self.meetings = 0
        self.meetings_with_room = 0
        self.meetings_with_building = 0
        self.meetings_missing_building = 0
        self.days: collections.Counter[Any] = collections.Counter()
        self.delivery_modes: collections.Counter[str] = collections.Counter()
        self.repetitions: collections.Counter[str] = collections.Counter()
        self.session_codes: collections.Counter[str] = collections.Counter()
        self.campuses: collections.Counter[str] = collections.Counter()
        # building code -> marker id(s) seen for it
        self.building_markers: dict[str, set[str]] = collections.defaultdict(set)
        self.millis_not_on_minute = 0
        self.sample_room_strings: list[str] = []
        self.sections_with_enrolment = 0
        # (building, day, start_min, end_min, enrolment) for every usable
        # meeting, so we can measure concurrency after sampling.
        self.activity_rows: list[tuple[str, int, int, int, int]] = []

    def observe_course(self, course: dict[str, Any]) -> None:
        self.courses += 1
        self.campuses[str(course.get("campus"))] += 1
        for section in course.get("sections") or []:
            self.sections += 1
            if section.get("cancelInd") == "Y":
                self.cancelled_sections += 1
            if section.get("currentEnrolment") is not None:
                self.sections_with_enrolment += 1
            for mode in section.get("deliveryModes") or []:
                self.delivery_modes[str(mode.get("mode"))] += 1
            for meeting in section.get("meetingTimes") or []:
                self.observe_meeting(meeting, section)

    def observe_meeting(
        self, meeting: dict[str, Any], section: dict[str, Any] | None = None
    ) -> None:
        self.meetings += 1
        self.repetitions[str(meeting.get("repetition"))] += 1
        self.session_codes[str(meeting.get("sessionCode"))] += 1

        start = meeting.get("start") or {}
        end = meeting.get("end") or {}
        self.days[start.get("day")] += 1
        for millis in (start.get("millisofday"), end.get("millisofday")):
            if millis is None or millis % 60_000 != 0:
                self.millis_not_on_minute += 1

        building = meeting.get("building") or {}
        code = (building.get("buildingCode") or "").strip()
        if code:
            self.meetings_with_building += 1
            marker = marker_id(building.get("buildingUrl"))
            if marker:
                self.building_markers[code].add(marker)
        else:
            self.meetings_missing_building += 1

        # Everything the building-level metric needs. Fall session only, so a
        # full-year course is not double-counted against itself.
        if (
            code
            and section is not None
            and section.get("cancelInd") != "Y"
            and meeting.get("sessionCode") == "20269"
            and start.get("day") is not None
            and start.get("millisofday") is not None
            and end.get("millisofday") is not None
        ):
            self.activity_rows.append((
                code,
                start["day"],
                start["millisofday"] // 60_000,
                end["millisofday"] // 60_000,
                section.get("currentEnrolment") or 0,
            ))

        room = (building.get("buildingRoomNumber") or "").strip()
        suffix = (building.get("buildingRoomSuffix") or "").strip()
        if room:
            self.meetings_with_room += 1
            if len(self.sample_room_strings) < 20:
                self.sample_room_strings.append(f"{code} {room}{suffix}".strip())

    @property
    def room_coverage_pct(self) -> float:
        if not self.meetings:
            return 0.0
        return 100.0 * self.meetings_with_room / self.meetings

    @property
    def enrolment_coverage_pct(self) -> float:
        if not self.sections:
            return 0.0
        return 100.0 * self.sections_with_enrolment / self.sections


def peak_concurrent(
    rows: list[tuple[str, int, int, int, int]]
) -> dict[str, int]:
    """Max simultaneous meetings per building, across the whole week.

    Sweep line: sort the start/end events for one (building, day), add 1 on a
    start, subtract 1 on an end, and track the running maximum.

    Intervals are half-open [start, end) per CLAUDE.md §6, so a class ending at
    11:00 and one starting at 11:00 never overlap. That is enforced by sorting
    ends before starts at equal minutes — the `0` vs `1` in the sort key below.
    """
    events: dict[tuple[str, int], list[tuple[int, int]]] = collections.defaultdict(list)
    for building, day, start, end, _enrolment in rows:
        if end <= start:
            continue  # defensive: a zero-or-negative-length meeting is not real
        events[(building, day)].append((start, 1))
        events[(building, day)].append((end, 0))

    peaks: collections.Counter[str] = collections.Counter()
    for (building, _day), day_events in events.items():
        # (minute, kind) with kind 0 = end, 1 = start; ends sort first.
        day_events.sort()
        running = 0
        for _minute, kind in day_events:
            running += 1 if kind else -1
            peaks[building] = max(peaks[building], running)
    return dict(peaks)


def activity_at(
    rows: list[tuple[str, int, int, int, int]], day: int, minute: int
) -> tuple[dict[str, int], dict[str, int]]:
    """Classes running and people in class, per building, at one minute."""
    classes: collections.Counter[str] = collections.Counter()
    people: collections.Counter[str] = collections.Counter()
    for building, row_day, start, end, enrolment in rows:
        if row_day == day and start <= minute < end:
            classes[building] += 1
            people[building] += enrolment
    return dict(classes), dict(people)


def probe_division(
    client: TTBClient, division: str, sessions: list[str], max_pages: int
) -> tuple[Counters, int, dict[str, Any] | None]:
    """Sample `max_pages` pages of one division. Returns counters and total."""
    counters = Counters()
    total = 0
    first_course: dict[str, Any] | None = None

    for page in client.iter_pages(
        divisions=[division], sessions=sessions, max_pages=max_pages
    ):
        total = page.total
        for course in page.courses:
            if first_course is None:
                first_course = course
            counters.observe_course(course)
        logger.info(
            "%s: page %s/%s (%s courses, total %s)",
            division, page.page, page.last_page, len(page.courses), page.total,
        )

    return counters, total, first_course


def _activity_report(rows: list[tuple[str, int, int, int, int]]) -> dict[str, Any]:
    """Evidence that the building-level metric is computable from this data.

    Everything here is measured on a *sample*, so the peaks are lower bounds on
    what a full scrape will show. It exists to prove the metric works, not to
    produce production numbers.
    """
    peaks = peak_concurrent(rows)
    # Monday 10:30 — inside the 10:50 rush the project is designed around.
    classes, people = activity_at(rows, day=1, minute=630)

    snapshot = []
    for building in sorted(peaks):
        peak = peaks[building]
        now = classes.get(building, 0)
        snapshot.append({
            "building_code": building,
            "classes_now": now,
            "people_now": people.get(building, 0),
            "peak_concurrent": peak,
            "load_pct": round(100.0 * now / peak, 1) if peak else 0.0,
        })
    snapshot.sort(key=lambda row: (row["load_pct"], -row["peak_concurrent"]))

    return {
        "note": (
            "Sampled, not exhaustive: peak_concurrent is a lower bound and will "
            "rise with a full scrape. Fall session (20269) meetings only."
        ),
        "meetings_considered": len(rows),
        "buildings_seen": len(peaks),
        "sample_minute": "Monday 10:30",
        "snapshot": snapshot,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--division", action="append", choices=sorted(DIVISIONS),
        help="division to probe (repeatable; default: all)",
    )
    parser.add_argument(
        "--pages", type=int, default=3,
        help="pages to sample per division (default: 3, i.e. 60 courses)",
    )
    parser.add_argument(
        "--delay", type=float, default=1.0,
        help="seconds to wait between requests (default: 1.0)",
    )
    parser.add_argument(
        "--out", type=Path,
        default=REPO_ROOT / "docs" / "ttb-probe.json",
        help="where to write the machine-readable probe result",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    divisions = args.division or sorted(DIVISIONS)
    overall = Counters()
    per_division: dict[str, Any] = {}
    sample_course: dict[str, Any] | None = None

    with TTBClient(delay_seconds=args.delay) as client:
        for division in divisions:
            counters, total, first_course = probe_division(
                client, division, SESSIONS_2026_27, args.pages
            )
            if sample_course is None and first_course is not None:
                sample_course = first_course

            per_division[division] = {
                "name": DIVISIONS[division],
                "total_courses_reported": total,
                "courses_sampled": counters.courses,
                "sections_sampled": counters.sections,
                "meetings_sampled": counters.meetings,
                "meetings_with_building_code": counters.meetings_with_building,
                "meetings_with_room_number": counters.meetings_with_room,
                "room_coverage_pct": round(counters.room_coverage_pct, 1),
                "enrolment_coverage_pct": round(counters.enrolment_coverage_pct, 1),
            }

            # Fold into the overall tally.
            overall.courses += counters.courses
            overall.sections += counters.sections
            overall.cancelled_sections += counters.cancelled_sections
            overall.meetings += counters.meetings
            overall.meetings_with_room += counters.meetings_with_room
            overall.meetings_with_building += counters.meetings_with_building
            overall.meetings_missing_building += counters.meetings_missing_building
            overall.millis_not_on_minute += counters.millis_not_on_minute
            overall.sections_with_enrolment += counters.sections_with_enrolment
            overall.activity_rows.extend(counters.activity_rows)
            overall.days.update(counters.days)
            overall.delivery_modes.update(counters.delivery_modes)
            overall.repetitions.update(counters.repetitions)
            overall.session_codes.update(counters.session_codes)
            overall.campuses.update(counters.campuses)
            for code, markers in counters.building_markers.items():
                overall.building_markers[code].update(markers)
            overall.sample_room_strings.extend(
                counters.sample_room_strings[
                    : max(0, 20 - len(overall.sample_room_strings))
                ]
            )

    result = {
        "pages_sampled_per_division": args.pages,
        "sessions": SESSIONS_2026_27,
        "per_division": per_division,
        "overall": {
            "courses_sampled": overall.courses,
            "sections_sampled": overall.sections,
            "cancelled_sections": overall.cancelled_sections,
            "meetings_sampled": overall.meetings,
            "meetings_with_building_code": overall.meetings_with_building,
            "meetings_missing_building_code": overall.meetings_missing_building,
            "meetings_with_room_number": overall.meetings_with_room,
            "room_coverage_pct": round(overall.room_coverage_pct, 1),
            "start_end_millis_not_on_minute_boundary": overall.millis_not_on_minute,
            "day_values": dict(sorted(overall.days.items(), key=lambda kv: (kv[0] is None, kv[0]))),
            "delivery_modes": dict(overall.delivery_modes.most_common()),
            "repetition_values": dict(overall.repetitions.most_common()),
            "session_codes_on_meetings": dict(overall.session_codes.most_common()),
            "campus_values": dict(overall.campuses.most_common()),
            "building_code_to_map_markers": {
                code: sorted(markers)
                for code, markers in sorted(overall.building_markers.items())
            },
            "sample_room_strings": overall.sample_room_strings,
            "sections_with_enrolment": overall.sections_with_enrolment,
            "enrolment_coverage_pct": round(overall.enrolment_coverage_pct, 1),
        },
        "building_activity": _activity_report(overall.activity_rows),
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n")

    # One real course, kept verbatim, so docs/ttb-api.md can point at a
    # payload nobody has to re-fetch to read.
    if sample_course is not None:
        sample_path = args.out.with_name("ttb-sample-course.json")
        sample_path.write_text(json.dumps(sample_course, indent=2) + "\n")
        print(f"wrote {sample_path}")
    print(json.dumps(result["overall"], indent=2))
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
