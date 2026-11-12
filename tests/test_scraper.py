"""Parsing the Clerk's roll calls, and the House-wide loyalty math.

Fixtures are real Clerk documents for 2007, trimmed to Colorado's members,
D.C.'s delegate and a few others: roll call 996 as XML and as an HTML vote
page (the same vote both ways), and roll call 1000, one of the 2007 votes
the Clerk has no XML for.
"""

from pathlib import Path

import vote_scraper as vs

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CO = {"D000197", "U000038", "L000564", "P000593", "S001158", "T000458", "M001152"}


def xml(roll):
    return vs.parse_roll_call_xml((FIXTURES / f"roll-2007-{roll}.xml").read_bytes(), 2007, roll, CO)


def page(roll):
    return vs.parse_vote_page((FIXTURES / f"vote-2007-{roll}.html").read_text(encoding="utf-8"), 2007, roll, CO)


def test_xml_roll_call():
    r = xml(996)
    assert (r.congress, r.session, r.date, r.time) == ("110", "1st", "24-Oct-2007", "12:08 PM")
    assert (r.bill_number, r.vote_question, r.vote_type, r.result) == ("H R 1483", "On Passage", "RECORDED VOTE", "Passed")
    assert r.totals == {"Yea Total": 291, "Nay Total": 122, "Present Total": 0, "NV Total": 19,
                        "Dem Yea": 222, "Dem Nay": 0, "Rep Yea": 69, "Rep Nay": 122}
    assert r.votes["D000197"] == "Aye" and r.votes["L000564"] == "No"
    assert set(r.votes) == CO


def test_html_page_matches_xml():
    """The fallback for roll calls with no XML must produce exactly what the XML would."""
    x, h = xml(996), page(996)
    for field in ("congress", "session", "date", "time", "bill_number", "vote_question", "vote_type",
                  "result", "description", "votes", "totals", "house"):
        assert getattr(h, field) == getattr(x, field), field


def test_html_delegate_state_matches_xml():
    # Delegates can't vote on passage, so they're not in roll 996; this is
    # one member row in the Clerk's page format. The page says "DC"; the XML
    # (and so the House-wide data) says "XX".
    row = ('<tr><td data-label="member"><a href="/Members/N000147" class="library-link">Norton</a></td>'
           '<td data-label="member" hidden>Norton</td><td data-label="party">Democratic</td>'
           '<td data-label="state" class="visible-sm visible-xs">DC</td><td data-label="vote">Aye</td></tr>')
    r = vs.parse_vote_page("<table>" + row + "</table>", 2009, 500, set())
    assert r.house["N000147"] == ("Y", "D", "XX", "Norton")


def test_html_only_roll_call():
    r = page(1000)
    assert (r.bill_number, r.result, r.date) == ("H R 505", "Passed", "24-Oct-2007")
    assert r.totals["Yea Total"] == 261 and r.totals["Dem Nay"] == 1 and r.totals["Rep Yea"] == 39
    assert set(r.votes) == CO


def test_html_page_without_votes():
    assert vs.parse_vote_page("<html><body><h1>Roll Call 1187</h1></body></html>", 2007, 1187, CO) is None


def test_vote_codes():
    assert [vs.vote_code(v) for v in ("Yea", "Aye", "Nay", "No", "Present", "Not Voting", "Pelosi", "")] == \
        ["Y", "Y", "N", "N", "P", "V", "O", ""]


def test_house_loyalty():
    # Three Democrats, one Republican, one delegate; two roll calls where
    # most Democrats voted yes.
    matrix = {
        "D1": {"meta": {"name": "One", "party": "D", "state": "CO"}, "codes": {"2026-1": "Y", "2026-2": "Y"}},
        "D2": {"meta": {"name": "Two", "party": "D", "state": "NY"}, "codes": {"2026-1": "Y", "2026-2": "N"}},
        "D3": {"meta": {"name": "Three", "party": "D", "state": "CA"}, "codes": {"2026-1": "V"}},
        "R1": {"meta": {"name": "Four", "party": "R", "state": "TX"}, "codes": {"2026-1": "N", "2026-2": "N"}},
        "DC": {"meta": {"name": "Norton", "party": "D", "state": "XX"}, "codes": {"2026-1": "Y", "2026-2": "Y"}},
    }
    rows = [{"Year": "2026", "Roll Call Number": str(n), "Vote Question": "On Passage", "Bill Number": f"H R {n}",
             "Dem Yea": "200", "Dem Nay": "10", "Rep Yea": "5", "Rep Nay": "200", "Description": ""} for n in (1, 2)]
    vs.MIN_RANKED_VOTES, saved = 1, vs.MIN_RANKED_VOTES
    try:
        out = vs.compute_house_loyalty(matrix, rows)
    finally:
        vs.MIN_RANKED_VOTES = saved
    by_id = {e["id"]: e for e in out["variants"]["all"]}
    assert (by_id["D1"]["withParty"], by_id["D1"]["counted"]) == (2, 2)
    assert (by_id["D2"]["withParty"], by_id["D2"]["counted"]) == (1, 2)
    assert (by_id["D3"]["missed"], by_id["D3"]["total"], by_id["D3"]["counted"]) == (1, 1, 0)   # not in office for roll 2
    assert by_id["D1"]["rank"] == 1 and by_id["D2"]["rank"] == 2
    assert by_id["DC"]["delegate"] and by_id["DC"]["rank"] is None   # delegates are never ranked
    assert out["rollCalls"] == 2 and out["missingRollCalls"] == 0
