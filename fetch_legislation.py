#!/usr/bin/env python3
"""Fetch bills sponsored and cosponsored by Colorado's House members.

Reads the delegation from members.json and writes, for each Congress requested:
  data/<congress>/legislation.json   each member's sponsored/cosponsored bills
  data/<congress>/bill_topics.json   the topic (policy area) of every bill voted
                                     on in all_votes.csv, cached between runs
using the Congress.gov API
(https://api.congress.gov, free key from https://api.congress.gov/sign-up/).

The API key comes from the CONGRESS_API_KEY environment variable (the GitHub
Actions secret), or else from a local .env / congress.env file in this folder
containing the line CONGRESS_API_KEY=...  Those files are git-ignored. The key
is sent only in a request header and is never printed or written anywhere.

Usage:
    python fetch_legislation.py                  # the "current" Congress
    python fetch_legislation.py --congress 118 119
"""

import argparse
import collections
import datetime as dt
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from vote_rules import write_vote_info

ROOT = Path(__file__).resolve().parent
API = "https://api.congress.gov/v3"
PAGE = 250  # the API's maximum page size

# Bill types kept. Amendments have no "type" and are skipped; House members
# can't sponsor Senate bills, and cosponsorship lists don't include them.
BILL_TYPES = {"HR", "HRES", "HJRES", "HCONRES"}

# Path segment congress.gov uses for each bill type, for reader-facing links.
WEB_TYPE = {
    "HR": "house-bill",
    "HRES": "house-resolution",
    "HJRES": "house-joint-resolution",
    "HCONRES": "house-concurrent-resolution",
}

LAW_RE = re.compile(r"Became (Public|Private) Law No:\s*([\d-]+)", re.I)


def load_key():
    key = os.environ.get("CONGRESS_API_KEY", "").strip()
    if key:
        return key
    for name in (".env", "congress.env"):
        path = ROOT / name
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if line.startswith("CONGRESS_API_KEY="):
                key = line.split("=", 1)[1].strip().strip("'\"")
                if key:
                    return key
    sys.exit("No Congress.gov API key: set CONGRESS_API_KEY, or put "
             "CONGRESS_API_KEY=... in .env or congress.env in this folder.")


