"""Offline tests for the TTB client.

Every test here runs against httpx's MockTransport — nothing touches
utoronto.ca. That is the point: the scraper is the most breakage-prone part of
the project, and it needs to be debuggable on a plane.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from vacant_scraper.ttb import (
    ENDPOINT,
    SERVER_PAGE_SIZE,
    Page,
    TTBClient,
    TTBError,
    build_request_body,
)

FIXTURES = Path(__file__).parent / "fixtures"


def make_page_body(*, courses: list[dict], total: int, page: int) -> dict:
    """A minimal response in the shape TTB returns."""
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
    """A TTBClient wired to a mock transport, with delays turned off."""
    transport = httpx.MockTransport(handler)
    return TTBClient(
        delay_seconds=0,
        backoff_base_seconds=0,
        client=httpx.Client(transport=transport),
    )


# --- pagination ------------------------------------------------------------


def test_last_page_rounds_up():
    # 45 courses at 20 per page is three pages, the last one partial.
    assert Page(courses=[], total=45, page=1, page_size=20).last_page == 3
    assert Page(courses=[], total=40, page=1, page_size=20).last_page == 2
    assert Page(courses=[], total=1, page=1, page_size=20).last_page == 1
    # An empty result set is still "one page", so loops terminate.
    assert Page(courses=[], total=0, page=1, page_size=20).last_page == 1


def test_iter_pages_walks_to_the_end_and_stops():
    """Three pages of 45 results: pages 1, 2, 3 requested, then stop."""
    requested_pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        page = body["page"]
        requested_pages.append(page)
        # 20, 20, 5 courses across the three pages.
        count = 20 if page < 3 else 5
        courses = [{"code": f"P{page}C{i}"} for i in range(count)]
        return httpx.Response(200, json=make_page_body(courses=courses, total=45, page=page))

    with client_with(handler) as client:
        pages = list(client.iter_pages(divisions=["ARTSC"], sessions=["20269"]))

    assert requested_pages == [1, 2, 3], "must not probe past the last page"
    assert [len(p.courses) for p in pages] == [20, 20, 5]


def test_iter_courses_flattens_every_page():
    # total=45 at 20/page is three pages; each returns one stub course.
    def handler(request: httpx.Request) -> httpx.Response:
        page = json.loads(request.content)["page"]
        courses = [{"code": f"C{page}"}]
        return httpx.Response(200, json=make_page_body(courses=courses, total=45, page=page))

    with client_with(handler) as client:
        codes = [c["code"] for c in client.iter_courses(divisions=["ARTSC"], sessions=["20269"])]

    assert codes == ["C1", "C2", "C3"]


def test_iter_pages_respects_max_pages():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        page = json.loads(request.content)["page"]
        return httpx.Response(
            200, json=make_page_body(courses=[{"code": "X"}], total=500, page=page)
        )

    with client_with(handler) as client:
        pages = list(
            client.iter_pages(divisions=["ARTSC"], sessions=["20269"], max_pages=2)
        )

    assert len(pages) == 2
    assert calls == 2


def test_iter_pages_can_resume_from_a_later_page():
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = json.loads(request.content)["page"]
        seen.append(page)
        return httpx.Response(
            200, json=make_page_body(courses=[{"code": "X"}], total=60, page=page)
        )

    with client_with(handler) as client:
        list(client.iter_pages(divisions=["ARTSC"], sessions=["20269"], start_page=3))

    assert seen == [3]


def test_iter_pages_stops_on_an_unexpectedly_empty_page():
    """Guards against looping to last_page when the server runs dry early."""
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = json.loads(request.content)["page"]
        seen.append(page)
        courses = [{"code": "X"}] if page == 1 else []
        return httpx.Response(
            200, json=make_page_body(courses=courses, total=1000, page=page)
        )

    with client_with(handler) as client:
        list(client.iter_pages(divisions=["ARTSC"], sessions=["20269"]))

    assert seen == [1, 2], "should stop right after the first empty page"


# --- retry / backoff -------------------------------------------------------


def test_retries_5xx_then_succeeds():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(503, text="upstream is sad")
        return httpx.Response(200, json=make_page_body(courses=[], total=0, page=1))

    with client_with(handler) as client:
        page = client.fetch_page(divisions=["ARTSC"], sessions=["20269"], page=1)

    assert attempts == 3
    assert page.total == 0


def test_retries_transport_errors():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("connection reset", request=request)
        return httpx.Response(200, json=make_page_body(courses=[], total=0, page=1))

    with client_with(handler) as client:
        client.fetch_page(divisions=["ARTSC"], sessions=["20269"], page=1)

    assert attempts == 2


def test_gives_up_after_max_attempts():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(500, text="nope")

    client = client_with(handler)
    client.max_attempts = 3
    with client, pytest.raises(TTBError, match="giving up"):
        client.fetch_page(divisions=["ARTSC"], sessions=["20269"], page=1)

    assert attempts == 3


def test_does_not_retry_client_errors():
    """A 400 means our request is wrong; resending it just adds load."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(400, text="bad filter")

    with client_with(handler) as client:
        with pytest.raises(TTBError, match="client error 400"):
            client.fetch_page(divisions=["ARTSC"], sessions=["20269"], page=1)

    assert attempts == 1


