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
import html
import json
import logging
import shutil
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

# Which votes are final, party majorities, delegates and so on: one home for
# the rules, shared with vote_info.json (which the page reads).
from vote_rules import NON_STATE_CODES, final_flags, load_bills, party_majority, roll_key, write_vote_info

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


# Exit code when members.json has no delegation for the Congress yet, e.g.
# the first days of a new Congress. The nightly workflow reports it as a
# warning rather than a failure, so the rest of the run still happens.
EXIT_NO_DELEGATION = 3


class NoDelegation(Exception):
    pass


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def load_members(congress: int) -> dict[str, str]:
    """Return {member_id: csv_name} for one Congress from members.json,
    sorted by ID so the CSV column order is stable."""
    with MEMBERS_FILE.open(encoding="utf-8") as f:
        config = json.load(f)
    entry = config.get("congresses", {}).get(str(congress))
    if not entry or not entry.get("members"):
        raise NoDelegation(
            f"No members listed for the {ordinal(congress)} Congress in {MEMBERS_FILE.name}. "
            "Add the Colorado delegation there (see NEW_CONGRESS.md); nothing was scraped for it."
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
    # Every House member on this roll call: member_id -> (code, party, state, name).
    # Feeds the House-wide matrix used for party-loyalty rankings.
    house: dict = field(default_factory=dict)


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
        # A few roll calls have no XML even though the Clerk lists them --
        # e.g. 2007's roll calls 1000-1186. Their HTML vote page has the same
        # data, so fall back to that before calling the roll call missing.
        return fetch_roll_call_page(session, year, roll_number, member_ids)
    if response.status_code != 200:
        log.warning(
            "Unexpected status %d for roll %d (%d) -- treating as missing",
            response.status_code,
            roll_number,
            year,
        )
        return None

    return parse_roll_call_xml(response.content, year, roll_number, member_ids)


def parse_roll_call_xml(content: bytes, year: int, roll_number: int, member_ids: set[str]) -> Optional[RollCallResult]:
    """Parse one Clerk roll-call XML document. None if it has no vote data
    (the Clerk serves empty documents past the end of a session)."""
    try:
        root = ET.fromstring(content)
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
            vote_text = (vote_el.text or "").strip()
            if member_id in member_ids:
                result.votes[member_id] = vote_text
            if member_id:
                result.house[member_id] = (
                    vote_code(vote_text),
                    legislator.get("party") or "",
                    legislator.get("state") or "",
                    legislator.get("unaccented-name") or (legislator.text or "").strip(),
                )

    result.totals = parse_totals(meta.find("vote-totals"))
    return result


VOTE_PAGE_URL = "https://clerk.house.gov/Votes/{year}{roll}"
PAGE_PARTY_CODES = {"Democratic": "D", "Republican": "R", "Independent": "I"}
_TAG = re.compile(r"<[^>]+>")


def _page_text(fragment: str) -> str:
    return " ".join(html.unescape(_TAG.sub(" ", fragment)).split())


def parse_vote_page(page: str, year: int, roll_number: int, member_ids: set[str]) -> Optional[RollCallResult]:
    """Parse a Clerk HTML vote page (clerk.house.gov/Votes/<year><roll>) into
    the same RollCallResult, in the same formats, that the XML gives. Returns
    None if the page has no member votes (e.g. a roll call that doesn't exist)."""
    def find(pattern: str) -> Optional[str]:
        m = re.search(pattern, page, re.S)
        return _page_text(m.group(1)) if m else None

    heading = find(r'<div class="col-md-9 roll-call-first-col">\s*<div class="first-row heading"[^>]*>(.*?)</div>') or ""
    when = re.match(r"(\w{3}) (\d{1,2}), (\d{4}), (\d{1,2}):(\d{2}) ([AP]M)", heading)
    where = re.search(r"(\d+)th Congress, (\w+) Session", heading)
    bill = find(r'<span[^>]*class="legisNum"[^>]*>(.*?)</span>')

    result = RollCallResult(
        year=year,
        roll_number=roll_number,
        congress=where.group(1) if where else None,
        session=where.group(2) if where else None,
        # "24-Oct-2007" and "3:04 PM", as in the XML.
        date=f"{int(when.group(2))}-{when.group(1)}-{when.group(3)}" if when else None,
        time=f"{int(when.group(4))}:{when.group(5)} {when.group(6)}" if when else None,
        # "H. R. 505" -> "H R 505", as in the XML.
        bill_number=" ".join(bill.replace(".", " ").upper().split()) if bill else None,
        vote_question=find(r'<p class="roll-call-first-row"[^>]*><label>Vote Question:</label>(.*?)</p>'),
        vote_type=find(r'aria-label="Vote Type, ([^"]*)"'),
        result=find(r'aria-label="Status, ([^"]*)"'),
        description=find(r'<p class="roll-call-description">\s*<label[^>]*></label>(.*?)</p>'),
    )

    if not result.bill_number:
        # The XML names votes that aren't on a bill; the page leaves them blank.
        question = (result.vote_question or "").lower()
        result.bill_number = (
            "QUORUM" if result.vote_type == "QUORUM"
            else "JOURNAL" if question.startswith("on approving the journal")
            else "ADJOURN" if question.startswith("on motion to adjourn")
            else None if question.startswith("election of the speaker")
            else "MOTION"
        )

    for row in re.findall(r"<tr>(.*?)</tr>", page, re.S):
        id_m = re.search(r'href="[^"]*(?:/Members/|index=)([A-Z]\d{6})"', row)
        vote_m = re.search(r'<td data-label="vote">(.*?)</td>', row, re.S)
        if not id_m or not vote_m:
            continue
        member_id = id_m.group(1)
        vote_text = _page_text(vote_m.group(1))
        name = find_in(row, r'<td data-label="member" hidden>(.*?)</td>')
        party = find_in(row, r'<td data-label="party">(.*?)</td>')
        state = find_in(row, r'<td data-label="state" class="visible-sm visible-xs">(.*?)</td>')
        if state in NON_STATE_CODES:
            state = "XX"  # as in the XML, for delegates and the resident commissioner
        if member_id in member_ids:
            result.votes[member_id] = vote_text
        result.house[member_id] = (vote_code(vote_text), PAGE_PARTY_CODES.get(party, party), state, name)

    if not result.house:
        return None

    # Recorded votes label the totals "Aye"/"No"; yea-and-nay votes "yea"/"nay".
    for label, column in (("yea|aye", "Yea Total"), ("nay|no", "Nay Total"),
                          ("present", "Present Total"), ("not voting", "NV Total")):
        m = re.search(rf'<p tabindex="0" aria-label="(?:{label}), (\d+)">', page, re.I)
        if m:
            result.totals[column] = int(m.group(1))
    for party, prefix in PARTY_PREFIX.items():
        m = re.search(rf'aria-label="{party}">{party}</td>(.*?)</tr>', page, re.S)
        if not m:
            continue
        for label, suffix in (("yea", "Yea"), ("nay", "Nay")):
            n = re.search(rf'aria-label="(\d+) vote on {label}"', m.group(1))
            if n:
                result.totals[f"{prefix} {suffix}"] = int(n.group(1))
    return result


def find_in(fragment: str, pattern: str) -> str:
    m = re.search(pattern, fragment, re.S)
    return _page_text(m.group(1)) if m else ""


def fetch_roll_call_page(
    session: requests.Session, year: int, roll_number: int, member_ids: set[str]
) -> Optional[RollCallResult]:
    url = VOTE_PAGE_URL.format(year=year, roll=roll_number)
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT * 2, headers={"Accept": "text/html,*/*"})
    except requests.exceptions.RequestException as exc:
        log.warning("Request error for vote page %d (%d): %s", roll_number, year, exc)
        return None
    if response.status_code != 200:
        return None
    result = parse_vote_page(response.text, year, roll_number, member_ids)
    if result is not None:
        log.info("Roll %d (%d): no XML; used the Clerk's HTML vote page", roll_number, year)
    return result


