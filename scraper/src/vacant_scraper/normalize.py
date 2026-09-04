"""Raw TTB scrape -> `data/meetings.json` (Task 6).

The highest-bug-density piece of this project (CLAUDE.md §10) because the raw
data is messy in ways that are easy to get subtly wrong: ~5% of meetings carry
no building at all (TBA), some sections are cancelled, some are online-only,
and a full-year course legitimately produces two meeting rows for the same
timetable slot (one per term session) that must NOT be treated as duplicates.

This module does not talk to the network or touch Postgres. It reads
`scraper/raw/*.json` (written by `vacant_scraper.scrape`) and `data/
buildings.json` (written by `vacant_scraper.extract_buildings`), and writes
three JSON artifacts: the normalized meetings themselves, a categorized report
of everything dropped, and a dated row appended to the room-coverage history.

Every meeting is classified into exactly one bucket: kept, or one of five drop
categories (CLAUDE.md §10): `online_or_async`, `not_st_george`,
`cancelled_section`, `unknown_building_code`, `unparseable_time`. A meeting
with an empty room number is KEPT, not dropped -- that is the expected case
today (0% room coverage), not a data-quality problem.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

TORONTO = ZoneInfo("America/Toronto")

DROP_CATEGORIES = (
    "online_or_async",
    "not_st_george",
    "cancelled_section",
    "unknown_building_code",
    "unparseable_time",
)

ONLINE_MODES = {"SYNC", "ASYNC"}
VALID_DAYS = {1, 2, 3, 4, 5}
SAMPLES_PER_CATEGORY = 5


def write_json_atomic(path: Path, data: Any) -> None:
    """Write via a temp file + rename, so a killed process never leaves a
    truncated file under the final name. Duplicated from `scrape.py` rather
    than shared -- each module in this codebase is self-contained.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


# --- pure transforms ------------------------------------------------------


def parse_marker_id(building_url: str | None) -> str | None:
    """Extract the number after `#!m/` in a map.utoronto.ca URL.

    This is the join key into `buildings.json`, not the `?id=` value (that's
    the campus id, identical for every building -- CLAUDE.md decision log).
    """
    if not building_url:
        return None
    match = re.search(r"#!m/(\d+)", building_url)
    return match.group(1) if match else None


def parse_time(meeting_time: dict[str, Any]) -> tuple[int, int, int] | None:
    """Return (day_of_week, start_min, end_min), or None if unparseable.

    `day_of_week` is 0-indexed from Monday (CLAUDE.md §6: `day - 1`).
    Guards, in order: start/end present; day in {1..5} for both and equal
    (a meeting split across two different `day` values was never observed
    and would be nonsensical -- treat it as unparseable rather than guess
    which day is right); millisofday lands on a minute boundary; end is
    strictly after start (a zero-or-negative-length meeting is unusable).
    """
    start = meeting_time.get("start")
    end = meeting_time.get("end")
    if not start or not end:
        return None

    start_day = start.get("day")
    end_day = end.get("day")
    if start_day not in VALID_DAYS or end_day != start_day:
        return None

    start_millis = start.get("millisofday")
    end_millis = end.get("millisofday")
    if start_millis is None or end_millis is None:
        return None
    if start_millis % 60000 != 0 or end_millis % 60000 != 0:
        return None

    start_min = start_millis // 60000
    end_min = end_millis // 60000
    if end_min <= start_min:
        return None

    return start_day - 1, start_min, end_min


def is_online_delivery(section: dict[str, Any], session_code: str) -> bool:
    """True only for SYNC/ASYNC. `deliveryModes` is keyed by session, not
    directly on the meeting, because a section's mode can differ between the
    two term sessions it runs in.

    A session code absent from `deliveryModes` entirely fails open (treated
    as in-person) rather than dropping the meeting -- never observed in real
    data, but an unmatched session shouldn't silently discard a class.
    """
    for entry in section.get("deliveryModes") or []:
        if entry.get("session") == session_code:
            return entry.get("mode") in ONLINE_MODES
    return False


def resolve_building(
    marker_id: str | None, buildings_by_map_id: dict[int, dict[str, Any]]
) -> dict[str, Any] | None:
    if marker_id is None:
        return None
    try:
        return buildings_by_map_id.get(int(marker_id))
    except ValueError:
        return None


# --- normalization ----------------------------------------------------------


