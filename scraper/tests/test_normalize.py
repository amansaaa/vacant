"""Tests for the normalizer (Task 6 -- highest-bug-density task per CLAUDE.md
§10). Nothing here touches the network or a real filesystem beyond `tmp_path`.
"""

from __future__ import annotations

import json

from vacant_scraper.normalize import (
    DropCounts,
    append_coverage_history,
    coverage_row,
    is_online_delivery,
    load_buildings,
    load_raw_divisions,
    main,
    normalize_all,
    parse_marker_id,
    parse_time,
    resolve_building,
    write_report,
)

BA_MARKER = 494470
BA_URL = "https://map.utoronto.ca/?id=1809#!m/494470"

BUILDINGS_BY_MAP_ID = {
    BA_MARKER: {"code": "BA", "map_id": BA_MARKER, "name": "Bahen Centre"},
    494547: {"code": "WW", "map_id": 494547, "name": "Woodsworth College"},
}


# --- builders ------------------------------------------------------------


def make_meeting_time(
    *, day=1, start_millis=36000000, end_millis=46800000,
    session="20269", building_url=BA_URL, building_code="BA",
    room="", repetition="WEEKLY",
) -> dict:
    return {
        "start": {"day": day, "millisofday": start_millis},
        "end": {"day": day, "millisofday": end_millis},
        "sessionCode": session,
        "repetition": repetition,
        "building": {
            "buildingCode": building_code,
            "buildingRoomNumber": room,
            "buildingRoomSuffix": "",
            "buildingUrl": building_url,
            "buildingName": None,
        },
    }


def make_section(
    *, name="LEC0101", cancel="N", enrolment=28,
    meeting_times=None, delivery_modes=None,
) -> dict:
    return {
        "name": name,
        "teachMethod": "LEC",
        "cancelInd": cancel,
        "currentEnrolment": enrolment,
        "maxEnrolment": 30,
        "deliveryModes": delivery_modes or [{"session": "20269", "mode": "INPER"}],
        "meetingTimes": meeting_times if meeting_times is not None else [make_meeting_time()],
    }


def make_course(*, code="CSC148H1", campus="St. George", sections=None) -> dict:
    return {
        "id": "abc123", "name": "Intro", "code": code, "campus": campus,
        "sections": sections if sections is not None else [make_section()],
    }


def payload(*courses) -> dict:
    return {"division": "ARTSC", "courses": list(courses)}


# --- pure functions --------------------------------------------------------


def test_parse_marker_id_extracts_number_after_hash_m():
    assert parse_marker_id(BA_URL) == "494470"


def test_parse_marker_id_returns_none_for_missing_or_malformed_url():
    assert parse_marker_id(None) is None
    assert parse_marker_id("") is None
    assert parse_marker_id("https://map.utoronto.ca/?id=1809") is None


def test_parse_time_converts_day_and_millis_correctly():
    mt = make_meeting_time(day=1, start_millis=36000000, end_millis=46800000)
    assert parse_time(mt) == (0, 600, 780)


def test_parse_time_rejects_day_outside_1_to_5():
    for bad_day in (0, 6, 7):
        mt = make_meeting_time(day=bad_day)
        mt["end"]["day"] = bad_day
        assert parse_time(mt) is None


def test_parse_time_rejects_non_minute_boundary_millis():
    mt = make_meeting_time(start_millis=36000001)
    assert parse_time(mt) is None


def test_parse_time_rejects_missing_start_or_end():
    mt = make_meeting_time()
    del mt["start"]
    assert parse_time(mt) is None


def test_parse_time_rejects_start_day_and_end_day_mismatch():
    mt = make_meeting_time()
    mt["end"]["day"] = 2
    assert parse_time(mt) is None


def test_parse_time_rejects_end_before_or_equal_to_start():
    mt = make_meeting_time(start_millis=36000000, end_millis=36000000)
    assert parse_time(mt) is None
    mt2 = make_meeting_time(start_millis=36000000, end_millis=30000000)
    assert parse_time(mt2) is None


def test_is_online_delivery_true_for_sync_and_async():
    section = make_section(delivery_modes=[{"session": "20269", "mode": "SYNC"}])
    assert is_online_delivery(section, "20269") is True
    section2 = make_section(delivery_modes=[{"session": "20269", "mode": "ASYNC"}])
    assert is_online_delivery(section2, "20269") is True


def test_is_online_delivery_false_for_inper_and_hybr():
    section = make_section(delivery_modes=[{"session": "20269", "mode": "INPER"}])
    assert is_online_delivery(section, "20269") is False
    section2 = make_section(delivery_modes=[{"session": "20269", "mode": "HYBR"}])
    assert is_online_delivery(section2, "20269") is False


