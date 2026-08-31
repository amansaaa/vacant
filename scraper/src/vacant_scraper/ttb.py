"""Client for the UofT Timetable Builder (TTB) backend.

TTB is a client-side app at https://ttb.utoronto.ca that talks to an EASI-hosted
JSON API. The only call we need is `getPageableCourses`: a POST that takes a
search filter and returns one page of matching courses, each carrying its
sections and their meeting times.

Everything measured about this endpoint — required headers, the fixed page size,
the day/time encoding — is written up in `docs/ttb-api.md`. Two facts are load
bearing enough to repeat here:

1. `Accept: application/json` is REQUIRED. Without it the server returns XML
   with a 200 status, which fails to parse in a confusing way.
2. `pageSize` in the request body is IGNORED. The server always returns 20
   courses per page, so pagination is driven purely by `page` and `total`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

import httpx

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.easi.utoronto.ca/ttb/getPageableCourses"

USER_AGENT = (
    "vacant-scraper/0.1 (UofT free-classroom finder; contact: johndoe@gmail.com)"
)

# The server pins this regardless of what we ask for. Measured, not assumed —
# requests with pageSize 5, 500 and 2000 all came back with exactly 20 courses.
SERVER_PAGE_SIZE = 20

# The six St. George divisions TTB exposes. Taken from the division filter in
# the TTB UI; a request may name any subset of them.
DIVISIONS = {
    "APSC": "Applied Science and Engineering",
    "ARTSC": "Arts and Science",
    "ARCLA": "Architecture, Landscape, and Design (Daniels)",
    "FIS": "Information",
    "FPEH": "Kinesiology and Physical Education",
    "MUSIC": "Music",
}

# Fall 2026, Winter 2027, and the pseudo-session for full-year (Y) courses.
# A Y course is returned once but repeats each meeting under both real session
# codes, so asking for all three is how you get complete coverage.
SESSIONS_2026_27 = ["20269", "20271", "20269-20271"]


class TTBError(RuntimeError):
    """The endpoint answered, but not with something we can use."""


@dataclass(frozen=True)
class Page:
    """One page of the paginated course search."""

    courses: list[dict[str, Any]]
    total: int
    page: int
    page_size: int

    @property
    def last_page(self) -> int:
        """1-indexed number of the final page for this result set."""
        if self.total <= 0 or self.page_size <= 0:
            return 1
        # Ceiling division: 3758 courses at 20/page is 188 pages, not 187.
        return -(-self.total // self.page_size)


def build_request_body(
    *,
    divisions: list[str],
    sessions: list[str],
    page: int,
    page_size: int = SERVER_PAGE_SIZE,
) -> dict[str, Any]:
    """The search filter, in the shape TTB's own frontend sends it.

    Every key here is present in the browser's request. The empty lists and
    strings are 'no filter on this facet' — omitting them entirely is untested,
    so we send the full shape rather than guess which ones are optional.
    """
    return {
        "courseCodeAndTitleProps": {
            "courseCode": "",
            "courseTitle": "",
            "courseSectionCode": "",
        },
        "departmentProps": [],
        "campuses": ["St. George"],
        "sessions": sessions,
        "requirementProps": [],
        "instructor": "",
        "courseLevels": [],
        "deliveryModes": [],
        "dayPreferences": [],
        "timePreferences": [],
        "divisions": divisions,
        "creditWeights": [],
        "availableSpace": False,
        "waitListable": False,
        "page": page,
        "pageSize": page_size,
        "direction": "asc",
    }


@dataclass
class TTBClient:
    """Fetches course pages, with retries and a delay between requests.

    Pass `client` to inject an httpx.Client (the tests use one wired to a mock
    transport); otherwise one is created on first use and closed by `close()`.
    """

    delay_seconds: float = 1.0
    max_attempts: int = 4
    backoff_base_seconds: float = 2.0
    timeout_seconds: float = 60.0
    client: httpx.Client | None = None
    _owns_client: bool = field(default=False, init=False, repr=False)

    def _http(self) -> httpx.Client:
        if self.client is None:
            self.client = httpx.Client(timeout=self.timeout_seconds)
            self._owns_client = True
        return self.client

    def close(self) -> None:
        if self.client is not None and self._owns_client:
            self.client.close()
            self.client = None
            self._owns_client = False

    def __enter__(self) -> "TTBClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def fetch_page(self, *, divisions: list[str], sessions: list[str], page: int) -> Page:
        """Fetch one page, retrying on transport errors and 5xx responses.

        Retries use exponential backoff (base 2: 2s, 4s, 8s). A 4xx is not
        retried — that means our request is wrong, and sending it again will
        produce the same answer while adding load to someone else's server.
        """
        body = build_request_body(divisions=divisions, sessions=sessions, page=page)
        headers = {
            # Required. Without it the server responds with XML, status 200.
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }

        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self._http().post(ENDPOINT, json=body, headers=headers)
            except httpx.HTTPError as exc:
                last_error = exc
                logger.warning(
                    "page %s: transport error on attempt %s/%s: %s",
                    page, attempt, self.max_attempts, exc,
                )
            else:
                if response.status_code >= 500:
                    last_error = TTBError(
                        f"server error {response.status_code} on page {page}"
                    )
                    logger.warning(
                        "page %s: HTTP %s on attempt %s/%s",
                        page, response.status_code, attempt, self.max_attempts,
                    )
                elif response.status_code >= 400:
                    # Our fault. Retrying cannot help.
                    raise TTBError(
                        f"client error {response.status_code} on page {page}: "
                        f"{response.text[:200]}"
                    )
                else:
                    return _parse_page(response)

            if attempt < self.max_attempts:
                sleep_for = self.backoff_base_seconds ** attempt
                logger.info("backing off %.1fs before retry", sleep_for)
                time.sleep(sleep_for)

        raise TTBError(
            f"giving up on page {page} after {self.max_attempts} attempts"
        ) from last_error

    def iter_pages(
        self,
        *,
        divisions: list[str],
        sessions: list[str],
        start_page: int = 1,
        max_pages: int | None = None,
    ) -> Iterator[Page]:
        """Yield pages from `start_page` until the result set is exhausted.

        The first response tells us `total`, which is what bounds the loop —
        we never probe past the end. Sleeps `delay_seconds` between requests
        (not before the first, and not after the last).
        """
        page_number = start_page
        pages_yielded = 0
        while True:
            page = self.fetch_page(
                divisions=divisions, sessions=sessions, page=page_number
            )
            yield page
            pages_yielded += 1

            if page_number >= page.last_page:
                return
            if max_pages is not None and pages_yielded >= max_pages:
                return
            if not page.courses:
                # Defensive: an empty page before `total` is reached would
                # otherwise loop until last_page doing nothing useful.
                logger.warning("page %s came back empty; stopping", page_number)
                return

            page_number += 1
            if self.delay_seconds:
                time.sleep(self.delay_seconds)

    def iter_courses(
        self, *, divisions: list[str], sessions: list[str]
    ) -> Iterator[dict[str, Any]]:
        """Flatten `iter_pages` into a stream of course objects."""
        for page in self.iter_pages(divisions=divisions, sessions=sessions):
            yield from page.courses


def _parse_page(response: httpx.Response) -> Page:
    """Pull the pageable-course block out of a response body."""
    try:
        data = response.json()
    except ValueError as exc:
        # Almost always the missing Accept header: the body is XML.
        raise TTBError(
            f"response was not JSON (first 120 chars: {response.text[:120]!r})"
        ) from exc

    statuses = data.get("status") or []
    for status in statuses:
        if status.get("code") not in (0, None):
            raise TTBError(f"endpoint reported failure: {status}")

    try:
        pageable = data["payload"]["pageableCourse"]
    except (KeyError, TypeError) as exc:
        raise TTBError(f"unexpected response shape: {list(data)}") from exc

    return Page(
        courses=pageable.get("courses") or [],
        total=int(pageable.get("total") or 0),
        page=int(pageable.get("page") or 0),
        page_size=int(pageable.get("pageSize") or SERVER_PAGE_SIZE),
    )
