"""Offline tests for the full scrape job.

Same rule as test_ttb.py: nothing here touches the network. A TTBClient
wired to httpx.MockTransport stands in for the real endpoint.
"""

from __future__ import annotations

import json

import httpx
import pytest

from vacant_scraper.scrape import (
    RunSummary,
    division_output_path,
    fetch_division,
    main,
    run,
    write_json_atomic,
)
from vacant_scraper.ttb import SERVER_PAGE_SIZE, TTBClient


def make_page_body(*, courses: list[dict], total: int, page: int) -> dict:
    return {
        "payload": {
            "pageableCourse": {
                "courses": courses,
                "total": total,
                "page": page,
                "pageSize": SERVER_PAGE_SIZE,
                "direction": None,
            }
        },
        "status": [{"code": 0, "message": "Success"}],
    }


def client_with(handler) -> TTBClient:
    transport = httpx.MockTransport(handler)
    return TTBClient(
        delay_seconds=0,
        backoff_base_seconds=0,
        max_attempts=2,
        client=httpx.Client(transport=transport),
    )


# --- small helpers -----------------------------------------------------


def test_division_output_path(tmp_path):
    assert division_output_path(tmp_path, "APSC") == tmp_path / "APSC.json"


def test_write_json_atomic_leaves_no_tmp_file_on_success(tmp_path):
    path = tmp_path / "APSC.json"
    write_json_atomic(path, {"division": "APSC"})

    assert path.exists()
    assert not path.with_suffix(".json.tmp").exists()
    assert json.loads(path.read_text()) == {"division": "APSC"}


# --- fetch_division ------------------------------------------------------


def test_fetch_division_collects_all_pages_into_one_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        page = json.loads(request.content)["page"]
        count = 20 if page < 3 else 5
        courses = [{"code": f"P{page}C{i}"} for i in range(count)]
        return httpx.Response(200, json=make_page_body(courses=courses, total=45, page=page))

    with client_with(handler) as client:
        payload, page_count = fetch_division(
            client, division="APSC", sessions=["20269"]
        )

    assert page_count == 3
    assert payload["division"] == "APSC"
    assert payload["sessions"] == ["20269"]
    assert payload["course_count"] == 45
    assert payload["page_count"] == 3
    assert len(payload["courses"]) == 45
    assert payload["courses"][0]["code"] == "P1C0"
    assert "fetched_at" in payload


# --- run: resumability ----------------------------------------------------


def test_run_skips_existing_division_without_a_network_call(tmp_path):
    existing = {
        "division": "APSC", "sessions": ["20269"], "fetched_at": "x",
        "course_count": 7, "page_count": 1, "courses": [{"code": "OLD"}],
    }
    write_json_atomic(tmp_path / "APSC.json", existing)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["divisions"] != ["APSC"], "APSC must not be fetched"
        return httpx.Response(
            200, json=make_page_body(courses=[{"code": "X"}], total=1, page=1)
        )

    with client_with(handler) as client:
        summary = run(
            raw_dir=tmp_path, divisions=["APSC", "FIS"], sessions=["20269"],
            client=client, skip_existing=True,
        )

    by_division = {r.division: r for r in summary.results}
    assert by_division["APSC"].status == "skipped"
    assert by_division["APSC"].course_count == 7
    assert by_division["FIS"].status == "fetched"
    # File untouched.
    assert json.loads((tmp_path / "APSC.json").read_text()) == existing


def test_run_refetches_when_skip_existing_false(tmp_path):
    stale = {
        "division": "APSC", "sessions": ["20269"], "fetched_at": "x",
        "course_count": 1, "page_count": 1, "courses": [{"code": "OLD"}],
    }
    write_json_atomic(tmp_path / "APSC.json", stale)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=make_page_body(courses=[{"code": "NEW"}], total=1, page=1)
        )

    with client_with(handler) as client:
        summary = run(
            raw_dir=tmp_path, divisions=["APSC"], sessions=["20269"],
            client=client, skip_existing=False,
        )

    assert summary.results[0].status == "fetched"
    written = json.loads((tmp_path / "APSC.json").read_text())
    assert written["courses"] == [{"code": "NEW"}]


def test_run_continues_after_one_division_fails(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        division = json.loads(request.content)["divisions"][0]
        if division == "FIS":
            return httpx.Response(500, text="nope")
        return httpx.Response(
            200, json=make_page_body(courses=[{"code": "X"}], total=1, page=1)
        )

    with client_with(handler) as client:
        summary = run(
            raw_dir=tmp_path, divisions=["APSC", "FIS", "MUSIC"],
            sessions=["20269"], client=client, skip_existing=True,
        )

    by_division = {r.division: r for r in summary.results}
    assert by_division["FIS"].status == "failed"
    assert by_division["FIS"].error is not None
    assert not (tmp_path / "FIS.json").exists()
    assert by_division["APSC"].status == "fetched"
    assert by_division["MUSIC"].status == "fetched"
    assert (tmp_path / "APSC.json").exists()
    assert (tmp_path / "MUSIC.json").exists()


# --- RunSummary.ok ---------------------------------------------------------


def test_run_summary_ok_true_when_all_succeed_or_skip():
    from datetime import datetime, timezone
    from vacant_scraper.scrape import DivisionResult

    now = datetime.now(timezone.utc)
    summary = RunSummary(
        started_at=now, finished_at=now,
        results=[
            DivisionResult(division="A", status="fetched"),
            DivisionResult(division="B", status="skipped"),
        ],
    )
    assert summary.ok


def test_run_summary_ok_false_when_any_failed():
    from datetime import datetime, timezone
    from vacant_scraper.scrape import DivisionResult

    now = datetime.now(timezone.utc)
    summary = RunSummary(
        started_at=now, finished_at=now,
        results=[
            DivisionResult(division="A", status="fetched"),
            DivisionResult(division="B", status="failed", error="boom"),
        ],
    )
    assert not summary.ok


# --- main(): CLI ------------------------------------------------------------


def _factory_for(handler):
    def factory(delay: float) -> TTBClient:
        return TTBClient(
            delay_seconds=0, backoff_base_seconds=0, max_attempts=2,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
    return factory


def test_main_parses_repeated_division_and_session_flags(tmp_path):
    seen: list[tuple[str, ...]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((tuple(body["divisions"]), tuple(body["sessions"])))
        return httpx.Response(
            200, json=make_page_body(courses=[{"code": "X"}], total=1, page=1)
        )

    exit_code = main(
        [
            "--division", "APSC", "--division", "FIS",
            "--session", "20269",
            "--raw-dir", str(tmp_path),
        ],
        client_factory=_factory_for(handler),
    )

    assert exit_code == 0
    divisions_seen = {d for (d,), s in seen}
    assert divisions_seen == {"APSC", "FIS"}
    assert all(s == ("20269",) for (d,), s in seen)


def test_main_rejects_unknown_division(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should never be called")

    with pytest.raises(SystemExit):
        main(
            ["--division", "NOPE", "--raw-dir", str(tmp_path)],
            client_factory=_factory_for(handler),
        )


def test_main_returns_zero_on_full_success(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=make_page_body(courses=[{"code": "X"}], total=1, page=1)
        )

    exit_code = main(
        ["--division", "APSC", "--raw-dir", str(tmp_path)],
        client_factory=_factory_for(handler),
    )
    assert exit_code == 0


def test_main_returns_nonzero_exit_code_on_failure(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="nope")

    exit_code = main(
        ["--division", "APSC", "--raw-dir", str(tmp_path)],
        client_factory=_factory_for(handler),
    )
    assert exit_code == 1