def test_is_online_delivery_defaults_to_false_when_session_not_in_delivery_modes():
    section = make_section(delivery_modes=[{"session": "20271", "mode": "SYNC"}])
    assert is_online_delivery(section, "20269") is False


def test_resolve_building_looks_up_by_int_map_id():
    assert resolve_building("494470", BUILDINGS_BY_MAP_ID)["code"] == "BA"
    assert resolve_building("999999", BUILDINGS_BY_MAP_ID) is None
    assert resolve_building(None, BUILDINGS_BY_MAP_ID) is None


# --- one test per drop category --------------------------------------------


def test_online_meeting_is_dropped_as_online_or_async():
    course = make_course(sections=[make_section(
        meeting_times=[make_meeting_time()],
        delivery_modes=[{"session": "20269", "mode": "SYNC"}],
    )])
    result = normalize_all([payload(course)], BUILDINGS_BY_MAP_ID)
    assert result.drops.online_or_async == 1
    assert result.meetings == []


def test_async_meeting_is_dropped_as_online_or_async():
    course = make_course(sections=[make_section(
        meeting_times=[make_meeting_time()],
        delivery_modes=[{"session": "20269", "mode": "ASYNC"}],
    )])
    result = normalize_all([payload(course)], BUILDINGS_BY_MAP_ID)
    assert result.drops.online_or_async == 1


def test_hybrid_and_in_person_meetings_are_kept():
    course = make_course(sections=[make_section(
        meeting_times=[make_meeting_time()],
        delivery_modes=[{"session": "20269", "mode": "HYBR"}],
    )])
    result = normalize_all([payload(course)], BUILDINGS_BY_MAP_ID)
    assert result.drops.online_or_async == 0
    assert len(result.meetings) == 1


def test_non_st_george_course_is_dropped_as_not_st_george():
    course = make_course(campus="Scarborough")
    result = normalize_all([payload(course)], BUILDINGS_BY_MAP_ID)
    assert result.drops.not_st_george == 1
    assert result.meetings == []


def test_cancelled_section_is_dropped_as_cancelled_section():
    course = make_course(sections=[make_section(cancel="Y")])
    result = normalize_all([payload(course)], BUILDINGS_BY_MAP_ID)
    assert result.drops.cancelled_section == 1
    assert result.meetings == []


def test_cancelled_and_online_section_counts_once_as_cancelled_not_online():
    course = make_course(sections=[make_section(
        cancel="Y",
        meeting_times=[make_meeting_time()],
        delivery_modes=[{"session": "20269", "mode": "SYNC"}],
    )])
    result = normalize_all([payload(course)], BUILDINGS_BY_MAP_ID)
    assert result.drops.cancelled_section == 1
    assert result.drops.online_or_async == 0


def test_meeting_with_unresolvable_marker_id_is_dropped_as_unknown_building_code():
    course = make_course(sections=[make_section(
        meeting_times=[make_meeting_time(
            building_url="https://map.utoronto.ca/?id=1809#!m/000000",
        )],
    )])
    result = normalize_all([payload(course)], BUILDINGS_BY_MAP_ID)
    assert result.drops.unknown_building_code == 1


def test_meeting_with_empty_building_code_is_dropped_as_unknown_building_code():
    # The real TBA case: buildingCode is "" and buildingUrl is absent/empty.
    course = make_course(sections=[make_section(
        meeting_times=[make_meeting_time(building_url="", building_code="")],
    )])
    result = normalize_all([payload(course)], BUILDINGS_BY_MAP_ID)
    assert result.drops.unknown_building_code == 1


def test_meeting_with_known_excluded_building_is_dropped_as_unknown_building_code():
    # AB/BF/UY: real buildings Task 5 couldn't geo-match, deliberately absent
    # from buildings.json. This is the expected case, not a bug.
    course = make_course(sections=[make_section(
        meeting_times=[make_meeting_time(
            building_url="https://map.utoronto.ca/?id=1809#!m/999888",
            building_code="AB",
        )],
    )])
    result = normalize_all([payload(course)], BUILDINGS_BY_MAP_ID)
    assert result.drops.unknown_building_code == 1
    assert result.drop_samples["unknown_building_code"][0]["buildingCode"] == "AB"


def test_meeting_with_malformed_building_url_is_dropped_as_unknown_building_code():
    course = make_course(sections=[make_section(
        meeting_times=[make_meeting_time(building_url="not-a-url")],
    )])
    result = normalize_all([payload(course)], BUILDINGS_BY_MAP_ID)
    assert result.drops.unknown_building_code == 1


def test_meeting_with_unparseable_time_is_dropped_as_unparseable_time():
    mt = make_meeting_time()
    mt["end"]["day"] = 3  # mismatched day -> unparseable
    course = make_course(sections=[make_section(meeting_times=[mt])])
    result = normalize_all([payload(course)], BUILDINGS_BY_MAP_ID)
    assert result.drops.unparseable_time == 1


