#!/usr/bin/env python3
"""
USAGE
-----
    # Colorado delegation, current session, auto-detect the end of the range
    python vote_scraper.py --year 2026

    # Explicit member list and explicit roll call range
    python vote_scraper.py --year 2026 --start 1 --end 250 \\
        --members B000825 C001121 C001137 D000197 E000300 H001100 N000191 P000620

    # Multiple years (e.g. both sessions of a Congress)
    python vote_scraper.py --year 2025 --year 2026

    # Daily cron/scheduled run: only fetch what's new since last time
    python vote_scraper.py --year 2026 --incremental

Members come from members.json, for the Congress that the --year values
belong to. Output defaults to data/<congress>/all_votes.csv: one row per
roll call, one column per member's vote, then House-wide tally columns.
"""

import argparse
import csv
import json
import logging
import shutil
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

log = logging.getLogger("vote_scraper")

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

ROLL_URL = "https://clerk.house.gov/evs/{year}/roll{roll:03d}.xml"

# Colorado's House delegation for each Congress lives in members.json, which
# index.html also reads. IDs are Bioguide/Clerk member IDs -- the same IDs
# that appear as name-id="..." attributes inside each roll call XML.
MEMBERS_FILE = Path(__file__).resolve().parent / "members.json"

# House-wide tally columns, appended after the member columns so older
# readers of the CSV (which look columns up by name) are unaffected.
TALLY_COLUMNS = [
    "Yea Total",
    "Nay Total",
    "Present Total",
    "NV Total",
    "Dem Yea",
    "Dem Nay",
    "Rep Yea",
    "Rep Nay",
]
PARTY_PREFIX = {"Democratic": "Dem", "Republican": "Rep"}


def congress_for_year(year: int) -> int:
    """The 1st Congress began in 1789; each Congress spans two years."""
    return (year - 1789) // 2 + 1


def load_members(congress: int) -> dict[str, str]:
    """Return {member_id: csv_name} for one Congress from members.json,
    sorted by ID so the CSV column order is stable."""
    with MEMBERS_FILE.open(encoding="utf-8") as f:
        config = json.load(f)
    entry = config.get("congresses", {}).get(str(congress))
    if not entry or not entry.get("members"):
        raise SystemExit(
            f"No members listed for the {congress}th Congress in {MEMBERS_FILE.name}. "
            "Add the delegation's Bioguide IDs there before scraping."
        )
    return {m["id"]: m["csvName"] for m in sorted(entry["members"], key=lambda m: m["id"])}

REQUEST_TIMEOUT = 15  # seconds
DELAY_BETWEEN_REQUESTS = 0.5  # seconds -- be a polite scraper
MAX_CONSECUTIVE_MISSES = 6  # stop walking roll numbers after this many 404s in a row

# In --incremental mode, re-fetch this many of the most-recently-known roll
# calls even though we already have them, in case the Clerk corrected one
# after initial publication. Cheap insurance against stale/wrong data.
INCREMENTAL_RECHECK_LAST_N = 3


@dataclass
class RollCallResult:
    year: int
    roll_number: int
    congress: Optional[str] = None
    session: Optional[str] = None
    date: Optional[str] = None
    time: Optional[str] = None
    bill_number: Optional[str] = None
    vote_question: Optional[str] = None
    vote_type: Optional[str] = None
    result: Optional[str] = None
    description: Optional[str] = None
    votes: dict = field(default_factory=dict)  # member_id -> vote string
    totals: dict = field(default_factory=dict)  # TALLY_COLUMNS name -> int


def build_session() -> requests.Session:
    """A requests Session with retry/backoff for transient failures, so a
    single flaky response doesn't kill the whole run."""
    session = requests.Session()
    retries = Retry(
        total=4,
        backoff_factor=1.5,  # 0s, 1.5s, 3s, 6s...
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (compatible; CongressionalVoteResearch/2.0; "
            "+https://clerk.house.gov/Votes)",
            "Accept": "application/xml,text/xml,*/*",
        }
    )
    return session


