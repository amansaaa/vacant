"""Entrypoint for `python -m vacant_scraper` — runs the full scrape job."""

from vacant_scraper.scrape import main

if __name__ == "__main__":
    raise SystemExit(main())
