#!/usr/bin/env python3
"""Backfill earlier Congresses, unattended.

For each Congress (default: the 117th back to the 108th, 2003; the Clerk's
roll-call files only carry member IDs from 2003 on):

  1. Adds the Colorado delegation to members.json if it isn't there,
     built from the public-domain congress-legislators dataset (IDs,
     districts, parties, first-term dates; a note for anyone who served
     only part of the Congress). Existing entries are never changed.
  2. Scrapes the Congress's roll calls (vote_scraper.py), unless
     data/<congress>/all_votes.csv already exists.
  3. Fetches sponsored/co-led bills and topics (fetch_legislation.py).
     Cached and resumable; waits out the API's hourly limit.

Every step is skipped or resumed if already done, so it's safe to re-run
after an interruption. A failure in one Congress is logged and the next
one starts. A check of each Congress is printed at the end (and written to
backfill.log): roll calls, and any member with an empty vote column (which
would mean a wrong ID).

Usage:
    python backfill.py                    # 117 down to 108
    python backfill.py 115 114            # just these
    python backfill.py --dry-run 110      # show the members.json entry only
"""

import argparse
import csv
import datetime as dt
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
PY = sys.executable
LOG = ROOT / "backfill.log"
LEGISLATORS = [
    "https://unitedstates.github.io/congress-legislators/legislators-current.json",
    "https://unitedstates.github.io/congress-legislators/legislators-historical.json",
]
MONTHS = ["Jan.", "Feb.", "March", "April", "May", "June", "July", "Aug.", "Sept.", "Oct.", "Nov.", "Dec."]
PARTY = {"Democrat": "D", "Republican": "R", "Independent": "I"}


def log(msg):
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M} {msg}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def ap_date(d):
    return f"{MONTHS[d.month - 1]} {d.day}, {d.year}"


def congress_span(c):
    start = 1787 + 2 * c          # 119 -> 2025
    return dt.date(start, 1, 3), dt.date(start + 2, 1, 3), [start, start + 1]


_people = None


def legislators():
    global _people
    if _people is None:
        _people = []
        for url in LEGISLATORS:
            _people += requests.get(url, timeout=180).json()
    return _people


def clean_name(p):
    n = p["name"]
    full = n.get("official_full") or f"{n.get('nickname') or n['first']} {n['last']}"
    full = re.sub(r"\b[A-Z]\.\s+", "", full)          # drop middle initials
    return re.sub(r"\s+", " ", full).strip()


def delegation(c):
    """Colorado House members who served in Congress c, as members.json entries."""
    cstart, cend, years = congress_span(c)
    out = []
    for p in legislators():
        terms = [t for t in p.get("terms", []) if t["type"] == "rep" and t["state"] == "CO"
                 and dt.date.fromisoformat(t["start"]) < cend and dt.date.fromisoformat(t["end"]) > cstart]
        if not terms:
            continue
        t0, t1 = terms[0], terms[-1]
        tstart, tend = dt.date.fromisoformat(t0["start"]), dt.date.fromisoformat(t1["end"])
        first = min(t["start"] for t in p["terms"])
        last = p["name"]["last"]
        m = {"id": p["id"]["bioguide"], "csvName": last, "displayName": last, "fullName": clean_name(p),
             "inCongressSince": first, "party": PARTY.get(t1.get("party"), (t1.get("party") or "?")[0]),
             "district": t1.get("district")}
        parts = []
        if (tstart - cstart).days > 30:
            parts.append(f"Took office {ap_date(tstart)}.")
        if (cend - tend).days > 30:
            parts.append(f"Left office {ap_date(tend)}.")
        if parts:
            m["note"] = " ".join(parts) + " Stats cover only votes while in office."
        out.append(m)
    # Same last name twice in one delegation: add a first initial.
    for m in out:
        if sum(1 for o in out if o["csvName"] == m["csvName"]) > 1:
            m["csvName"] = m["displayName"] = f"{m['fullName'].split()[0][0]}. {m['csvName']}"
    out.sort(key=lambda m: ({"D": 0, "R": 1}.get(m["party"], 2), m["district"] or 0))
    return {"years": years, "members": out}