def fetch_roll_call(
    session: requests.Session, year: int, roll_number: int, member_ids: set[str]
) -> Optional[RollCallResult]:
    """Fetch and parse a single roll call XML document. Returns None if the
    roll call doesn't exist (404) -- that's an expected, non-error condition
    signaling we've either gone past the end of the session or hit a gap."""
    url = ROLL_URL.format(year=year, roll=roll_number)

    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT)
    except requests.exceptions.RequestException as exc:
        log.warning("Request error for roll %d (%d): %s", roll_number, year, exc)
        return None

    if response.status_code == 404:
        return None
    if response.status_code != 200:
        log.warning(
            "Unexpected status %d for roll %d (%d) -- treating as missing",
            response.status_code,
            roll_number,
            year,
        )
        return None

    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as exc:
        log.warning("Malformed XML for roll %d (%d): %s", roll_number, year, exc)
        return None

    meta = root.find("vote-metadata")
    if meta is None:
        log.warning("No vote-metadata in roll %d (%d) -- skipping", roll_number, year)
        return None

    def text(tag: str) -> Optional[str]:
        el = meta.find(tag)
        return el.text.strip() if el is not None and el.text else None

    action_time_el = meta.find("action-time")
    action_time = action_time_el.text.strip() if action_time_el is not None and action_time_el.text else None

    result = RollCallResult(
        year=year,
        roll_number=roll_number,
        congress=text("congress"),
        session=text("session"),
        date=text("action-date"),
        time=action_time,
        bill_number=text("legis-num"),
        vote_question=text("vote-question"),
        vote_type=text("vote-type"),
        result=text("vote-result"),
        description=text("vote-desc"),
    )

    # Pull out only the members we're tracking. Every <recorded-vote> holds
    # one <legislator name-id="..."> plus a sibling <vote> value (Aye/No/
    # Present/Not Voting, or a name in the rare case of a Speaker election).
    vote_data = root.find("vote-data")
    if vote_data is not None:
        for recorded in vote_data.findall("recorded-vote"):
            legislator = recorded.find("legislator")
            vote_el = recorded.find("vote")
            if legislator is None or vote_el is None:
                continue
            member_id = legislator.get("name-id")
            if member_id in member_ids:
                result.votes[member_id] = (vote_el.text or "").strip()

    result.totals = parse_totals(meta.find("vote-totals"))
    return result


def parse_totals(vote_totals: Optional[ET.Element]) -> dict:
    """Pull House-wide and per-party Yea/Nay counts out of <vote-totals>.
    Missing pieces (e.g. a quorum call or Speaker election, which use a
    different layout) are simply left out, so those cells stay blank."""
    totals: dict = {}
    if vote_totals is None:
        return totals

    def count(el: ET.Element, tag: str) -> Optional[int]:
        child = el.find(tag)
        try:
            return int(child.text.strip()) if child is not None and child.text else None
        except ValueError:
            return None

    overall = vote_totals.find("totals-by-vote")
    if overall is not None:
        for tag, column in (
            ("yea-total", "Yea Total"),
            ("nay-total", "Nay Total"),
            ("present-total", "Present Total"),
            ("not-voting-total", "NV Total"),
        ):
            value = count(overall, tag)
            if value is not None:
                totals[column] = value

    for party_el in vote_totals.findall("totals-by-party"):
        prefix = PARTY_PREFIX.get((party_el.findtext("party") or "").strip())
        if not prefix:
            continue  # independents aren't part of the party-loyalty math
        for tag, suffix in (("yea-total", "Yea"), ("nay-total", "Nay")):
            value = count(party_el, tag)
            if value is not None:
                totals[f"{prefix} {suffix}"] = value
    return totals


def scrape_year(
    session: requests.Session,
    year: int,
    member_ids: set[str],
    start: int,
    end: Optional[int],
) -> list[RollCallResult]:
    """Walk roll call numbers for a given year until we either hit `end`
    or accumulate MAX_CONSECUTIVE_MISSES consecutive missing roll calls."""
    results: list[RollCallResult] = []
    consecutive_misses = 0
    roll_number = start

    while True:
        if end is not None and roll_number > end:
            log.info("Reached configured --end=%d for year %d. Stopping.", end, year)
            break

        record = fetch_roll_call(session, year, roll_number, member_ids)

        if record is None:
            consecutive_misses += 1
            log.debug(
                "Roll %d (%d) missing (%d consecutive miss(es))",
                roll_number,
                year,
                consecutive_misses,
            )
            if end is None and consecutive_misses >= MAX_CONSECUTIVE_MISSES:
                log.info(
                    "Hit %d consecutive missing roll calls for %d -- assuming "
                    "end of available data. Stopping at roll %d.",
                    MAX_CONSECUTIVE_MISSES,
                    year,
                    roll_number,
                )
                break
        else:
            consecutive_misses = 0
            results.append(record)
            log.info(
                "Year %d roll %d: %s | %s | %d/%d members found",
                year,
                roll_number,
                record.bill_number or "(no bill)",
                record.result or "?",
                len(record.votes),
                len(member_ids),
            )

        roll_number += 1
        time.sleep(DELAY_BETWEEN_REQUESTS)

    log.info("Year %d: collected %d roll calls.", year, len(results))
    return results