# --- kept-meeting edge cases -------------------------------------------


def test_meeting_with_no_room_number_is_kept_with_null_room():
    course = make_course(sections=[make_section(meeting_times=[make_meeting_time(room="")])])
    result = normalize_all([payload(course)], BUILDINGS_BY_MAP_ID)
    assert len(result.meetings) == 1
    assert result.meetings[0]["room_number"] is None


def test_meeting_with_room_number_is_kept_and_recorded():
    course = make_course(sections=[make_section(meeting_times=[make_meeting_time(room="1180")])])
    result = normalize_all([payload(course)], BUILDINGS_BY_MAP_ID)
    assert result.meetings[0]["room_number"] == "1180"


def test_meeting_with_missing_enrolment_is_kept_with_null_enrolment():
    section = make_section()
    del section["currentEnrolment"]
    course = make_course(sections=[section])
    result = normalize_all([payload(course)], BUILDINGS_BY_MAP_ID)
    assert result.meetings[0]["enrolment"] is None


def test_y_session_course_produces_two_separate_term_rows_not_a_duplicate():
    section = make_section(meeting_times=[
        make_meeting_time(session="20269"),
        make_meeting_time(session="20271"),
    ])
    course = make_course(sections=[section])
    result = normalize_all([payload(course)], BUILDINGS_BY_MAP_ID)
    assert len(result.meetings) == 2
    assert {m["term_code"] for m in result.meetings} == {"20269", "20271"}
    assert result.duplicate_meetings == 0


def test_biweekly_repetition_is_kept_and_treated_as_weekly():
    course = make_course(sections=[make_section(
        meeting_times=[make_meeting_time(repetition="BI_WEEKLY")],
    )])
    result = normalize_all([payload(course)], BUILDINGS_BY_MAP_ID)
    assert len(result.meetings) == 1
    assert result.drops.total == 0


def test_building_code_join_uses_buildings_json_code_not_ttb_buildingcode_string():
    # TTB says "WW" but the marker id resolves to BA in buildings.json --
    # the output should trust buildings.json.
    course = make_course(sections=[make_section(
        meeting_times=[make_meeting_time(building_url=BA_URL, building_code="WW")],
    )])
    result = normalize_all([payload(course)], BUILDINGS_BY_MAP_ID)
    assert result.meetings[0]["building_code"] == "BA"


# --- duplicates ----------------------------------------------------------


def test_exact_duplicate_meeting_across_two_courses_is_deduped():
    mt = make_meeting_time()
    course_a = make_course(code="CSC148H1", sections=[make_section(meeting_times=[mt])])
    course_b = make_course(code="CSC148H1", sections=[make_section(meeting_times=[mt])])
    result = normalize_all([payload(course_a, course_b)], BUILDINGS_BY_MAP_ID)
    assert len(result.meetings) == 1
    assert result.duplicate_meetings == 1


# --- integration ----------------------------------------------------------


def test_normalize_all_across_multiple_division_payloads_merges_correctly():
    course_a = make_course(code="CSC148H1")
    course_b = make_course(code="MAT137Y1")
    result = normalize_all(
        [payload(course_a), payload(course_b)], BUILDINGS_BY_MAP_ID,
    )
    assert len(result.meetings) == 2
    assert {m["course_code"] for m in result.meetings} == {"CSC148H1", "MAT137Y1"}


def test_normalize_all_report_counts_match_actual_drops():
    courses = [
        make_course(code="ONLINE1", sections=[make_section(
            meeting_times=[make_meeting_time()],
            delivery_modes=[{"session": "20269", "mode": "SYNC"}],
        )]),
        make_course(code="OFFCAMPUS", campus="Scarborough"),
        make_course(code="CANCELLED1", sections=[make_section(cancel="Y")]),
        make_course(code="NOBUILDING", sections=[make_section(
            meeting_times=[make_meeting_time(building_url="", building_code="")],
        )]),
        make_course(code="BADTIME", sections=[make_section(
            meeting_times=[{**make_meeting_time(), "end": {"day": 3, "millisofday": 1000}}],
        )]),
        make_course(code="GOOD1"),
    ]
    result = normalize_all([payload(*courses)], BUILDINGS_BY_MAP_ID)
    assert result.drops.as_dict() == {
        "online_or_async": 1, "not_st_george": 1, "cancelled_section": 1,
        "unknown_building_code": 1, "unparseable_time": 1,
    }
    assert len(result.meetings) == 1


