"""One-time extraction of `data/buildings.json` (Task 5).

This is disposable scaffolding, not maintained infra (CLAUDE.md §5: "the
extraction script is disposable; the JSON is the artifact"). It is not meant
to be re-run on a schedule — building reference data does not change.

Two data sources feed it, neither of which is map.utoronto.ca's own API:

1. **The TTB scrape itself** (`scraper/raw/*.json`, produced by
   `vacant_scraper.scrape`) supplies the exhaustive, *measured*
   `buildingCode -> map marker id` mapping — the marker id is the number
   after `#!m/` in each meeting's `buildingUrl`. This was the join CLAUDE.md
   already specifies, and it comes straight from data we already fetch; no
   need to touch map.utoronto.ca to get it.

2. **A hand-curated `BUILDING_GEO` table** (name, lat, lng, address, and
   opening hours where known) built by cross-referencing:
   - The official UofT St. George campus map legend (CODE -> full building
     name), transcribed from
     https://www.cs.toronto.edu/~six/data/uoft-map-ww126.pdf
   - OpenStreetMap's Overpass API (public, unauthenticated) for coordinates,
     addresses, and `opening_hours` tags where present.

   map.utoronto.ca runs on Concept3D, whose bulk location API
   (`api.concept3d.com/locations`) requires a paid, non-self-service key —
   confirmed by a direct probe returning 401. That path was not viable, and
   turned out to be unnecessary once (1) showed the join didn't need it.

Coverage: 63 of 66 building codes seen in a full scrape (2026-09-03) have
real coordinates below. Three (AB, BF, UY) could not be matched with
confidence to a public source and are intentionally left out — CLAUDE.md
anticipates this ("hand-correct the ~40 St. George buildings that matter").
Fill them in by hand from map.utoronto.ca before relying on them.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# CODE -> full building name, transcribed from the UofT St. George campus map
# legend (2017-18 edition; MY added by hand since Myhal Centre opened in 2018
# and postdates that map).
BUILDING_NAMES: dict[str, str] = {
    "AB": "Astronomy and Astrophysics",
    "AD": "Enrolment Services",
    "AH": "Muzzo Family Alumni Hall",
    "AN": "Annesley Hall",
    "AP": "Anthropology Building",
    "BA": "Bahen Centre for Information Technology",
    "BC": "Birge-Carnegie Library",
    "BF": "Bancroft Building",
    "BI": "Banting Institute",
    "BL": "Claude T. Bissell Building",
    "BN": "Clara Benson Building",
    "BR": "Brennan Hall",
    "BS": "St. Basil's Church",
    "BT": "Isabel Bader Theatre",
    "BW": "Burwash Hall",
    "CA": "Campus Co-op Day Care",
    "CB": "Best Institute",
    "CE": "Centre of Engineering Innovation & Entrepreneurship",
    "CG": "Canadiana Gallery",
    "CH": "Convocation Hall",
    "CM": "Student Commons",
    "CN": "89 Chestnut Residence",
    "CO": "162 St. George St.",
    "CR": "Carr Hall",
    "CS": "School of Continuing Studies",
    "CU": "Cumberland House",
    "CX": "Communication House",
    "DA": "Daniels Building",
    "DC": "Terrence Donnelly CCBR",
    "DN": "Dentistry Building",
    "DR": "J. Robert S. Prichard Alumni House",
    "EA": "Engineering Annex",
    "EH": "Elmsley Hall",
    "EJ": "Edward Johnson Building",
    "EM": "Emmanuel College",
    "EP": "Stewart Building",
    "ER": "Early Learning Centre",
    "ES": "Earth Sciences Centre",
    "EX": "Exam Centre",
    "FA": "Faculty Association",
    "FC": "Faculty Club",
    "FE": "University of Toronto Schools",
    "FG": "FitzGerald Building",
    "FH": "Falconer Hall",
    "FI": "Fields Institute",
    "GA": "Gage Building",
    "GB": "Galbraith Building",
    "GC": "Goldring Student Centre",
    "GD": "Graduate House",
    "GE": "Max Gluskin House",
    "GI": "George Ignatieff Theatre",
    "GM": "Luella Massey Studio Theatre",
    "GO": "Goldring Centre for High Performance Sport",
    "GR": "Graham (John W.) Library",
    "GS": "School of Graduate Studies",
    "GU": "Graduate Students' Union",
    "HA": "Haultain Building",
    "HH": "Hart House",
    "HI": "St. Hilda's College",
    "HS": "Health Sciences Building",
    "HU": "215 Huron St.",
    "IA": "Internal Audit",
    "IN": "Innis College",
    "IR": "Centre for Industrial Relations",
    "IS": "Innis College Student Residence",
    "JH": "Jackman Humanities Building",
    "JP": "90 Wellesley Street West",
    "KL": "J. M. Kelly Library",
    "KP": "Koffler House",
    "KS": "Koffler Student Services Centre",
    "KX": "Knox College",
    "LA": "Gerald Larkin Building",
    "LB": "Lower Burwash House",
    "LC": "Loretto College",
    "LG": "Fasken Martineau Building",
    "LI": "Lillian Massey Building",
    "LM": "Lash Miller Chemical Laboratories",
    "LW": "Faculty of Law",
    "MA": "Massey College",
    "MB": "Lassonde Mining Building",
    "MC": "Mechanical Engineering Building",
    "ME": "39 Queen's Park Cres. East",
    "MG": "Margaret Addison Hall",
    "MK": "Munk School of Global Affairs (Observatory site)",
    "ML": "McLuhan Program",
    "MM": "Macdonald-Mowat House",
    "MO": "Morrison Hall",
    "MP": "McLennan Physical Laboratories",
    "MR": "McMurrich Building",
    "MS": "Medical Sciences Building",
    "MU": "Munk School of Global Affairs (Trinity site)",
    "MY": "Myhal Centre for Engineering Innovation and Entrepreneurship",
    "NB": "North Borden Building",
    "NC": "New College",
    "NF": "Northrop Frye Hall",
    "NL": "C. David Naylor Building",
    "NR": "New College III",
    "OA": "263 McCaul St.",
    "OH": "Odette Hall",
    "OI": "OISE (Ontario Institute for Studies in Education)",
    "PB": "Leslie L. Dan Pharmacy Building",
    "PG": "45 St. George St.",
    "PI": "Pontifical Institute",
    "PR": "E. J. Pratt Library",
    "PT": "D. L. Pratt Building",
    "RB": "Fisher Rare Book Library",
    "RG": "Regis College",
    "RJ": "Rowell Jackman Hall",
    "RL": "Robarts Library",
    "RM": "254-56 McCaul St.",
    "RS": "Rosebrugh Building",
    "RT": "Rotman School of Management",
    "RU": "Rehabilitation Sciences Building",
    "RW": "Ramsay Wright Building",
    "SA": "713 Spadina Ave.",
    "SB": "South Borden Building",
    "SC": "Sussex Court",
    "SD": "Sir Daniel Wilson Residence",
    "SF": "Sandford Fleming Building",
    "SI": "Simcoe Hall",
    "SK": "Factor-Inwentash Faculty of Social Work",
    "SM": "Gerstein Science Information Centre",
    "SO": "Stewart Observatory",
    "SR": "Sam Sorbara Hall Student Residence",
    "SS": "Sidney Smith Hall",
    "SU": "40 Sussex Avenue",
    "TC": "Trinity College",
    "TF": "Teefy Hall",
    "TH": "Toronto School of Theology",
    "TR": "Soldiers' Tower",
    "TT": "455 Spadina Ave.",
    "UB": "Upper Burwash House",
    "UC": "University College",
    "UP": "University College Union",
    "VA": "Varsity Centre",
    "VC": "Victoria College",
    "VI": "Nona Macdonald Visitors Centre",
    "VP": "Varsity Pavillion",
    "WA": "123 St. George St.",
    "WB": "Wallberg Building",
    "WE": "Wetmore Hall",
    "WI": "Wilson Hall",
    "WO": "Woodsworth College Residence",
    "WR": "McCarthy House / Jackman Institute of Child Study",
    "WS": "Warren Stevens Building",
    "WT": "Whitney Hall",
    "WW": "Woodsworth College",
    "WY": "Wycliffe College",
    "XG": "665 Spadina Ave.",
    "ZC": "88 College St.",
}

# CODE -> (lat, lng, address, opening_hours). Coordinates and addresses are
# from OpenStreetMap (Overpass API), matched to BUILDING_NAMES by name.
# `opening_hours` is OSM's own syntax where an entry carried the tag; parsed
# into the per-weekday minutes-since-midnight shape by `parse_opening_hours`.
# Buildings not listed here matched no OSM record with confidence and need
# manual lookup (see module docstring).
BUILDING_GEO: dict[str, tuple[float, float, str | None, str | None]] = {
    "AH": (43.6647637, -79.3901376, "121 St. Joseph Street", None),
    "AP": (43.6598666, -79.3984688, "19 Ursula Franklin Street", None),
    "BA": (43.6597085, -79.3974539, "40 St George Street", None),
    "BN": (43.6629833, -79.4002584, "320 Huron Street", None),
    "BR": (43.6663937, -79.3897654, "81A St. Mary Street", None),
    "BT": (43.6672514, -79.3923649, "93 Charles Street West", None),
    "CH": (43.660776, -79.3954478, "31 King's College Circle", None),
    "CR": (43.6652328, -79.3904148, "100 St. Joseph Street", None),
    "DA": (43.6596491, -79.4007115, "1 Spadina Crescent", None),
    "EJ": (43.6666122, -79.3945874, "80 Queen's Park", None),
    "EM": (43.6667142, -79.3926504, "75 Queen's Park", None),
    "EP": (43.6591984, -79.3919911, None, None),
    "ES": (43.6607775, -79.3996512, None, None),
    "FE": (43.6665211, -79.402276, "371 Bloor Street West", None),
    "GB": (43.6599593, -79.3959662, "35 St George Street", None),
    "GI": (43.6658417, -79.3971179, None, None),
    "GM": (43.6643593, -79.4011292, "4 Glen Morris Street", None),
    "HA": (43.6599224, -79.3936379, "170 College Street", None),
    "HI": (43.665726, -79.3979508, "44 Devonshire Place", None),
    "HS": (43.6590815, -79.3927635, "155 College Street", None),
    "IN": (43.6656204, -79.399593, "2 Sussex Avenue", None),
    "JH": (43.6677314, -79.4003457, "170 St George Street", None),
    "JP": (43.664231, -79.3893241, "90 Wellesley Street West", None),
    "KP": (43.6605786, -79.4006308, "569 Spadina Avenue", None),
    "LA": (43.6656724, -79.3969449, "15 Devonshire Place", None),
    "LI": (43.6683959, -79.3935362, "125 Queen's Park", None),
    "LM": (43.6615127, -79.3982727, "80 St George Street", None),
    "MB": (43.6595254, -79.3934676, "170 College Street", None),
    "MC": (43.6600562, -79.3938368, "5 King's College Road", None),
    "MK": (43.6631663, -79.3946165, "12 Hart House Circle", None),
    "MP": (43.6608184, -79.398389, "255 Huron Street", None),
    "MS": (43.6607973, -79.3933729, "1 King's College Circle", None),
    "MY": (43.6607638, -79.3965358, "55 St George Street", None),
    "NB": (43.6604056, -79.4001115, "563 Spadina Crescent", None),
    "NF": (43.6664141, -79.3921389, "73 Queen's Park Crescent East", None),
    "NL": (43.6603578, -79.3918974, "6 Queen's Park Crescent West", None),
    "OH": (43.6663535, -79.3886433, None, None),
    "OI": (43.6683, -79.3985, "252 Bloor Street West", None),
    "PB": (43.6599499, -79.3917767, "144 College Street", None),
    "PR": (43.6663202, -79.3912921, "71 Queen's Park Crescent East", None),
    "PT": (43.6596106, -79.3948157, "6 King's College Road", None),
    "RL": (
        43.6643897, -79.3995759, "130 St George Street",
        "Mo-Fr 08:30-23:00; Sa 09:00-22:00; Su 10:00-22:00",
    ),
    "RS": (43.6600629, -79.3932695, "164 College Street", None),
    "RT": (43.6653599, -79.3984429, "105 St George Street", None),
    "RW": (43.6632684, -79.39906, "25 Harbord Street", None),
    "SB": (43.6600989, -79.3997906, "487 Spadina Crescent", None),
    "SD": (43.662544, -79.3971848, "73-75 St George Street", None),
    "SF": (43.6601523, -79.3952176, "10 King's College Road", None),
    "SK": (43.6682995, -79.3977668, "246 Bloor Street West", None),
    "SM": (
        43.662088, -79.3936403, "9 King's College Circle",
        "Mo-Th 08:30-23:00; Fr 08:30-22:00; Sa 09:00-22:00; Su 10:00-22:00",
    ),
    "SS": (
        43.6624674, -79.3988052, "100 St George Street",
        "Mo-Fr 10:00-18:30; Sa-Su off",
    ),
    "SU": (43.6650438, -79.4018606, "40 Sussex Avenue", None),
    "TC": (43.6653146, -79.3957278, "6 Hoskin Avenue", None),
    "TF": (43.6653867, -79.3908241, "59 Queen's Park Crescent East", None),
    "UC": (43.6628344, -79.3958563, "15 King's College Circle", None),
    "UP": (43.6633987, -79.397575, "79 St George Street", None),
    "VC": (43.6669263, -79.3919559, "91 Charles Street West", None),
    "WB": (43.659188, -79.3952423, "184-200 College Street", None),
    "WE": (43.6621286, -79.4004026, "300 Huron Street", None),
    "WI": (43.6618546, -79.4007161, "40 Willcocks Street", None),
    "WO": (43.6672835, -79.3991697, "321 Bloor Street West", None),
    "WS": (43.6627537, -79.4010325, "55 Harbord Street", None),
    "WW": (43.6668706, -79.3990601, None, None),
}

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# Used when a building has no `opening_hours` tag in BUILDING_GEO. A generic
# academic-building schedule, not a measurement — CLAUDE.md treats building
# hours as advisory ("Scraped hours will sometimes be wrong... hiding a
# genuinely quiet building is worse than showing a caveat"), so an
# approximate default here is acceptable as long as it's not a hard filter.
DEFAULT_HOURS = {
    **{d: {"open": 8 * 60, "close": 23 * 60} for d in DAYS[:5]},
    **{d: {"open": 9 * 60, "close": 22 * 60} for d in DAYS[5:]},
}

_DAY_RANGE_RE = re.compile(
    r"(Mo|Tu|We|Th|Fr|Sa|Su)(?:-(Mo|Tu|We|Th|Fr|Sa|Su))?\s+"
    r"(?:(\d{2}):(\d{2})-(\d{2}):(\d{2})|off)"
)
_DAY_ORDER = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]


def parse_opening_hours(osm_hours: str) -> dict[str, dict[str, int]]:
    """Parse the small subset of OSM `opening_hours` syntax actually seen in
    this dataset: semicolon-separated `Dd[-Dd] HH:MM-HH:MM` or `Dd-Dd off`
    clauses. Not a general RFC parser — falls back to `DEFAULT_HOURS` for any
    day a clause doesn't cover.
    """
    hours = dict(DEFAULT_HOURS)
    for clause in osm_hours.split(";"):
        clause = clause.strip()
        m = _DAY_RANGE_RE.match(clause)
        if not m:
            continue
        start_day, end_day, oh, om, ch, cm = m.groups()
        end_day = end_day or start_day
        start_idx = _DAY_ORDER.index(start_day)
        end_idx = _DAY_ORDER.index(end_day)
        for i in range(start_idx, end_idx + 1):
            day = DAYS[i]
            if oh is None:
                hours[day] = {"open": 0, "close": 0}  # "off"
            else:
                hours[day] = {
                    "open": int(oh) * 60 + int(om),
                    "close": int(ch) * 60 + int(cm),
                }
    return hours


def extract_code_to_map_id(raw_dir: Path) -> dict[str, str]:
    """Scan every raw division file for `(buildingCode, marker id)` pairs.

    The marker id is the number after `#!m/` in `buildingUrl`. CLAUDE.md's
    join decision assumes this is 1:1; we assert that here rather than
    silently picking one if the assumption ever breaks.
    """
    mapping: dict[str, set[str]] = {}
    for path in sorted(raw_dir.glob("*.json")):
        data = json.loads(path.read_text())
        for course in data.get("courses", []):
            for section in course.get("sections") or []:
                for meeting in section.get("meetingTimes") or []:
                    building = meeting.get("building")
                    if not building:
                        continue
                    code = building.get("buildingCode")
                    url = building.get("buildingUrl") or ""
                    marker = re.search(r"#!m/(\d+)", url)
                    if code and marker:
                        mapping.setdefault(code, set()).add(marker.group(1))

    bad = {code: ids for code, ids in mapping.items() if len(ids) > 1}
    if bad:
        raise ValueError(f"building code(s) mapped to multiple marker ids: {bad}")

    return {code: next(iter(ids)) for code, ids in mapping.items()}


def build_buildings(code_to_map_id: dict[str, str]) -> tuple[list[dict[str, Any]], list[str]]:
    """Assemble the `data/buildings.json` records.

    Returns (records, codes_needing_manual_completion). A code with no
    `BUILDING_GEO` entry is skipped rather than written with fabricated
    coordinates — CLAUDE.md's "hand-correct" step is expected to fill these
    in, not this script.
    """
    records = []
    needs_manual = []
    for code in sorted(code_to_map_id):
        geo = BUILDING_GEO.get(code)
        if geo is None:
            needs_manual.append(code)
            continue
        lat, lng, address, opening_hours = geo
        records.append({
            "code": code,
            "map_id": int(code_to_map_id[code]),
            "name": BUILDING_NAMES.get(code, code),
            "lat": lat,
            "lng": lng,
            "address": address,
            "hours": (
                parse_opening_hours(opening_hours)
                if opening_hours
                else dict(DEFAULT_HOURS)
            ),
        })
    return records, needs_manual


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--raw-dir", type=Path, default=Path("raw"),
        help="Directory of {DIVISION}.json files from vacant_scraper.scrape "
             "(default: raw/).",
    )
    parser.add_argument(
        "--out", type=Path, default=Path("../data/buildings.json"),
        help="Output path for the buildings JSON (default: ../data/buildings.json).",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(levelname)-7s %(message)s")

    code_to_map_id = extract_code_to_map_id(args.raw_dir)
    logger.info("found %d building codes across raw scrape files", len(code_to_map_id))

    records, needs_manual = build_buildings(code_to_map_id)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(records, indent=2) + "\n")

    logger.info("wrote %d buildings to %s", len(records), args.out)
    if needs_manual:
        logger.warning(
            "%d code(s) have no geo data and were skipped -- fill in "
            "BUILDING_GEO by hand from map.utoronto.ca and re-run: %s",
            len(needs_manual), sorted(needs_manual),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