def vote_code(vote_text: str) -> str:
    """One-letter code for the House-wide matrix."""
    if vote_text in ("Yea", "Aye"):
        return "Y"
    if vote_text in ("Nay", "No"):
        return "N"
    if vote_text == "Present":
        return "P"
    if vote_text == "Not Voting":
        return "V"
    return "O" if vote_text else ""  # "O": e.g. a name in a Speaker election


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


# --------------------------------------------------------------------------
# House-wide matrix and party-loyalty rankings
# --------------------------------------------------------------------------
#
# house_votes.csv: one row per House member, one column per roll call
# ("2026-247"), cells are vote_code() letters; blank = not on that roll call.
# house_loyalty.json: per-member loyalty stats and ranks within party,
# computed over the whole matrix. The page loads only the JSON.

HOUSE_MATRIX_NAME = "house_votes.csv"
HOUSE_LOYALTY_NAME = "house_loyalty.json"
HOUSE_META_COLUMNS = ["id", "name", "party", "state"]
MIN_RANKED_VOTES = 100  # counted (party-majority yea/nay) votes needed to be ranked
def load_house_matrix(path: Path) -> dict[str, dict]:
    """{member_id: {"meta": {name, party, state}, "codes": {roll_key: code}}}"""
    matrix: dict[str, dict] = {}
    if not path.exists():
        return matrix
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            codes = {k: v for k, v in row.items() if k not in HOUSE_META_COLUMNS and v}
            matrix[row["id"]] = {
                "meta": {"name": row["name"], "party": row["party"], "state": row["state"]},
                "codes": codes,
            }
    return matrix