def add_to_members_json(c, entry):
    """Insert the entry as text so the hand-formatted file keeps its layout."""
    path = ROOT / "members.json"
    text = path.read_text()
    members = ",\n".join("        " + json.dumps(m, ensure_ascii=False) for m in entry["members"])
    block = (f',\n    "{c}": {{\n      "years": {json.dumps(entry["years"])},\n'
             f'      "members": [\n{members}\n      ]\n    }}')
    i = text.rstrip().rfind("}")          # end of file
    j = text.rstrip()[:i].rstrip().rfind("}")  # end of "congresses"
    new = text[:j].rstrip() + block + "\n  " + text[j:]
    json.loads(new)                       # refuse to write invalid JSON
    path.write_text(new)


def run(cmd):
    log("  $ " + " ".join(str(x) for x in cmd))
    with LOG.open("a") as f:
        return subprocess.run(cmd, cwd=ROOT, stdout=f, stderr=subprocess.STDOUT).returncode


def check(c):
    """One-line health check for a Congress's data."""
    path = ROOT / "data" / str(c) / "all_votes.csv"
    if not path.is_file():
        return f"{c}: NO VOTE DATA"
    rows = list(csv.DictReader(path.open()))
    cfg = json.loads((ROOT / "members.json").read_text())["congresses"][str(c)]
    empty = [m["displayName"] for m in cfg["members"]
             if not any((r.get(f"{m['csvName']} ({m['id']})") or "").strip() for r in rows)]
    leg = (ROOT / "data" / str(c) / "legislation.json").is_file()
    return (f"{c}: {len(rows)} roll calls, {len(cfg['members'])} members"
            + (f", EMPTY COLUMNS: {', '.join(empty)}" if empty else ", all members have votes")
            + ("" if leg else ", NO BILL DATA"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("congress", nargs="*", type=int, default=list(range(117, 107, -1)))
    ap.add_argument("--dry-run", action="store_true", help="print the members.json entries and stop")
    args = ap.parse_args()

    if args.dry_run:
        for c in args.congress:
            print(c, json.dumps(delegation(c), indent=1, ensure_ascii=False))
        return

    t0 = time.time()
    log(f"Backfill starting: {args.congress}")
    for c in args.congress:
        if c < 108:
            log(f"{c}: skipped (the Clerk's files before 2003 don't identify members by ID)")
            continue
        try:
            cfg = json.loads((ROOT / "members.json").read_text())
            if str(c) not in cfg["congresses"]:
                entry = delegation(c)
                add_to_members_json(c, entry)
                log(f"{c}: added {len(entry['members'])} members to members.json: "
                    + ", ".join(f"{m['fullName']} ({m['party']}-{m['district']})" for m in entry["members"]))
            _, _, years = congress_span(c)
            if not (ROOT / "data" / str(c) / "all_votes.csv").is_file():
                log(f"{c}: scraping roll calls for {years[0]}-{years[1]}")
                if run([PY, "-u", "vote_scraper.py", "--year", str(years[0]), "--year", str(years[1])]):
                    log(f"{c}: SCRAPE FAILED (see above); moving on")
                    continue
            log(f"{c}: fetching bills")
            if run([PY, "-u", "fetch_legislation.py", "--congress", str(c)]):
                log(f"{c}: BILL FETCH FAILED (see above); re-run later to resume")
            log("  " + check(c))
        except Exception as e:  # keep going with the next Congress
            log(f"{c}: ERROR {type(e).__name__}: {e}")
    log(f"Backfill finished in {(time.time() - t0) / 3600:.1f} hours. Summary:")
    for c in args.congress:
        if c >= 108:
            log("  " + check(c))


if __name__ == "__main__":
    main()
