"""The full scrape job: fetch every division and write raw JSON to disk.

This is the production entrypoint that Task 3's `TTBClient` was built for. It
does exactly three things and nothing else:

1. Ask TTB for every division, for the full 2026-27 session set.
2. Write one untouched JSON file per division to `raw/`.
3. Be safe to kill and rerun: a division whose file already exists is not
   re-fetched, so a partial run resumes instead of starting over.

No normalization, no database, no vacancy math. Task 6 reads what this writes;
Task 9 loads it into Postgres. This module's only job is "get the data down
reliably."
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from vacant_scraper import ttb
from vacant_scraper.ttb import TTBClient, TTBError

logger = logging.getLogger(__name__)


def division_output_path(raw_dir: Path, division: str) -> Path:
    return raw_dir / f"{division}.json"


def write_json_atomic(path: Path, data: dict) -> None:
    """Write via a temp file + rename, so a killed process never leaves a
    truncated file under the final name.

    That matters here specifically because the resume logic below treats
    "the file exists" as "this division is done". A half-written file under
    the real name would be mistaken for a completed division forever.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


@dataclass
class DivisionResult:
    division: str
    status: str  # "fetched" | "skipped" | "failed"
    course_count: int = 0
    page_count: int = 0
    elapsed_seconds: float = 0.0
    error: str | None = None


@dataclass
class RunSummary:
    started_at: datetime
    finished_at: datetime
    results: list[DivisionResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(r.status == "failed" for r in self.results)


def fetch_division(
    client: TTBClient, *, division: str, sessions: list[str]
) -> tuple[dict, int]:
    """Fetch every page for one division, in a single combined query across
    all `sessions`. Returns (file payload, page count).

    One request stream per division rather than one per (division, session)
    — that matches the measured 243-request total for a full scrape. Asking
    per session would triple the request count for no benefit, since TTB
    accepts a list of sessions in one search.
    """
    courses: list[dict] = []
    page_count = 0
    for page in client.iter_pages(divisions=[division], sessions=sessions):
        courses.extend(page.courses)
        page_count += 1

    payload = {
        "division": division,
        "sessions": sessions,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "course_count": len(courses),
        "page_count": page_count,
        "courses": courses,
    }
    return payload, page_count


def run(
    *,
    raw_dir: Path,
    divisions: list[str],
    sessions: list[str],
    client: TTBClient,
    skip_existing: bool,
) -> RunSummary:
    """Fetch each division in turn, writing raw JSON as it goes.

    A division that fails (retries exhausted, or any other error) is logged
    and recorded as failed, but does not stop the run — the other divisions
    still get a chance to write their data.
    """
    raw_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc)
    results: list[DivisionResult] = []

    for i, division in enumerate(divisions):
        if i > 0 and client.delay_seconds:
            # iter_pages only delays between pages of the SAME division; the
            # gap between one division's last page and the next division's
            # first page needs its own pause.
            time.sleep(client.delay_seconds)

        path = division_output_path(raw_dir, division)

        if skip_existing and path.exists():
            existing = json.loads(path.read_text())
            results.append(
                DivisionResult(
                    division=division,
                    status="skipped",
                    course_count=existing.get("course_count", 0),
                )
            )
            logger.info(
                "division=%s status=skipped courses=%s (already on disk)",
                division, existing.get("course_count", 0),
            )
            continue

        start = time.monotonic()
        try:
            payload, page_count = fetch_division(
                client, division=division, sessions=sessions
            )
        except TTBError as exc:
            elapsed = time.monotonic() - start
            results.append(
                DivisionResult(
                    division=division, status="failed",
                    elapsed_seconds=elapsed, error=str(exc),
                )
            )
            logger.error(
                "division=%s status=failed error=%r", division, str(exc)
            )
            continue

        elapsed = time.monotonic() - start
        write_json_atomic(path, payload)
        results.append(
            DivisionResult(
                division=division, status="fetched",
                course_count=payload["course_count"],
                page_count=page_count, elapsed_seconds=elapsed,
            )
        )
        logger.info(
            "division=%s status=fetched courses=%s pages=%s elapsed_s=%.1f",
            division, payload["course_count"], page_count, elapsed,
        )

    finished_at = datetime.now(timezone.utc)
    return RunSummary(started_at=started_at, finished_at=finished_at, results=results)


def _log_summary(summary: RunSummary) -> None:
    fetched = sum(1 for r in summary.results if r.status == "fetched")
    skipped = sum(1 for r in summary.results if r.status == "skipped")
    failed = sum(1 for r in summary.results if r.status == "failed")
    total_courses = sum(r.course_count for r in summary.results)
    elapsed = (summary.finished_at - summary.started_at).total_seconds()

    logger.info(
        "run summary: divisions=%s fetched=%s skipped=%s failed=%s "
        "total_courses=%s elapsed_s=%.1f",
        len(summary.results), fetched, skipped, failed, total_courses, elapsed,
    )
    if failed:
        for r in summary.results:
            if r.status == "failed":
                logger.error("division=%s failed: %s", r.division, r.error)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vacant_scraper",
        description="Fetch every TTB division and write raw JSON to disk.",
    )
    parser.add_argument(
        "--division", action="append", choices=sorted(ttb.DIVISIONS),
        help="Fetch only this division (repeatable). Default: all divisions. "
             "Naming a division explicitly always refetches it, even if its "
             "file already exists.",
    )
    parser.add_argument(
        "--session", action="append", choices=list(ttb.SESSIONS_2026_27),
        help="Query only this session (repeatable). Default: all three "
             "2026-27 sessions. Restricting this means the written file "
             "will not represent full-year coverage.",
    )
    parser.add_argument(
        "--raw-dir", type=Path, default=Path("raw"),
        help="Directory to write {DIVISION}.json files into (default: raw/).",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Refetch every targeted division even if its file already exists.",
    )
    parser.add_argument(
        "--delay-seconds", type=float, default=1.0,
        help="Seconds to pause between requests (default: 1.0).",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    client_factory: Callable[[float], TTBClient] = lambda delay: TTBClient(
        delay_seconds=delay
    ),
) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    explicit_divisions = args.division is not None
    divisions = args.division or sorted(ttb.DIVISIONS)
    sessions = args.session or list(ttb.SESSIONS_2026_27)
    skip_existing = not (args.force or explicit_divisions)

    if args.session:
        logger.warning(
            "restricted to sessions=%s; the written file(s) will not "
            "represent full 2026-27 coverage",
            sessions,
        )

    client = client_factory(args.delay_seconds)
    try:
        summary = run(
            raw_dir=args.raw_dir,
            divisions=divisions,
            sessions=sessions,
            client=client,
            skip_existing=skip_existing,
        )
    finally:
        client.close()

    _log_summary(summary)
    return 0 if summary.ok else 1