@dataclass
class DropCounts:
    online_or_async: int = 0
    not_st_george: int = 0
    cancelled_section: int = 0
    unknown_building_code: int = 0
    unparseable_time: int = 0

    def add(self, category: str) -> None:
        setattr(self, category, getattr(self, category) + 1)

    def as_dict(self) -> dict[str, int]:
        return {cat: getattr(self, cat) for cat in DROP_CATEGORIES}

    @property
    def total(self) -> int:
        return sum(self.as_dict().values())


@dataclass
class NormalizeResult:
    meetings: list[dict[str, Any]] = field(default_factory=list)
    drops: DropCounts = field(default_factory=DropCounts)
    drop_samples: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {cat: [] for cat in DROP_CATEGORIES}
    )
    duplicate_meetings: int = 0
    total_input_meetings: int = 0

    def record_drop(self, category: str, sample: dict[str, Any]) -> None:
        self.drops.add(category)
        if len(self.drop_samples[category]) < SAMPLES_PER_CATEGORY:
            self.drop_samples[category].append(sample)


def _meeting_row(
    *, course: dict, section: dict, meeting_time: dict,
    building: dict, day_of_week: int, start_min: int, end_min: int,
) -> dict[str, Any]:
    ttb_code = (meeting_time.get("building") or {}).get("buildingCode")
    if ttb_code and ttb_code != building["code"]:
        logger.warning(
            "building code mismatch: TTB said %s, buildings.json (via marker "
            "id) says %s -- using buildings.json", ttb_code, building["code"],
        )

    room_number = (meeting_time.get("building") or {}).get("buildingRoomNumber") or None

    return {
        "term_code": meeting_time["sessionCode"],
        "building_code": building["code"],
        "room_number": room_number,
        "course_code": course["code"],
        "section": section["name"],
        "day_of_week": day_of_week,
        "start_min": start_min,
        "end_min": end_min,
        "enrolment": section.get("currentEnrolment"),
    }


def _row_key(row: dict[str, Any]) -> tuple:
    return (
        row["term_code"], row["building_code"], row["room_number"],
        row["course_code"], row["section"], row["day_of_week"],
        row["start_min"], row["end_min"],
    )


def normalize_all(
    raw_payloads: list[dict[str, Any]],
    buildings_by_map_id: dict[int, dict[str, Any]],
) -> NormalizeResult:
    """Walk every course/section/meetingTime across all division payloads.

    Ordering of checks (deliberate, not arbitrary -- determines which single
    category a meeting with multiple issues lands in):
    1. not_st_george, once per course (cheapest, skips everything below)
    2. cancelled_section, once per section (cancellation is more "final"
       than delivery mode -- a cancelled+online section counts once, as
       cancelled, not online)
    3. online_or_async, per meeting (mode is keyed by session)
    4. unparseable_time, per meeting (unusable regardless of building)
    5. unknown_building_code, per meeting (covers TBA/empty buildingCode,
       a malformed buildingUrl, and a marker id absent from buildings.json
       -- including the 3 buildings Task 5 excluded: AB, BF, UY)
    """
    result = NormalizeResult()
    seen_rows: set[tuple] = set()

    for payload in raw_payloads:
        for course in payload.get("courses", []):
            if course.get("campus") != "St. George":
                for section in course.get("sections") or []:
                    n = len(section.get("meetingTimes") or [])
                    result.total_input_meetings += n
                    for _ in range(n):
                        result.record_drop("not_st_george", {"course_code": course.get("code")})
                continue

            for section in course.get("sections") or []:
                meeting_times = section.get("meetingTimes") or []
                result.total_input_meetings += len(meeting_times)

                if section.get("cancelInd") == "Y":
                    for _ in meeting_times:
                        result.record_drop("cancelled_section", {
                            "course_code": course.get("code"),
                            "section": section.get("name"),
                        })
                    continue

                for meeting_time in meeting_times:
                    session_code = meeting_time.get("sessionCode")

                    if is_online_delivery(section, session_code):
                        result.record_drop("online_or_async", {
                            "course_code": course.get("code"),
                            "section": section.get("name"),
                            "session": session_code,
                        })
                        continue

                    parsed_time = parse_time(meeting_time)
                    if parsed_time is None:
                        result.record_drop("unparseable_time", {
                            "course_code": course.get("code"),
                            "section": section.get("name"),
                            "start": meeting_time.get("start"),
                            "end": meeting_time.get("end"),
                        })
                        continue

                    building_info = meeting_time.get("building") or {}
                    marker_id = parse_marker_id(building_info.get("buildingUrl"))
                    building = resolve_building(marker_id, buildings_by_map_id)
                    if building is None:
                        result.record_drop("unknown_building_code", {
                            "course_code": course.get("code"),
                            "section": section.get("name"),
                            "buildingCode": building_info.get("buildingCode"),
                            "buildingUrl": building_info.get("buildingUrl"),
                        })
                        continue

                    day_of_week, start_min, end_min = parsed_time
                    row = _meeting_row(
                        course=course, section=section, meeting_time=meeting_time,
                        building=building, day_of_week=day_of_week,
                        start_min=start_min, end_min=end_min,
                    )

                    key = _row_key(row)
                    if key in seen_rows:
                        result.duplicate_meetings += 1
                        continue
                    seen_rows.add(key)
                    result.meetings.append(row)

    return result