def result_to_row(r: RollCallResult, member_ids: dict[str, str]) -> dict:
    """Build one CSV row (as a plain dict matching write_csv's fieldnames)
    from a freshly-scraped RollCallResult."""
    row = {
        "Year": r.year,
        "Congress": r.congress,
        "Session": r.session,
        "Roll Call Number": r.roll_number,
        "Date": r.date,
        "Time": r.time,
        "Bill Number": r.bill_number,
        "Vote Question": r.vote_question,
        "Vote Type": r.vote_type,
        "Result": r.result,
        "Description": r.description,
    }
    for member_id, name in member_ids.items():
        row[f"{name} ({member_id})"] = r.votes.get(member_id, "")
    for column in TALLY_COLUMNS:
        row[column] = r.totals.get(column, "")
    return row


def csv_fieldnames(member_ids: dict[str, str]) -> list[str]:
    return [
        "Year",
        "Congress",
        "Session",
        "Roll Call Number",
        "Date",
        "Time",
        "Bill Number",
        "Vote Question",
        "Vote Type",
        "Result",
        "Description",
    ] + [f"{name} ({member_id})" for member_id, name in member_ids.items()] + TALLY_COLUMNS


def write_csv(all_results: list[RollCallResult], member_ids: dict[str, str], output_path: Path) -> None:
    fieldnames = csv_fieldnames(member_ids)
    rows = [result_to_row(r, member_ids) for r in all_results]

    # Explicit lineterminator: csv.DictWriter defaults to '\r\n' (the Excel
    # dialect). Left unset, the *first* incremental run against a file that
    # already has plain '\n' endings flips every single line, making git
    # see a full-file rewrite even when no data actually changed. Forcing
    # '\n' here makes output deterministic run-over-run.
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    log.info("Wrote %d rows to %s", len(rows), output_path)


def write_rows(rows: list[dict], member_ids: dict[str, str], output_path: Path) -> None:
    """Like write_csv, but for rows that are already plain dicts -- used by
    the incremental path, where rows may be a mix of freshly-scraped data
    (via result_to_row) and rows loaded back in from the existing CSV."""
    fieldnames = csv_fieldnames(member_ids)
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    log.info("Wrote %d rows to %s", len(rows), output_path)


def load_existing_rows(output_path: Path) -> dict[tuple[int, int], dict]:
    """Load a previously-written CSV, keyed by (year, roll_number), for
    incremental merging. Returns {} if the file doesn't exist or can't be
    parsed -- in either case we just fall back to a full scrape."""
    if not output_path.exists():
        return {}
    existing: dict[tuple[int, int], dict] = {}
    try:
        with output_path.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    year = int(row["Year"])
                    roll = int(row["Roll Call Number"])
                except (KeyError, ValueError, TypeError):
                    continue
                existing[(year, roll)] = row
    except (OSError, csv.Error) as exc:
        log.warning("Could not read existing output file %s (%s) -- doing a full scrape instead.", output_path, exc)
        return {}
    return existing


def resume_start_for_year(existing: dict[tuple[int, int], dict], year: int, requested_start: int) -> int:
    """Where should this year's walk resume? Just past the highest roll
    number already on file, minus a small safety window to re-check for
    late corrections -- but never earlier than the user's --start."""
    known_rolls = [roll for (y, roll) in existing.keys() if y == year]
    if not known_rolls:
        return requested_start
    resume = max(known_rolls) - INCREMENTAL_RECHECK_LAST_N + 1
    return max(resume, requested_start)