def test_write_report_includes_capped_samples_per_category(tmp_path):
    courses = [
        make_course(code=f"C{i}", campus="Scarborough") for i in range(10)
    ]
    result = normalize_all([payload(*courses)], BUILDINGS_BY_MAP_ID)
    out = tmp_path / "report.json"
    write_report(out, result, "2026-09-04")
    report = json.loads(out.read_text())
    assert report["dropped"]["not_st_george"] == 10
    assert len(report["samples"]["not_st_george"]) == 5


def test_coverage_row_computes_correct_percentage():
    meetings = [
        {"room_number": "101"}, {"room_number": None}, {"room_number": None}, {"room_number": "202"},
    ]
    row = coverage_row(meetings, "2026-09-04")
    assert row == {"date": "2026-09-04", "meetings": 4, "with_room": 2, "coverage_pct": 50.0}


def test_coverage_row_handles_zero_meetings():
    row = coverage_row([], "2026-09-04")
    assert row["coverage_pct"] == 0.0


def test_append_coverage_history_creates_file_when_absent(tmp_path):
    path = tmp_path / "coverage-history.json"
    append_coverage_history(path, {"date": "2026-09-04", "meetings": 10, "with_room": 0, "coverage_pct": 0.0})
    history = json.loads(path.read_text())
    assert history == [{"date": "2026-09-04", "meetings": 10, "with_room": 0, "coverage_pct": 0.0}]


def test_append_coverage_history_replaces_same_date_row_on_rerun(tmp_path):
    path = tmp_path / "coverage-history.json"
    append_coverage_history(path, {"date": "2026-09-04", "meetings": 10, "with_room": 0, "coverage_pct": 0.0})
    append_coverage_history(path, {"date": "2026-09-04", "meetings": 20, "with_room": 5, "coverage_pct": 25.0})
    history = json.loads(path.read_text())
    assert len(history) == 1
    assert history[0]["meetings"] == 20


def test_append_coverage_history_appends_new_row_for_new_date(tmp_path):
    path = tmp_path / "coverage-history.json"
    append_coverage_history(path, {"date": "2026-09-03", "meetings": 10, "with_room": 0, "coverage_pct": 0.0})
    append_coverage_history(path, {"date": "2026-09-04", "meetings": 20, "with_room": 5, "coverage_pct": 25.0})
    history = json.loads(path.read_text())
    assert len(history) == 2
    assert [r["date"] for r in history] == ["2026-09-03", "2026-09-04"]


def test_load_buildings_indexes_by_map_id(tmp_path):
    path = tmp_path / "buildings.json"
    path.write_text(json.dumps([{"code": "BA", "map_id": 494470, "name": "Bahen"}]))
    buildings = load_buildings(path)
    assert buildings[494470]["code"] == "BA"


def test_load_raw_divisions_reads_every_json_file(tmp_path):
    (tmp_path / "APSC.json").write_text(json.dumps(payload(make_course(code="A1"))))
    (tmp_path / "ARTSC.json").write_text(json.dumps(payload(make_course(code="A2"))))
    payloads = load_raw_divisions(tmp_path)
    assert len(payloads) == 2


def test_main_end_to_end_writes_all_three_artifacts_to_tmp_path(tmp_path):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "ARTSC.json").write_text(json.dumps(payload(make_course())))

    buildings_path = tmp_path / "buildings.json"
    buildings_path.write_text(json.dumps([
        {"code": "BA", "map_id": BA_MARKER, "name": "Bahen Centre"},
    ]))

    out = tmp_path / "meetings.json"
    coverage_out = tmp_path / "coverage-history.json"
    report_out = tmp_path / "normalize-report.json"

    exit_code = main([
        "--raw-dir", str(raw_dir),
        "--buildings", str(buildings_path),
        "--out", str(out),
        "--coverage-out", str(coverage_out),
        "--report-out", str(report_out),
    ])

    assert exit_code == 0
    meetings = json.loads(out.read_text())
    assert len(meetings) == 1
    assert meetings[0]["building_code"] == "BA"

    coverage = json.loads(coverage_out.read_text())
    assert len(coverage) == 1

    report = json.loads(report_out.read_text())
    assert report["kept"] == 1


def test_main_returns_nonzero_when_raw_dir_is_empty(tmp_path):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    buildings_path = tmp_path / "buildings.json"
    buildings_path.write_text("[]")

    exit_code = main([
        "--raw-dir", str(raw_dir),
        "--buildings", str(buildings_path),
        "--out", str(tmp_path / "meetings.json"),
        "--coverage-out", str(tmp_path / "coverage-history.json"),
        "--report-out", str(tmp_path / "normalize-report.json"),
    ])
    assert exit_code == 1


def test_drop_counts_as_dict_matches_declared_categories():
    counts = DropCounts()
    assert set(counts.as_dict().keys()) == {
        "online_or_async", "not_st_george", "cancelled_section",
        "unknown_building_code", "unparseable_time",
    }