class Client:
    def __init__(self, key):
        self.key = key
        self.local = threading.local()   # one Session per thread
        self.lock = threading.Lock()
        self.calls = 0

    @property
    def session(self):
        s = getattr(self.local, "session", None)
        if s is None:
            s = self.local.session = requests.Session()
            s.headers.update({"X-Api-Key": self.key, "Accept": "application/json"})
        return s

    def get(self, path, **params):
        params.setdefault("format", "json")
        rate_waits = 0
        attempt = 0
        while attempt < 5:
            try:
                r = self.session.get(API + path, params=params, timeout=60)
            except requests.RequestException as e:
                # Report the error type only: messages can include the URL.
                err = type(e).__name__
            else:
                with self.lock:
                    self.calls += 1
                if r.ok:
                    return r.json()
                if r.status_code == 429 and rate_waits < 12:
                    # Hourly limit (5,000 calls): wait it out rather than give
                    # up, so a long backfill can run unattended. Up to ~1 hour.
                    rate_waits += 1
                    wait = int(r.headers.get("Retry-After") or 0) or 300
                    print(f"  rate limit reached; waiting {wait}s ({rate_waits}/12)", file=sys.stderr, flush=True)
                    time.sleep(wait)
                    continue
                if r.status_code not in (500, 502, 503, 504):
                    raise SystemExit(f"Congress.gov returned HTTP {r.status_code} for {path}")
                err = f"HTTP {r.status_code}"
            wait = 2 ** attempt * 5
            attempt += 1
            print(f"  {err} on {path}; retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
        raise SystemExit(f"Gave up on {path} after repeated errors.")

    def member_items(self, bioguide, kind):
        """All of a member's sponsored- or cosponsored-legislation items.

        Every page is fetched: the API does not list items in Congress order
        (Mark Udall's first page mixes the 109th-113th), so stopping at the
        first older item dropped whole Congresses for long-serving members.
        A short read fails loudly rather than silently undercounting.
        """
        items, offset, total = [], 0, 0
        while True:
            data = self.get(f"/member/{bioguide}/{kind}", limit=PAGE, offset=offset)
            key = next(k for k in data if k.endswith("Legislation"))
            page = data[key] or []
            items.extend(page)
            offset += PAGE
            total = (data.get("pagination") or {}).get("count", 0)
            if not page or offset >= total:
                break
        if len(items) < total:
            raise SystemExit(f"Got {len(items)} of {total} {kind} items for {bioguide}; try again.")
        return items


def bill_record(item):
    action = item.get("latestAction") or {}
    text = (action.get("text") or "").strip()
    law = LAW_RE.search(text)
    btype = item["type"].upper()
    congress = int(item["congress"])
    return {
        "type": btype,
        "number": str(item["number"]),
        "title": (item.get("title") or "").strip(),
        "introduced": item.get("introducedDate") or "",
        "policyArea": ((item.get("policyArea") or {}).get("name")) or "",
        "latestAction": text,
        "latestActionDate": action.get("actionDate") or "",
        "law": law.group(2) if law else None,
        "url": f"https://www.congress.gov/bill/{congress}{ordinal_suffix(congress)}-congress/"
               f"{WEB_TYPE[btype]}/{item['number']}",
    }


def ordinal_suffix(n):
    if 10 <= n % 100 <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def split_by_congress(items):
    out = collections.defaultdict(list)
    for it in items:
        if (it.get("type") or "").upper() in BILL_TYPES and it.get("congress") and it.get("number"):
            out[int(it["congress"])].append(bill_record(it))
    for bills in out.values():
        bills.sort(key=lambda b: (b["introduced"], int(re.sub(r"\D", "", b["number"]) or 0)), reverse=True)
    return out


CSV_BILL_RE = re.compile(r"^(H R|H RES|H J RES|H CON RES|S|S RES|S J RES|S CON RES) (\d+)$")
RULE_RE = re.compile(r"\((H\. ?R\.|H\. ?Res\.|H\. ?J\. ?Res\.|H\. ?Con\. ?Res\.|S\.|S\. ?J\. ?Res\.|S\. ?Con\. ?Res\.) ?(\d+)\)")
RECHECK_DAYS = 7  # bills without a topic yet are re-checked this often
WORKERS = 8       # parallel bill lookups (well under the API's 5,000 requests/hour)


def csv_bill_numbers(congress):
    """Distinct bill numbers ("H R 2670") voted on in data/<c>/all_votes.csv."""
    import csv
    path = ROOT / "data" / congress / "all_votes.csv"
    if not path.is_file():
        return []
    with path.open(newline="") as f:
        nums = {(r.get("Bill Number") or "").strip() for r in csv.DictReader(f)}
    return sorted(n for n in nums if CSV_BILL_RE.match(n))


def rule_targets(title):
    """Bills a special rule ("Providing for consideration of the bill (H.R. 1234) ...")
    sets up, as CSV-style numbers, so rule votes can take those bills' topics."""
    if not title.lower().startswith("providing for"):
        return []
    out = []
    for kind, n in RULE_RE.findall(title):
        k = re.sub(r"[^A-Za-z]", "", kind).upper()          # HR, HRES, HJRES, S, ...
        csv_kind = {"HR": "H R", "HRES": "H RES", "HJRES": "H J RES", "HCONRES": "H CON RES",
                    "S": "S", "SJRES": "S J RES", "SCONRES": "S CON RES"}[k]
        num = f"{csv_kind} {n}"
        if num not in out:
            out.append(num)
    return out


def update_bill_topics(client, congress):
    """Cache each voted-on bill's Congress.gov policy area in data/<c>/bill_topics.json.

    Only bills not yet in the cache (or still without a topic after a week)
    are fetched, so the nightly run makes a handful of calls."""
    path = ROOT / "data" / congress / "bill_topics.json"
    cache = json.loads(path.read_text())["bills"] if path.is_file() else {}
    today = dt.date.today()
    wanted = set(csv_bill_numbers(congress))

    def needs_lookup(num):
        old = cache.get(num)
        if not old:
            return True
        return not old.get("policyArea") and \
            (today - dt.date.fromisoformat(old.get("checked", "2000-01-01"))).days >= RECHECK_DAYS

    def lookup(num):
        kind, n = CSV_BILL_RE.match(num).groups()
        bill = client.get(f"/bill/{congress}/{kind.replace(' ', '').lower()}/{n}").get("bill") or {}
        title = (bill.get("title") or "").strip()
        entry = {"policyArea": ((bill.get("policyArea") or {}).get("name")) or "",
                 "title": title, "checked": today.isoformat()}
        targets = rule_targets(title)
        if targets:
            entry["ruleFor"] = targets
        return num, entry

    # Rounds: look up what's missing (a few at a time; uncached bill lookups
    # take ~2s each), then add any bills the new rules point to, and repeat.
    fetched = 0
    todo = sorted(n for n in wanted if needs_lookup(n))
    while todo:
        print(f"  {congress}: looking up {len(todo)} bills...", flush=True)
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for i, (num, entry) in enumerate(pool.map(lookup, todo), 1):
                cache[num] = entry
                fetched += 1
                if i % 100 == 0:
                    print(f"    {i} of {len(todo)}", flush=True)
                    write_topics(path, congress, today, cache)
        targets = {t for e in cache.values() for t in e.get("ruleFor", [])}
        todo = sorted(t for t in targets if t not in cache)
    write_topics(path, congress, today, cache)
    with_topic = sum(1 for n in wanted if cache.get(n, {}).get("policyArea") or
                     any(cache.get(t, {}).get("policyArea") for t in cache.get(n, {}).get("ruleFor", [])))
    print(f"Wrote {path.relative_to(ROOT)}: {len(wanted)} voted-on bills, {with_topic} with a topic, "
          f"{fetched} looked up this run")


def write_topics(path, congress, today, cache):
    out = {
        "_comment": "Congress.gov policy area (topic) for each bill voted on in this Congress, keyed by "
                    "the Clerk's bill number as in all_votes.csv. Special rules (resolutions 'providing "
                    "for consideration of' other bills) are all filed under 'Congress'; 'ruleFor' "
                    "lists the bills they set up, so the page shows those bills' topics instead. Written by "
                    "fetch_legislation.py.",
        "congress": int(congress),
        "updated": today.isoformat(),
        "bills": dict(sorted(cache.items())),
    }
    path.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- progress
# How far each bill a member sponsored or co-led got, from its action
# history. Congress.gov tags the key steps with these action codes.
STAGE_CODES = {"5000": "reported", "8000": "house", "17000": "senate",
               "28000": "president", "31000": "vetoed", "36000": "law"}
# "Co-led": the member was one of this many or fewer original cosponsors
# (on the bill the day it was introduced), so likely helped write it.
CO_LEAD_MAX = 5
ORIGINALS_RECHECK_DAYS = 14   # cosponsor lists can lag a bill's introduction
CSV_PREFIX = {"HR": "H R", "HRES": "H RES", "HJRES": "H J RES", "HCONRES": "H CON RES"}


def bill_key(b):
    return f"{CSV_PREFIX[b['type']]} {b['number']}"   # as in all_votes.csv


def api_bill_path(congress, b):
    return f"/bill/{congress}/{b['type'].lower()}/{b['number']}"


def run_pool(label, todo, fn, store, save):
    if not todo:
        return
    print(f"  looking up {label} for {len(todo)} bills...", flush=True)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for i, (k, v) in enumerate(pool.map(fn, todo), 1):
            store[k] = v
            if i % 200 == 0:
                print(f"    {i} of {len(todo)}", flush=True)
                save()
    save()


def update_progress(client, congress, members, fetched):
    """Original cosponsors of every cosponsored bill, plus the action-history
    stages and (for co-led bills) the sponsor, cached in data/<c>/bill_progress.json.

    Original cosponsors don't change once recorded, so each bill is looked up
    once; stages are re-fetched only when a bill's latest action changes."""
    path = ROOT / "data" / congress / "bill_progress.json"
    cache = json.loads(path.read_text()) if path.is_file() else {}
    originals = cache.get("originals", {})
    stages = cache.get("stages", {})
    sponsors = cache.get("sponsors", {})
    today = dt.date.today()
    cn = int(congress)

    def save():
        path.write_text(json.dumps({
            "_comment": "Cache for fetch_legislation.py, keyed by the Clerk's bill number. 'originals': "
                        "Bioguide IDs of each bill's original cosponsors. 'stages': first date of each "
                        "key action (reported by committee, passed House, passed Senate, presented to "
                        "the President, vetoed, became law). 'sponsors': sponsor of each co-led bill.",
            "congress": cn, "updated": today.isoformat(),
            "originals": dict(sorted(originals.items())),
            "stages": dict(sorted(stages.items())),
            "sponsors": dict(sorted(sponsors.items())),
        }, indent=1, ensure_ascii=False) + "\n")

    # 1. Original cosponsors of every bill a member cosponsored.
    cosp = {}
    for m in members:
        for b in fetched[m["id"]]["cosponsored"].get(cn, []):
            cosp[bill_key(b)] = b

    def needs_originals(k):
        o = originals.get(k)
        if not o:
            return True
        intro = cosp[k]["introduced"]
        recent = intro and (today - dt.date.fromisoformat(intro)).days < ORIGINALS_RECHECK_DAYS
        return bool(recent and o.get("checked") != today.isoformat())

    def get_originals(k):
        data = client.get(api_bill_path(congress, cosp[k]) + "/cosponsors", limit=250)
        people = data.get("cosponsors") or []
        return k, {"originals": [p["bioguideId"] for p in people if p.get("isOriginalCosponsor")],
                   "total": (data.get("pagination") or {}).get("count", len(people)),
                   "checked": today.isoformat()}

    run_pool("original cosponsors", sorted(k for k in cosp if needs_originals(k)), get_originals, originals, save)

    coled = {}
    for m in members:
        coled[m["id"]] = [b for b in fetched[m["id"]]["cosponsored"].get(cn, [])
                          if m["id"] in originals.get(bill_key(b), {}).get("originals", [])
                          and len(originals[bill_key(b)]["originals"]) <= CO_LEAD_MAX]

    # 2. Stages for every bill a member sponsored or co-led.
    tracked = {}
    for m in members:
        for b in fetched[m["id"]]["sponsored"].get(cn, []) + coled[m["id"]]:
            tracked[bill_key(b)] = b

    def get_stages(k):
        b, found, offset = tracked[k], {}, 0
        while True:
            data = client.get(api_bill_path(congress, b) + "/actions", limit=250, offset=offset)
            acts = data.get("actions") or []
            for a in acts:
                stage = STAGE_CODES.get(str(a.get("actionCode") or ""))
                if not stage:
                    continue
                d = a.get("actionDate") or ""
                if stage not in found or d < found[stage]:
                    found[stage] = d
                law = LAW_RE.search(a.get("text") or "")
                if law:
                    found["lawNumber"] = law.group(2)
            offset += 250
            if not acts or offset >= (data.get("pagination") or {}).get("count", 0):
                break
        found["latestActionDate"] = b["latestActionDate"]
        return k, found

    run_pool("action histories", sorted(k for k in tracked
                                        if stages.get(k, {}).get("latestActionDate") != tracked[k]["latestActionDate"]),
             get_stages, stages, save)

    # 3. Who sponsored each co-led bill.
    def get_sponsor(k):
        bill = client.get(api_bill_path(congress, tracked[k])).get("bill") or {}
        s = (bill.get("sponsors") or [{}])[0]
        return k, {"id": s.get("bioguideId", ""), "name": f"{s.get('firstName', '')} {s.get('lastName', '')}".strip(),
                   "party": s.get("party", ""), "state": s.get("state", "")}

    coled_keys = {bill_key(b) for bills in coled.values() for b in bills}
    run_pool("sponsors", sorted(k for k in coled_keys if k not in sponsors), get_sponsor, sponsors, save)
    return coled, originals, stages, sponsors


def with_progress(b, stages, extra=None):
    """A copy of bill record b with its stages (and law number) attached."""
    out = dict(b)
    s = stages.get(bill_key(b), {})
    out["stages"] = {k: s[k] for k in ("reported", "house", "senate", "president", "vetoed", "law") if s.get(k)}
    if s.get("lawNumber"):
        out["law"] = s["lawNumber"]
    if extra:
        out.update(extra)
    return out


def topics(bills, limit=5):
    c = collections.Counter(b["policyArea"] for b in bills if b["policyArea"])
    return [{"name": n, "count": k} for n, k in c.most_common(limit)]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--congress", nargs="+", help="Congress number(s); default: members.json 'current'")
    args = ap.parse_args()

    config = json.loads((ROOT / "members.json").read_text())
    congresses = args.congress or [str(config["current"])]
    for c in congresses:
        if not (config["congresses"].get(c) or {}).get("members"):
            sys.exit(f"members.json has no members for the {c}th Congress.")

    client = Client(load_key())

    # Each member's lists span their whole career, so fetch each member once
    # and split the results by Congress.
    wanted = {}
    for c in congresses:
        for m in config["congresses"][c]["members"]:
            wanted[m["id"]] = m
    fetched = {}
    for mid, m in wanted.items():
        print(f"{m['fullName']}: fetching sponsored and cosponsored bills...", flush=True)
        fetched[mid] = {
            "sponsored": split_by_congress(client.member_items(mid, "sponsored-legislation")),
            "cosponsored": split_by_congress(client.member_items(mid, "cosponsored-legislation")),
        }

    today = dt.date.today().isoformat()
    for c in congresses:
        cn = int(c)
        print(f"{c}th Congress:", flush=True)
        delegation = config["congresses"][c]["members"]
        coled, originals, stages, sponsors = update_progress(client, c, delegation, fetched)
        members = {}
        for m in delegation:
            spon = [with_progress(b, stages) for b in fetched[m["id"]]["sponsored"].get(cn, [])]
            cosp = fetched[m["id"]]["cosponsored"].get(cn, [])
            led = [with_progress(b, stages, {
                       "sponsor": sponsors.get(bill_key(b), {}),
                       "originalCosponsors": len(originals[bill_key(b)]["originals"]),
                   }) for b in coled[m["id"]]]
            members[m["id"]] = {
                "sponsored": {
                    "count": len(spon),
                    "becameLaw": sum(1 for b in spon if b["law"]),
                    "topics": topics(spon),
                    "bills": spon,
                },
                "coled": {
                    "count": len(led),
                    "becameLaw": sum(1 for b in led if b["law"]),
                    "bills": led,
                },
                "cosponsored": {
                    "count": len(cosp),
                    "becameLaw": sum(1 for b in cosp if b["law"]),
                    "topics": topics(cosp),
                },
            }
            passed = sum(1 for b in spon + led if b["stages"].get("house"))
            print(f"  {m['displayName']}: {len(spon)} sponsored, {len(led)} co-led; {passed} passed House, "
                  f"{sum(1 for b in spon + led if b['law'])} law; {len(cosp)} cosponsored in all")
        out = {
            "_comment": "Bills and resolutions each Colorado House member sponsored or co-led in this "
                        "Congress, from the Congress.gov API (fetch_legislation.py). Amendments are not "
                        "included. 'Co-led' means the member was one of " + str(CO_LEAD_MAX) + " or fewer "
                        "original cosponsors. 'stages' gives the first date of each key action (reported by "
                        "committee, passed House, passed Senate, presented to the President, vetoed, became "
                        "law); 'law' is the public law number. Bills whose text was folded into a different "
                        "law are not counted as becoming law. 'cosponsored' counts every bill the member "
                        "cosponsored, co-led or not.",
            "congress": cn,
            "updated": today,
            "coLeadMax": CO_LEAD_MAX,
            "members": members,
        }
        path = ROOT / "data" / c / "legislation.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
        print(f"Wrote {path.relative_to(ROOT)}")
        update_bill_topics(client, c)
        # Topics tell debate rules apart and group votes by bill, so the
        # page's vote classifications are rebuilt with them.
        write_vote_info(ROOT / "data" / c)
    print(f"{client.calls} API calls.")


if __name__ == "__main__":
    main()
