"""Checks on the real data in data/ and members.json.

These catch the problems that have actually happened: roll calls missing
from a year (2007's 1000-1186), a member with a wrong ID (an empty vote
column), bill lists cut short (0 cosponsored bills), and vote_info.json
falling out of step with the votes. The nightly workflow runs them before
committing, so bad data doesn't publish.
"""

import csv
import datetime as dt
import json
from pathlib import Path

import pytest

import vote_rules as vr

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CONFIG = json.loads((ROOT / "members.json").read_text(encoding="utf-8"))
CONGRESSES = sorted((c for c in CONFIG["congresses"] if (DATA / c / vr.VOTES_NAME).exists()), key=int)


def votes(c):
    with (DATA / c / vr.VOTES_NAME).open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def finished(c):
    """A Congress whose two years are over (its bill counts are settled)."""
    return max(CONFIG["congresses"][c]["years"]) < dt.date.today().year


def test_there_is_data():
    assert CONGRESSES, "no Congress has an all_votes.csv"


@pytest.mark.parametrize("c", CONGRESSES)
def test_members_json_entry(c):
    members = CONFIG["congresses"][c]["members"]
    ids = [m["id"] for m in members]
    names = [m["csvName"] for m in members]
    assert len(ids) == len(set(ids)), "duplicate member IDs"
    assert len(names) == len(set(names)), "two members share a csvName"
    for m in members:
        assert m["party"] in ("D", "R", "I"), m
        assert isinstance(m.get("district"), int), m
        assert len(m["id"]) == 7 and m["id"][0].isalpha() and m["id"][1:].isdigit(), m


@pytest.mark.parametrize("c", CONGRESSES)
def test_roll_calls_have_no_gaps(c):
    by_year = {}
    for r in votes(c):
        by_year.setdefault(int(r["Year"]), []).append(int(r["Roll Call Number"]))
    for year, rolls in by_year.items():
        assert sorted(rolls) == list(range(1, len(rolls) + 1)), \
            f"{year}: roll calls missing or repeated (have {len(rolls)}, highest {max(rolls)})"


MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def clerk_date(s):
    day, month, year = s.split("-")
    return dt.date(int(year), MONTHS[month.lower()], int(day))


@pytest.mark.parametrize("c", CONGRESSES)
def test_finished_years_reach_the_end_of_the_session(c):
    """Roll calls cut off at the end of a year leave no gap in the numbers
    (2007 stopped at 999, on Oct. 24). Every year since 2003 has had House
    votes in December, so a finished year whose last roll call is before
    Nov. 15 is probably missing its end. If the House truly adjourned that
    early, check the Clerk's list for that year and add an exception here."""
    last = {}
    for r in votes(c):
        y = int(r["Year"])
        last[y] = max(last.get(y, dt.date.min), clerk_date(r["Date"]))
    for year, day in last.items():
        if year < dt.date.today().year:
            assert day >= dt.date(year, 11, 15), f"{year}'s last roll call is {day}: are later ones missing?"


@pytest.mark.parametrize("c", CONGRESSES)
def test_every_member_has_votes(c):
    rows = votes(c)
    for m in CONFIG["congresses"][c]["members"]:
        col = f"{m['csvName']} ({m['id']})"
        assert col in rows[0], f"no column for {col}"
        assert any((r[col] or "").strip() for r in rows), f"{col} has no votes: wrong ID?"


@pytest.mark.parametrize("c", CONGRESSES)
def test_vote_info_matches_votes(c):
    path = DATA / c / vr.VOTE_INFO_NAME
    assert path.exists(), "no vote_info.json: run vote_rules.py"
    info = json.loads(path.read_text(encoding="utf-8"))
    rows = votes(c)
    keys = [vr.roll_key(r["Year"], r["Roll Call Number"]) for r in rows]
    assert list(info["rollCalls"]) == keys, "vote_info.json doesn't cover the same roll calls"
    # And it's current: what the rules give today.
    assert info["rollCalls"] == vr.classify(rows, vr.load_bills(DATA / c), vr.load_amendments(DATA / c)), "vote_info.json is stale: run vote_rules.py"
    assert any(v.get("final") for v in info["rollCalls"].values())


@pytest.mark.parametrize("c", CONGRESSES)
def test_house_loyalty_covers_roll_calls(c):
    loyalty = json.loads((DATA / c / "house_loyalty.json").read_text(encoding="utf-8"))
    rows = votes(c)
    # Vacated votes have no recorded votes, so the House-wide data can't cover them.
    vacated = sum(1 for r in rows if not any((r[k] or "0") != "0" for k in ("Yea Total", "Nay Total", "Present Total", "NV Total")))
    assert loyalty["rollCalls"] == len(rows)
    assert loyalty["missingRollCalls"] <= vacated
    assert any(e["rank"] for e in loyalty["variants"]["all"])


@pytest.mark.parametrize("c", [c for c in CONGRESSES if finished(c) and (DATA / c / "legislation.json").exists()])
def test_bill_lists_are_complete(c):
    leg = json.loads((DATA / c / "legislation.json").read_text(encoding="utf-8"))
    for m in CONFIG["congresses"][c]["members"]:
        entry = leg["members"].get(m["id"])
        assert entry, f"{m['displayName']} missing from legislation.json"
        # Every member cosponsors something over two years; 0 means the
        # list was cut short.
        assert entry["cosponsored"]["count"] > 0, f"{m['displayName']}: 0 cosponsored bills"


def test_history_is_current():
    import history
    path = DATA / history.HISTORY_NAME
    assert path.exists(), "no history.json: run history.py"
    assert json.loads(path.read_text(encoding="utf-8")) == history.build_history(ROOT), "history.json is stale: run history.py"
