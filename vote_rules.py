"""How House roll calls are classified -- the one place these rules live.

vote_scraper.py uses them for the House-wide loyalty and closest-match
figures, and write_vote_info() writes the result for every roll call to
data/<congress>/vote_info.json, which the page reads instead of working
any of this out itself. Change a rule here and the page follows on the
next run (or after re-running write_vote_info() for past Congresses).

For each roll call ("2026-317"):
  final     the vote that decided a bill or resolution
  setAside  "Tabled" / "Sent to committee": a final vote that killed a
            resolution without an up-or-down vote on it
  majority  how most Democrats and most Republicans voted ("Y"/"N")
  groups    the bill(s) the vote belongs under on the page: its own bill,
            the bills a debate rule set up, or HOUSE_BUSINESS
  kind      final / rule / amendment / recommit / other
  topics    Congress.gov policy areas (a rule takes its bills' topics)
  amendment for an amendment vote: who offered it and what it did
            (from amendments.json, written by fetch_legislation.py)
"""

import csv
import json
import re
from pathlib import Path
from typing import Optional

VOTE_INFO_NAME = "vote_info.json"
TOPICS_NAME = "bill_topics.json"
AMENDMENTS_NAME = "amendments.json"
VOTES_NAME = "all_votes.csv"

# Votes on no bill (quorum calls, the Journal, adjournment, Speaker elections).
HOUSE_BUSINESS = "__house"
# A Clerk bill number: "H R 1", "H RES 1655", "S 2403", "H J RES 213".
BILL_NUMBER = re.compile(r"^(H|S)( [A-Z]+)* \d+$")

# Delegates and the resident commissioner (D.C. and territories) can vote on
# some amendments but not on final passage, so they're never ranked or
# compared. The Clerk's XML lists them with state "XX"; the codes cover any
# other spelling.
NON_STATE_CODES = {"XX", "DC", "PR", "GU", "VI", "AS", "MP"}

# The questions that decide a bill or resolution.
FINAL_QUESTION = re.compile(
    r"^(On Passage|Passage, Objections of the President|On Motion to Suspend the Rules and "
    r"(Pass|Agree|Concur)|On Motion to Concur in the Senate Amendments?|On Agreeing to the "
    r"Conference Report|On Agreeing to the Resolution|On Agreeing to Article [IVXLC]+|Article [IVXLC]+)\b",
    re.IGNORECASE,
)
# A debate rule's own resolution is procedural even though "On Agreeing to
# the Resolution" reads like a final vote. The sure sign is that
# bill_topics.json links the rule to the bills it set up ("ruleFor"); this
# wording check is the fallback without it ("Providing for further
# consideration of ...", "Rule providing for ...", "Consideration of ...").
RULE_DESCRIPTION = re.compile(
    r"^(Rule )?(Providing for )?(the )?(further )?consideration|^Providing for (the )?(adoption|disposition)\b",
    re.IGNORECASE,
)
# Privileged resolutions (impeachments, censures, a motion to vacate the
# chair) are often killed by tabling or referring them, with no up-or-down
# vote; that vote decided the resolution, so it counts as final.
DISPOSING_QUESTION = re.compile(r"^On Motion to (Table|Refer)( the Resolution)?$", re.IGNORECASE)
PASSED = ("Passed", "Agreed to")


def roll_key(year, roll) -> str:
    return f"{int(year)}-{int(roll)}"


def _int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def is_rule(row: dict, bills: Optional[dict] = None) -> bool:
    """Whether the vote's bill is a debate rule."""
    entry = (bills or {}).get(row.get("Bill Number") or "") or {}
    return bool(entry.get("ruleFor")) or bool(RULE_DESCRIPTION.match(row.get("Description") or ""))


def is_final_vote(row: dict, bills: Optional[dict] = None) -> bool:
    """A vote that decides a bill or resolution on its own terms (passage,
    adoption, an article of impeachment), not a debate rule's adoption."""
    question = row.get("Vote Question") or ""
    if not FINAL_QUESTION.match(question):
        return False
    if re.match(r"^On Agreeing to the Resolution", question, re.IGNORECASE) and is_rule(row, bills):
        return False
    return True


def disposing_vote(row: dict) -> bool:
    """A successful motion to table or refer a House resolution."""
    return (
        (row.get("Bill Number") or "").startswith("H RES")
        and bool(DISPOSING_QUESTION.match((row.get("Vote Question") or "").strip()))
        and row.get("Result") in PASSED
    )


def final_flags(rows: list[dict], bills: Optional[dict] = None) -> list[bool]:
    """is_final_vote() for each row, plus a successful motion to table or
    refer a House resolution that never got a final vote of its own.
    bills: bill_topics.json's "bills", to recognize debate rules."""
    flags = [is_final_vote(r, bills) for r in rows]
    decided = {r.get("Bill Number") for r, f in zip(rows, flags) if f}
    for i, r in enumerate(rows):
        if not flags[i] and r.get("Bill Number") not in decided and disposing_vote(r):
            flags[i] = True
    return flags


def set_aside(row: dict, final: bool, bills: Optional[dict] = None) -> Optional[str]:
    """What happened to a resolution decided by tabling or referral; None
    for any other vote. "Passed" would read as the resolution passing."""
    if not final or is_final_vote(row, bills) or not disposing_vote(row):
        return None
    return "Tabled" if re.search(r"Table", row.get("Vote Question") or "", re.IGNORECASE) else "Sent to committee"