# --- I/O ---------------------------------------------------------------


def load_buildings(path: Path) -> dict[int, dict[str, Any]]:
    buildings = json.loads(path.read_text())
    return {b["map_id"]: b for b in buildings}


def load_raw_divisions(raw_dir: Path) -> list[dict[str, Any]]:
    payloads = []
    for path in sorted(raw_dir.glob("*.json")):
        payloads.append(json.loads(path.read_text()))
    return payloads


def coverage_row(meetings: list[dict[str, Any]], today: str) -> dict[str, Any]:
    total = len(meetings)
    with_room = sum(1 for m in meetings if m["room_number"])
    coverage_pct = (with_room / total * 100) if total else 0.0
    return {
        "date": today,
        "meetings": total,
        "with_room": with_room,
        "coverage_pct": round(coverage_pct, 2),
    }


def append_coverage_history(path: Path, row: dict[str, Any]) -> None:
    """Append one dated row, replacing any existing row for the same date.

    Re-running the normalizer twice on the same day (dev iteration, CI)
    should not add a second row -- this file's value is a trend over
    calendar time (CLAUDE.md §3), and duplicate same-day rows are just git
    noise with no offsetting benefit.
    """
    if path.exists() and path.read_text().strip():
        history = json.loads(path.read_text())
    else:
        history = []

    history = [r for r in history if r["date"] != row["date"]]
    history.append(row)
    history.sort(key=lambda r: r["date"])

    write_json_atomic(path, history)


def write_report(path: Path, result: NormalizeResult, today: str) -> None:
    report = {
        "date": today,
        "total_input_meetings": result.total_input_meetings,
        "kept": len(result.meetings),
        "duplicate_meetings": result.duplicate_meetings,
        "dropped": result.drops.as_dict(),
        "samples": result.drop_samples,
    }
    write_json_atomic(path, report)


def _log_summary(result: NormalizeResult) -> None:
    logger.info(
        "kept=%s dropped=%s duplicates=%s (input=%s)",
        len(result.meetings), result.drops.total, result.duplicate_meetings,
        result.total_input_meetings,
    )
    for category in DROP_CATEGORIES:
        count = getattr(result.drops, category)
        if count:
            logger.info("  %s: %s", category, count)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vacant_scraper.normalize",
        description="Normalize raw TTB scrape JSON into data/meetings.json.",
    )
    parser.add_argument("--raw-dir", type=Path, default=Path("raw"))
    parser.add_argument("--buildings", type=Path, default=Path("../data/buildings.json"))
    parser.add_argument("--out", type=Path, default=Path("../data/meetings.json"))
    parser.add_argument("--coverage-out", type=Path, default=Path("../data/coverage-history.json"))
    parser.add_argument("--report-out", type=Path, default=Path("../data/normalize-report.json"))
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    raw_payloads = load_raw_divisions(args.raw_dir)
    if not raw_payloads:
        logger.error("no raw division files found in %s", args.raw_dir)
        return 1

    buildings_by_map_id = load_buildings(args.buildings)
    result = normalize_all(raw_payloads, buildings_by_map_id)

    today = datetime.now(TORONTO).date().isoformat()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(args.out, result.meetings)
    append_coverage_history(args.coverage_out, coverage_row(result.meetings, today))
    write_report(args.report_out, result, today)

    _log_summary(result)
    logger.info("wrote %s meetings to %s", len(result.meetings), args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
