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
