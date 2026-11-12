"""The rules in vote_rules.py, on hand-made roll calls."""

import vote_rules as vr


def row(bill="H R 1", question="On Passage", result="Passed", description="", roll=1, **extra):
    r = {"Year": "2026", "Roll Call Number": str(roll), "Bill Number": bill, "Vote Question": question,
         "Result": result, "Description": description}
    r.update(extra)
    return r


RULE_BILLS = {"H RES 5": {"ruleFor": ["H R 1", "H R 2"], "policyArea": "Congress"},
              "H R 1": {"policyArea": "Health"}, "H R 2": {"policyArea": "Taxation"}}


# ---- final votes

def test_passage_is_final():
    assert vr.is_final_vote(row())


def test_amendment_is_not_final():
    assert not vr.is_final_vote(row(question="On Agreeing to the Amendment"))


def test_impeachment_articles_are_final():
    assert vr.is_final_vote(row(bill="H RES 755", question="On Agreeing to Article I of the Resolution"))
    assert vr.is_final_vote(row(bill="H RES 520", question="Article IV"))


def test_debate_rule_adoption_is_not_final():
    # Known as a rule from bill_topics.json, whatever its description says.
    rule = row(bill="H RES 5", question="On Agreeing to the Resolution", description="Departments of Labor ...")
    assert not vr.is_final_vote(rule, RULE_BILLS)
    # Without topics, the wording gives it away.
    for desc in ("Providing for consideration of H.R. 1", "Providing for further consideration of H.R. 1",
                 "Rule providing for consideration of H.R. 1", "Consideration of H.R. 1",
                 "Providing for disposition of the Senate amendment to H.R. 1",
                 "Providing for the adoption of H. Res. 240"):
        assert not vr.is_final_vote(row(bill="H RES 9", question="On Agreeing to the Resolution", description=desc)), desc


def test_resolution_on_its_own_terms_is_final():
    # Not a rule: an expulsion, a disapproval resolution.
    assert vr.is_final_vote(row(bill="H RES 878", question="On Agreeing to the Resolution",
                                description="Providing for the expulsion of Representative George Santos"))


def test_tabling_a_resolution_with_no_other_vote_is_final():
    tabled = row(bill="H RES 1655", question="On Motion to Refer", result="Passed")
    assert vr.final_flags([tabled]) == [True]
    assert vr.set_aside(tabled, True) == "Sent to committee"
    tabled2 = row(bill="H RES 939", question="On Motion to Table", result="Passed")
    assert vr.set_aside(tabled2, True) == "Tabled"


def test_tabling_is_not_final_when_the_resolution_got_a_vote():
    tabling = row(bill="H RES 9", question="On Motion to Table", result="Failed", roll=1)
    adopted = row(bill="H RES 9", question="On Agreeing to the Resolution", roll=2, description="Censuring someone")
    assert vr.final_flags([tabling, adopted]) == [False, True]
    # A failed motion never disposes of anything.
    assert vr.final_flags([row(bill="H RES 10", question="On Motion to Table", result="Failed")]) == [False]


def test_tabling_a_bill_is_not_final():
    assert vr.final_flags([row(bill="H R 7", question="On Motion to Table", result="Passed")]) == [False]


# ---- party majorities

def test_party_majority():
    r = row(**{"Dem Yea": "10", "Dem Nay": "200", "Rep Yea": "210", "Rep Nay": "5"})
    assert vr.party_majority(r, "D") == "N"
    assert vr.party_majority(r, "R") == "Y"
    assert vr.party_majority(r, "I") is None


def test_party_majority_tie_or_missing():
    assert vr.party_majority(row(**{"Dem Yea": "5", "Dem Nay": "5"}), "D") is None
    assert vr.party_majority(row(**{"Dem Yea": "", "Dem Nay": ""}), "D") is None


# ---- grouping, kinds, topics

def test_groups():
    assert vr.group_keys(row(bill="H R 1"), RULE_BILLS) == ["H R 1"]
    assert vr.group_keys(row(bill="H RES 5"), RULE_BILLS) == ["H R 1", "H R 2"]
    for no_bill in ("QUORUM", "JOURNAL", "ADJOURN", "MOTION", ""):
        assert vr.group_keys(row(bill=no_bill), RULE_BILLS) == [vr.HOUSE_BUSINESS]


def test_kinds():
    assert vr.vote_kind(row(), True, RULE_BILLS) == "final"
    assert vr.vote_kind(row(bill="H RES 5", question="On Ordering the Previous Question"), False, RULE_BILLS) == "rule"
    assert vr.vote_kind(row(question="On Agreeing to the Amendment"), False, RULE_BILLS) == "amendment"
    assert vr.vote_kind(row(question="On Motion to Recommit"), False, RULE_BILLS) == "recommit"
    assert vr.vote_kind(row(bill="QUORUM", question="Call of the House"), False, RULE_BILLS) == "other"
    # Concurring in Senate amendments is a final vote, not an amendment vote.
    concur = row(question="On Motion to Concur in the Senate Amendment")
    assert vr.vote_kind(concur, vr.is_final_vote(concur), RULE_BILLS) == "final"


def test_topics():
    assert vr.topics(row(bill="H R 1"), RULE_BILLS) == ["Health"]
    assert vr.topics(row(bill="H RES 5"), RULE_BILLS) == ["Health", "Taxation"]   # a rule takes its bills' topics
    assert vr.topics(row(bill="H R 99"), RULE_BILLS) == []


def test_classify_leaves_out_empty_fields():
    info = vr.classify([row(bill="QUORUM", question="Call of the House", result="")], RULE_BILLS)
    assert info == {"2026-1": {"kind": "other", "groups": [vr.HOUSE_BUSINESS]}}
    hegseth = vr.classify([row(bill="H RES 1655", question="On Motion to Refer",
                               **{"Dem Yea": "0", "Dem Nay": "203", "Rep Yea": "210", "Rep Nay": "1"})])
    assert hegseth["2026-1"] == {"kind": "final", "groups": ["H RES 1655"], "final": True,
                                 "setAside": "Sent to committee", "majority": {"D": "N", "R": "Y"}}


def test_write_vote_info_round_trips(tmp_path):
    import csv
    import json
    rows = [row(roll=1), row(bill="H RES 5", question="On Agreeing to the Resolution", roll=2)]
    with (tmp_path / vr.VOTES_NAME).open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    (tmp_path / vr.TOPICS_NAME).write_text(json.dumps({"bills": RULE_BILLS}))
    data = json.loads(vr.write_vote_info(tmp_path).read_text())
    assert data["houseBusiness"] == vr.HOUSE_BUSINESS
    assert list(data["rollCalls"]) == ["2026-1", "2026-2"]
    assert data["rollCalls"]["2026-2"]["kind"] == "rule"
    assert "final" not in data["rollCalls"]["2026-2"]