def merge_house_matrix(matrix: dict[str, dict], results: list[RollCallResult]) -> None:
    """Fold freshly scraped roll calls into the matrix. Re-scraped roll calls
    replace their column entirely, so corrections never double-count. Results
    are applied oldest first, so a member's name/party/state ends up as of
    their most recent vote."""
    for r in sorted(results, key=lambda r: (r.year, r.roll_number)):
        key = roll_key(r.year, r.roll_number)
        for entry in matrix.values():
            entry["codes"].pop(key, None)
        for member_id, (code, party, state, name) in r.house.items():
            entry = matrix.setdefault(member_id, {"meta": {}, "codes": {}})
            entry["meta"] = {"name": name, "party": party, "state": state}
            if code:
                entry["codes"][key] = code


def write_house_matrix(matrix: dict[str, dict], keys: list[str], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(HOUSE_META_COLUMNS + keys)
        for member_id in sorted(matrix):
            entry = matrix[member_id]
            meta = entry["meta"]
            writer.writerow(
                [member_id, meta.get("name", ""), meta.get("party", ""), meta.get("state", "")]
                + [entry["codes"].get(k, "") for k in keys]
            )
    log.info("Wrote House matrix (%d members x %d roll calls) to %s", len(matrix), len(keys), path)


def _int_or_none(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def compute_house_loyalty(matrix: dict[str, dict], vote_rows: list[dict], bills: Optional[dict] = None) -> dict:
    """Party loyalty for every House member, with ranks within party, for all
    votes and for final votes only. Same definitions as the page's
    memberStats(): only yea/nay votes where the party had a majority count
    toward loyalty; blank cells (not in office) count toward nothing."""
    rolls = []
    for row, final in zip(vote_rows, final_flags(vote_rows, bills)):
        year, roll = _int_or_none(row.get("Year")), _int_or_none(row.get("Roll Call Number"))
        if year is None or roll is None:
            continue
        rolls.append((roll_key(year, roll), row, final))

    covered = {k for entry in matrix.values() for k in entry["codes"]}
    missing = [k for k, _, _ in rolls if k not in covered]
    if missing:
        log.warning(
            "House matrix is missing %d of %d roll calls (e.g. %s) -- rankings cover only the "
            "rest. Run a full (non-incremental) scrape of this Congress to fill it in.",
            len(missing), len(rolls), missing[0],
        )

    variants = {}
    for variant, include in (("all", lambda final: True), ("final", lambda final: final)):
        majorities = [
            (key, party_majority(row, "D"), party_majority(row, "R"))
            for key, row, final in rolls
            if include(final)
        ]
        entries = []
        for member_id, entry in matrix.items():
            party = entry["meta"].get("party", "")
            counted = with_party = missed = total = 0
            for key, maj_d, maj_r in majorities:
                code = entry["codes"].get(key)
                if not code:
                    continue  # not in office for this vote
                total += 1
                if code == "V":
                    missed += 1
                majority = maj_d if party == "D" else maj_r if party == "R" else None
                if majority and code in ("Y", "N"):
                    counted += 1
                    if code == majority:
                        with_party += 1
            if not total:
                continue
            entries.append({
                "id": member_id,
                "name": entry["meta"].get("name", ""),
                "party": party,
                "state": entry["meta"].get("state", ""),
                "counted": counted,
                "withParty": with_party,
                "missed": missed,
                "total": total,
                "loyalty": round(with_party / counted, 6) if counted else None,
                "delegate": entry["meta"].get("state", "") in NON_STATE_CODES,
                "rank": None,
                "of": None,
            })

        # Competition ranking ("1, 2, 2, 4") within each party; ties compare
        # exact fractions, not rounded percentages.
        for party in ("D", "R"):
            pool = [
                e for e in entries
                if e["party"] == party
                and e["counted"] >= MIN_RANKED_VOTES
                and e["state"] not in NON_STATE_CODES
            ]
            pool.sort(key=lambda e: e["withParty"] / e["counted"], reverse=True)
            for i, e in enumerate(pool):
                if i and pool[i - 1]["withParty"] * e["counted"] == e["withParty"] * pool[i - 1]["counted"]:
                    e["rank"] = pool[i - 1]["rank"]
                else:
                    e["rank"] = i + 1
                e["of"] = len(pool)
        entries.sort(key=lambda e: (e["party"], e["rank"] or 10**6, e["name"]))
        variants[variant] = entries

    return {
        "minVotes": MIN_RANKED_VOTES,
        "rollCalls": len(rolls),
        "missingRollCalls": len(missing),
        "variants": variants,
    }


HOUSE_MATCHES_NAME = "house_matches.json"
NAMES_CACHE_NAME = "house_names.json"  # lives in data/, shared by all Congresses
LEGISLATORS_URLS = [
    # Public-domain congress-legislators dataset (also the source of the
    # tenure dates in members.json). Current first; historical (~13MB) only
    # if a name is still missing, e.g. when backfilling an older Congress.
    "https://unitedstates.github.io/congress-legislators/legislators-current.json",
    "https://unitedstates.github.io/congress-legislators/legislators-historical.json",
]
# A match must have voted yea/nay on at least 80% of the roll calls the
# Colorado member did. Agreement is measured only on shared votes, so a
# member who skips many votes -- notably the Speaker, who by custom skips
# lopsided ones -- would otherwise look artificially close (98.7% for
# Speaker Johnson in the 119th). 80% removes that without dropping
# ordinary members; 90% starts to.
MATCH_MIN_SHARE = 0.8


def display_name(n: dict) -> str:
    """The name readers know, from a congress-legislators "name" record:
    nickname + last name when there is one ("Tom Cole"); otherwise the
    official name minus initials ("Brian K. Fitzpatrick" -> "Brian
    Fitzpatrick", "J. French Hill" -> "French Hill")."""
    if n.get("nickname") and n.get("last"):
        return f"{n['nickname']} {n['last']}"
    official = n.get("official_full")
    if official:
        words = official.split()
        trimmed = [w for w in words if not re.fullmatch(r"[A-Z]\.", w)]
        return " ".join(trimmed if len(trimmed) >= 2 else words)
    return " ".join(p for p in (n.get("first"), n.get("last")) if p)


def load_full_names(cache_path: Path, needed: set[str]) -> dict[str, str]:
    """{bioguide_id: "First Last"} for the IDs in `needed`, from a cached
    file that's refreshed from congress-legislators only when an ID is
    missing. Never raises: on any download problem it returns what it has,
    and callers fall back to the Clerk's last-name label."""
    names: dict[str, str] = {}
    if cache_path.exists():
        try:
            names = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("Could not read %s (%s); rebuilding it.", cache_path, exc)
            names = {}
    missing = needed - names.keys()
    for url in LEGISLATORS_URLS:
        if not missing:
            break
        try:
            response = requests.get(url, timeout=60)
            response.raise_for_status()
            for person in response.json():
                bioguide = person.get("id", {}).get("bioguide")
                if bioguide in missing:
                    n = person.get("name", {})
                    full = display_name(n)
                    if full:
                        names[bioguide] = full
            missing = needed - names.keys()
        except (requests.RequestException, ValueError) as exc:
            log.warning("Couldn't fetch member names from %s (%s); using Clerk names where needed.", url, exc)
            break
    if missing:
        log.warning("No full name for %d member(s) (e.g. %s); using Clerk names.", len(missing), sorted(missing)[0])
    try:
        cache_path.write_text(json.dumps(names, indent=0, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as exc:
        log.warning("Could not write %s (%s).", cache_path, exc)
    return names


def compute_closest_matches(matrix: dict[str, dict], vote_rows: list[dict], co_ids: list[str],
                            bills: Optional[dict] = None) -> dict:
    """For each Colorado member: the House members who vote most like them,
    and the closest member of the other major party. Agreement = same vote
    on roll calls where both voted yea or nay."""
    rolls = []
    for row, final in zip(vote_rows, final_flags(vote_rows, bills)):
        year, roll = _int_or_none(row.get("Year")), _int_or_none(row.get("Roll Call Number"))
        if year is not None and roll is not None:
            rolls.append((roll_key(year, roll), final))

    variants = {}
    for variant, include in (("all", lambda final: True), ("final", lambda final: final)):
        keys = [k for k, final in rolls if include(final)]
        # Yea/nay votes only, per member, for this variant.
        yn = {
            member_id: {k: entry["codes"][k] for k in keys if entry["codes"].get(k) in ("Y", "N")}
            for member_id, entry in matrix.items()
        }
        out = {}
        for co_id in co_ids:
            mine = yn.get(co_id, {})
            if not mine:
                continue
            my_party = matrix[co_id]["meta"].get("party")
            other_party = {"D": "R", "R": "D"}.get(my_party)
            candidates = []
            for member_id, theirs in yn.items():
                meta = matrix[member_id]["meta"]
                if member_id == co_id or meta.get("state") in NON_STATE_CODES:
                    continue
                shared = same = 0
                for k, v in mine.items():
                    w = theirs.get(k)
                    if w:
                        shared += 1
                        same += v == w
                if shared < MATCH_MIN_SHARE * len(mine):
                    continue  # e.g. the Speaker, or a short-tenure member
                candidates.append({
                    "id": member_id,
                    "name": meta.get("name", ""),
                    "party": meta.get("party", ""),
                    "state": meta.get("state", ""),
                    "same": same,
                    "shared": shared,
                    "agree": round(same / shared, 6),
                })
            candidates.sort(key=lambda c: (c["same"] / c["shared"], c["shared"]), reverse=True)
            across = next((c for c in candidates if c["party"] == other_party), None)
            out[co_id] = {"closest": candidates[:3], "acrossAisle": across}
        variants[variant] = out
    return {"minShare": MATCH_MIN_SHARE, "variants": variants}


def update_house_files(output_path: Path, results: list[RollCallResult], incremental: bool) -> None:
    """Refresh house_votes.csv and house_loyalty.json next to the main CSV."""
    matrix_path = output_path.parent / HOUSE_MATRIX_NAME
    loyalty_path = output_path.parent / HOUSE_LOYALTY_NAME
    matrix = load_house_matrix(matrix_path) if incremental else {}
    merge_house_matrix(matrix, results)

    with output_path.open(newline="", encoding="utf-8") as f:
        vote_rows = list(csv.DictReader(f))
    keys = [roll_key(r["Year"], r["Roll Call Number"]) for r in vote_rows]
    write_house_matrix(matrix, keys, matrix_path)

    bills = load_bills(output_path.parent)
    loyalty = compute_house_loyalty(matrix, vote_rows, bills)
    with loyalty_path.open("w", encoding="utf-8") as f:
        json.dump(loyalty, f, separators=(",", ":"), sort_keys=True)
        f.write("\n")
    ranked = sum(1 for e in loyalty["variants"]["all"] if e["rank"])
    log.info("Wrote party-loyalty stats (%d members, %d ranked) to %s",
             len(loyalty["variants"]["all"]), ranked, loyalty_path)

    # Closest matches for the Colorado delegation of this Congress.
    congress = _int_or_none(output_path.parent.name)
    co_ids = list(load_members(congress)) if congress else []
    if co_ids:
        matches = compute_closest_matches(matrix, vote_rows, co_ids, bills)
        needed = {
            m["id"]
            for v in matches["variants"].values()
            for entry in v.values()
            for m in entry["closest"] + ([entry["acrossAisle"]] if entry["acrossAisle"] else [])
        }
        full = load_full_names(output_path.parent.parent / NAMES_CACHE_NAME, needed)
        for v in matches["variants"].values():
            for entry in v.values():
                for m in entry["closest"] + ([entry["acrossAisle"]] if entry["acrossAisle"] else []):
                    m["fullName"] = full.get(m["id"], "")
        matches_path = output_path.parent / HOUSE_MATCHES_NAME
        with matches_path.open("w", encoding="utf-8") as f:
            json.dump(matches, f, separators=(",", ":"), sort_keys=True)
            f.write("\n")
        log.info("Wrote closest House matches for %d members to %s", len(co_ids), matches_path)


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
        try:
            member_ids = load_members(congress)
        except NoDelegation as exc:
            log.error("%s", exc)
            return EXIT_NO_DELEGATION

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
        if args.incremental:
            # Normal before a new Congress's first vote (or a session's).
            log.info("No roll calls yet for %s; nothing to write.", ", ".join(str(y) for y in args.year))
            emit_github_output("new_count", "0")
            emit_github_output("total_count", "0")
            return 0
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

    # House-wide matrix + loyalty rankings, only for the delegation's own
    # Congress output (skipped for ad hoc --members runs).
    if not args.members:
        update_house_files(args.output, all_results, args.incremental)
        write_vote_info(args.output.parent)

    if args.copy_to and args.copy_to.resolve() != args.output.resolve():
        shutil.copyfile(args.output, args.copy_to)
        log.info("Copied %s -> %s", args.output, args.copy_to)
    return 0


if __name__ == "__main__":
    sys.exit(main())
