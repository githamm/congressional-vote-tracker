"""Paging through a member's bill lists (Congress.gov)."""

import pytest

import fetch_legislation as fl


class FakeClient(fl.Client):
    """Serves canned pages instead of calling the API."""

    def __init__(self, pages, count):
        self.pages, self.count, self.calls = pages, count, 0

    def get(self, path, **params):
        self.calls += 1
        i = params["offset"] // fl.PAGE
        page = self.pages[i] if i < len(self.pages) else []
        return {"cosponsoredLegislation": page, "pagination": {"count": self.count}}


def items(*congresses):
    return [{"congress": c, "type": "HR", "number": str(i)} for i, c in enumerate(congresses)]


def test_reads_every_page_even_when_out_of_order():
    # Congress.gov's lists are only roughly newest-first (DeGette's jump
    # from the 116th back to the 112th and forward again); stopping at the
    # first older Congress once dropped whole Congresses.
    pages = [items(*[119] * fl.PAGE), items(*[112] * fl.PAGE), items(116, 110)]
    client = FakeClient(pages, count=2 * fl.PAGE + 2)
    got = client.member_items("D000197", "cosponsored-legislation")
    assert len(got) == 2 * fl.PAGE + 2
    assert {it["congress"] for it in got} == {119, 112, 116, 110}
    assert client.calls == 3


def test_short_read_fails_loudly():
    client = FakeClient([items(119, 118)], count=5)
    with pytest.raises(SystemExit):
        client.member_items("D000197", "cosponsored-legislation")


# ---- amendments

def test_clean_purpose():
    assert fl.clean_purpose("An amendment numbered 38 printed in the Congressional Record to prohibit use of funds.") \
        == "to prohibit use of funds."
    assert fl.clean_purpose("An amendment numbered 316 printed in Part A of House Report 119-755 to require a report.") \
        == "to require a report."
    assert fl.clean_purpose("Amendment sought to require a report.") == "sought to require a report."
    assert fl.clean_purpose("Amendment prohibits the use of cameras.") == "prohibits the use of cameras."
    # Anything else is left alone.
    assert fl.clean_purpose("At the end of the bill, insert the following.") == "At the end of the bill, insert the following."


class FakeAmendmentClient(fl.Client):
    def __init__(self, routes):
        self.routes, self.calls = routes, 0

    def get(self, path, **params):
        self.calls += 1
        return self.routes[path]


def test_amendment_votes_matched(monkeypatch, tmp_path):
    # Two amendment votes on H.R. 1: roll 10's amendment has the vote as its
    # latest action; roll 11's had a later action, so its history is read.
    monkeypatch.setattr(fl, "amendment_votes", lambda c: {"H R 1": {"2026-10": "", "2026-11": ""}})
    monkeypatch.setattr(fl, "ROOT", tmp_path)
    (tmp_path / "data" / "119").mkdir(parents=True)
    routes = {
        "/bill/119/hr/1/amendments": {"pagination": {"count": 3}, "amendments": [
            {"number": "5", "type": "HAMDT", "purpose": "Amendment sought to cut X.",
             "latestAction": {"actionDate": "2026-03-01", "text": "On agreeing to the Smith (NJ) amendment (A001) Failed by recorded vote: 200 - 220 (Roll no. 10)."}},
            {"number": "6", "type": "HAMDT", "purpose": "An amendment numbered 2 printed in the Congressional Record to add Y.",
             "latestAction": {"actionDate": "2026-03-02", "text": "Motion to reconsider laid on the table Agreed to without objection."}},
            {"number": "7", "type": "HAMDT", "purpose": "Voice-voted.",
             "latestAction": {"actionDate": "2026-03-02", "text": "On agreeing to the Jones amendment Agreed to by voice vote."}},
        ]},
        "/amendment/119/hamdt/6/actions": {"actions": [
            {"actionDate": "2026-03-01", "text": "On agreeing to the Doe amendment (A002) Agreed to by recorded vote: 230 - 190 (Roll no. 11).",
             "recordedVotes": [{"rollNumber": 11, "chamber": "House", "date": "2026-03-01T15:00:00Z"}]}]},
    }
    client = FakeAmendmentClient(routes)
    fl.update_amendments(client, "119")
    import json
    votes = json.loads((tmp_path / "data" / "119" / fl.AMENDMENTS_NAME).read_text())["votes"]
    assert votes["2026-10"]["author"] == "Smith (NJ)" and votes["2026-10"]["purpose"] == "sought to cut X."
    assert votes["2026-10"]["amendment"] == "H.Amdt. 5"
    assert votes["2026-11"]["author"] == "Doe" and votes["2026-11"]["purpose"] == "to add Y."
    assert client.calls == 2   # the list, plus one history; the voice-voted one isn't looked up