def merge_results(
    existing: dict[tuple[int, int], dict],
    new_results: list[RollCallResult],
    member_ids: dict[str, str],
) -> list[dict]:
    """Merge freshly-scraped results into the previously-loaded rows.
    New data wins on any (year, roll_number) that appears in both --
    that's how a corrected vote record ends up overwriting the stale one."""
    merged: dict[tuple[int, int], dict] = dict(existing)
    for r in new_results:
        merged[(r.year, r.roll_number)] = result_to_row(r, member_ids)
    return [merged[key] for key in sorted(merged.keys())]


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape House roll call votes from the Clerk's official per-roll-call XML feed.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--year",
        type=int,
        action="append",
        required=True,
        help="Year to scrape (repeatable, e.g. --year 2025 --year 2026)",
    )
    parser.add_argument(
        "--members",
        nargs="+",
        default=None,
        help="Bioguide/Clerk member IDs to track (default: the Colorado delegation "
        "for this Congress, from members.json)",
    )
    parser.add_argument(
        "--congress",
        type=int,
        default=None,
        help="Congress number (default: derived from --year, e.g. 2025/2026 -> 119)",
    )
    parser.add_argument("--start", type=int, default=1, help="First roll call number to try (default: 1)")
    parser.add_argument(
        "--end",
        type=int,
        default=None,
        help="Last roll call number to try. If omitted, stops automatically after "
        f"{MAX_CONSECUTIVE_MISSES} consecutive misses.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output CSV path (default: data/<congress>/all_votes.csv)",
    )
    parser.add_argument(
        "--copy-to",
        type=Path,
        default=None,
        help="Also copy the finished CSV here. The nightly workflow uses this to keep "
        "the root all_votes.csv current for the previously deployed page.",
    )
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="Resume from the existing --output file instead of re-scraping the "
        "whole session. Re-checks the last "
        f"{INCREMENTAL_RECHECK_LAST_N} known roll calls per year in case of late "
        "corrections. Falls back to a full scrape if --output doesn't exist yet.",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    return parser.parse_args(argv)


def emit_github_output(key: str, value: str) -> None:
    """Write a step output for a GitHub Actions workflow to pick up (e.g.
    to build a job summary or a richer commit message). Safe no-op when
    run outside Actions, such as locally on your own machine -- GITHUB_OUTPUT
    just won't be set."""
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as f:
        f.write(f"{key}={value}\n")


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    congresses = {congress_for_year(y) for y in args.year}
    if args.congress is None and len(congresses) > 1:
        log.error(
            "--year values span more than one Congress (%s); scrape each Congress separately.",
            ", ".join(str(c) for c in sorted(congresses)),
        )
        return 1
    congress = args.congress or congresses.pop()

    if args.members:
        # User-supplied IDs: we don't have display names for these, so just
        # label them by their ID.
        member_ids = {m: m for m in args.members}
    else:
        member_ids = load_members(congress)

    if args.output is None:
        args.output = Path("data") / str(congress) / "all_votes.csv"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    log.info("Congress %d: tracking %d members -> %s", congress, len(member_ids), args.output)

    existing_rows: dict[tuple[int, int], dict] = {}
    if args.incremental:
        existing_rows = load_existing_rows(args.output)
        if existing_rows:
            log.info(
                "Incremental mode: found %d previously-scraped roll call(s) in %s",
                len(existing_rows),
                args.output,
            )
        else:
            log.info(
                "Incremental mode: no existing data at %s (or it couldn't be read) -- "
                "doing a full scrape.",
                args.output,
            )

    session = build_session()
    all_results: list[RollCallResult] = []

    for year in args.year:
        year_start = args.start
        if args.incremental:
            year_start = resume_start_for_year(existing_rows, year, args.start)
            if year_start > args.start:
                log.info(
                    "Year %d: resuming from roll %d (re-checking the last %d already on file "
                    "in case of corrections)",
                    year,
                    year_start,
                    INCREMENTAL_RECHECK_LAST_N,
                )

        log.info("Starting year %d (roll calls %d..%s)", year, year_start, args.end or "auto")
        year_results = scrape_year(
            session=session,
            year=year,
            member_ids=set(member_ids.keys()),
            start=year_start,
            end=args.end,
        )
        all_results.extend(year_results)

    if not all_results and not existing_rows:
        log.error("No roll calls were collected. Check the --year/--start/--end values.")
        return 1

    if args.incremental:
        merged_rows = merge_results(existing_rows, all_results, member_ids)
        new_or_updated = len(all_results)
        write_rows(merged_rows, member_ids, args.output)
        log.info(
            "Done. %d new/updated roll call(s) this run, %d total in %s.",
            new_or_updated,
            len(merged_rows),
            args.output,
        )
        emit_github_output("new_count", str(new_or_updated))
        emit_github_output("total_count", str(len(merged_rows)))
    else:
        write_csv(all_results, member_ids, args.output)
        log.info(
            "Done. %d roll calls across %d year(s), %d members tracked.",
            len(all_results),
            len(args.year),
            len(member_ids),
        )
        emit_github_output("new_count", str(len(all_results)))
        emit_github_output("total_count", str(len(all_results)))

    if args.copy_to and args.copy_to.resolve() != args.output.resolve():
        shutil.copyfile(args.output, args.copy_to)
        log.info("Copied %s -> %s", args.output, args.copy_to)
    return 0


if __name__ == "__main__":
    sys.exit(main())