# --- request shape ---------------------------------------------------------


def test_sends_the_headers_and_body_the_endpoint_requires():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        captured["body"] = json.loads(request.content)
        captured["url"] = str(request.url)
        return httpx.Response(200, json=make_page_body(courses=[], total=0, page=1))

    with client_with(handler) as client:
        client.fetch_page(divisions=["APSC", "ARTSC"], sessions=["20269"], page=7)

    assert captured["url"] == ENDPOINT
    # Without this header the server answers with XML and a 200 status.
    assert captured["headers"]["accept"] == "application/json"
    assert "vacant-scraper" in captured["headers"]["user-agent"]
    assert captured["body"]["page"] == 7
    assert captured["body"]["divisions"] == ["APSC", "ARTSC"]
    assert captured["body"]["sessions"] == ["20269"]
    assert captured["body"]["campuses"] == ["St. George"]


def test_request_body_has_every_key_the_ttb_frontend_sends():
    body = build_request_body(divisions=["ARTSC"], sessions=["20269"], page=1)
    expected_keys = {
        "courseCodeAndTitleProps", "departmentProps", "campuses", "sessions",
        "requirementProps", "instructor", "courseLevels", "deliveryModes",
        "dayPreferences", "timePreferences", "divisions", "creditWeights",
        "availableSpace", "waitListable", "page", "pageSize", "direction",
    }
    assert set(body) == expected_keys


# --- malformed responses ---------------------------------------------------


def test_xml_response_raises_a_useful_error():
    """This is what you get when the Accept header goes missing."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<TTBResponse><payload><pageableCourse>...",
            headers={"content-type": "application/xml"},
        )

    with client_with(handler) as client:
        with pytest.raises(TTBError, match="not JSON"):
            client.fetch_page(divisions=["ARTSC"], sessions=["20269"], page=1)


def test_non_zero_status_code_in_payload_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"payload": None, "status": [{"code": 5, "message": "Boom"}]},
        )

    with client_with(handler) as client:
        with pytest.raises(TTBError, match="reported failure"):
            client.fetch_page(divisions=["ARTSC"], sessions=["20269"], page=1)


def test_unexpected_shape_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"somethingElse": {}, "status": []})

    with client_with(handler) as client:
        with pytest.raises(TTBError, match="unexpected response shape"):
            client.fetch_page(divisions=["ARTSC"], sessions=["20269"], page=1)


# --- against a recorded real response --------------------------------------


def test_parses_a_recorded_real_response():
    recorded = json.loads((FIXTURES / "ttb_page_real.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=recorded)

    with client_with(handler) as client:
        page = client.fetch_page(divisions=["ARTSC"], sessions=["20269"], page=1)

    assert page.page == 1
    assert page.page_size == SERVER_PAGE_SIZE
    assert page.total > 1000, "ARTSC alone has thousands of courses"
    assert [c["code"] for c in page.courses] == ["ABP100Y1", "ABP107Y1"]

    meeting = page.courses[0]["sections"][0]["meetingTimes"][0]
    # Times are integer millis-since-midnight, not "HH:MM" strings.
    assert meeting["start"]["millisofday"] == 36_000_000  # 10:00
    assert meeting["building"]["buildingCode"] == "WW"