def party_majority(row: dict, party: str) -> Optional[str]:
    """'Y' or 'N' for how most of a party voted; None on a tie, for other
    parties, or without party totals (e.g. a quorum call)."""
    prefix = {"D": "Dem", "R": "Rep"}.get(party)
    if not prefix:
        return None
    yea, nay = _int(row.get(f"{prefix} Yea")), _int(row.get(f"{prefix} Nay"))
    if yea is None or nay is None or yea == nay:
        return None
    return "Y" if yea > nay else "N"


def group_keys(row: dict, bills: dict) -> list[str]:
    """The bill(s) a vote belongs under: a debate rule's votes go under each
    bill the rule set up; votes on no bill go under HOUSE_BUSINESS."""
    bill = row.get("Bill Number") or ""
    rule_for = (bills.get(bill) or {}).get("ruleFor") or []
    if rule_for:
        return list(rule_for)
    return [bill] if BILL_NUMBER.match(bill) else [HOUSE_BUSINESS]


def vote_kind(row: dict, final: bool, bills: dict) -> str:
    bill = row.get("Bill Number") or ""
    if (bills.get(bill) or {}).get("ruleFor"):
        return "rule"
    if not BILL_NUMBER.match(bill):
        return "other"
    if final:
        return "final"
    question = row.get("Vote Question") or ""
    if re.search(r"amendment", question, re.IGNORECASE):
        return "amendment"
    if re.search(r"recommit", question, re.IGNORECASE):
        return "recommit"
    return "other"


def topics(row: dict, bills: dict) -> list[str]:
    """Congress.gov policy areas. Debate rules are all filed under
    "Congress", so a rule takes the topics of the bills it set up."""
    entry = bills.get(row.get("Bill Number") or "") or {}
    out: list[str] = []
    for b in entry.get("ruleFor") or []:
        t = (bills.get(b) or {}).get("policyArea")
        if t and t not in out:
            out.append(t)
    if not out and entry.get("policyArea"):
        out.append(entry["policyArea"])
    return out


def classify(rows: list[dict], bills: Optional[dict] = None, amendments: Optional[dict] = None) -> dict[str, dict]:
    """{roll_key: info} for every roll call, in the rows' order. Falsy
    fields are left out to keep the file small. amendments: amendments.json's
    "votes", keyed by roll call."""
    bills, amendments = bills or {}, amendments or {}
    out: dict[str, dict] = {}
    for row, final in zip(rows, final_flags(rows, bills)):
        info: dict = {"kind": vote_kind(row, final, bills), "groups": group_keys(row, bills)}
        if final:
            info["final"] = True
        aside = set_aside(row, final, bills)
        if aside:
            info["setAside"] = aside
        majority = {p: m for p in ("D", "R") if (m := party_majority(row, p))}
        if majority:
            info["majority"] = majority
        t = topics(row, bills)
        if t:
            info["topics"] = t
        key = roll_key(row["Year"], row["Roll Call Number"])
        if info["kind"] == "amendment" and key in amendments:
            info["amendment"] = amendments[key]
        out[key] = info
    return out


def load_bills(congress_dir: Path) -> dict:
    """bill_topics.json's "bills" for a Congress, or {} if it isn't there yet."""
    path = Path(congress_dir) / TOPICS_NAME
    return json.loads(path.read_text(encoding="utf-8")).get("bills", {}) if path.exists() else {}


def load_amendments(congress_dir: Path) -> dict:
    """amendments.json's "votes" for a Congress, or {} if it isn't there yet."""
    path = Path(congress_dir) / AMENDMENTS_NAME
    return json.loads(path.read_text(encoding="utf-8")).get("votes", {}) if path.exists() else {}


def write_vote_info(congress_dir: Path) -> Optional[Path]:
    """Write vote_info.json for one Congress from its all_votes.csv and
    (if present) bill_topics.json. Returns the path, or None without votes."""
    congress_dir = Path(congress_dir)
    votes_path = congress_dir / VOTES_NAME
    if not votes_path.exists():
        return None
    with votes_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    info = classify(rows, load_bills(congress_dir), load_amendments(congress_dir))
    header = {
        "_comment": "How each roll call is classified, from vote_rules.py (see its docstring for the "
                    "fields). Written by vote_scraper.py and fetch_legislation.py; the page reads it "
                    "instead of applying the rules itself. HOUSE_BUSINESS groups are \"" + HOUSE_BUSINESS + "\".",
        "congress": int(congress_dir.name) if congress_dir.name.isdigit() else None,
        "houseBusiness": HOUSE_BUSINESS,
    }
    # One roll call per line keeps nightly diffs small.
    lines = [json.dumps(header, ensure_ascii=False)[:-1] + ', "rollCalls": {']
    items = [f"{json.dumps(k)}: {json.dumps(v, ensure_ascii=False, separators=(',', ':'))}" for k, v in info.items()]
    lines.append(",\n".join(items))
    lines.append("}}")
    path = congress_dir / VOTE_INFO_NAME
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


if __name__ == "__main__":
    # Rebuild vote_info.json for every Congress (or those named), e.g. after
    # changing a rule: python vote_rules.py [119 118 ...]
    import sys

    data = Path(__file__).resolve().parent / "data"
    wanted = sys.argv[1:] or sorted(p.name for p in data.iterdir() if p.is_dir() and p.name.isdigit())
    for c in wanted:
        written = write_vote_info(data / c)
        print(f"{c}: {'wrote ' + str(written.relative_to(data.parent)) if written else 'no votes'}")
