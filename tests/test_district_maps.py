"""district_maps.py's geometry, and the map files it wrote."""

import json
from pathlib import Path

import district_maps as dm

ROOT = Path(__file__).resolve().parent.parent
MAPS = ROOT / "data" / "maps"


def test_clip_keeps_the_part_inside():
    square = [(0, 0), (10, 0), (10, 10), (0, 10)]
    clipped = dm.clip(square, (5, -1, 20, 20))
    xs = sorted({round(x, 6) for x, _ in clipped})
    assert xs == [5, 10]
    assert dm.clip(square, (20, 20, 30, 30)) == []


def test_thin_drops_close_points():
    assert dm.thin([(0, 0), (0.1, 0), (5, 0), (5.1, 0.1), (10, 0)], 1) == [(0, 0), (5, 0), (10, 0)]


def test_zoom_view_has_the_page_aspect():
    x, y, w, h = dm.zoom_view((100, 100, 140, 120))
    assert abs(w / h - dm.ZOOM_ASPECT) < 0.01
    assert x < 100 and x + w > 140 and y < 100 and y + h > 120


def test_small_and_thin_districts_are_zoomed():
    def rect(x0, y0, x1, y1):
        return [[(x0, y0), (x1, y0), (x1, y1), (x0, y1)]]
    districts = {1: rect(0, 0, 50, 50),          # small
                 2: rect(0, 100, 300, 160),      # thin
                 3: rect(300, 0, 1000, 700)}     # big
    era = {"first": 1, "last": None, "years": "2001–"}
    out = dm.build_era("test", era, districts)
    assert sorted(out["zoom"]) == ["1", "2"]
    assert set(out["detail"]["districts"]) >= {"1", "2"}


def test_every_congress_has_lines_for_each_district():
    index = json.loads((MAPS / "index.json").read_text(encoding="utf-8"))["lines"]
    config = json.loads((ROOT / "members.json").read_text(encoding="utf-8"))
    for c, info in config["congresses"].items():
        districts = {m["district"] for m in info["members"] if m.get("district")}
        if not districts:
            continue
        lines = [k for k, e in index.items() if e["first"] <= int(c) and (e["last"] is None or int(c) <= e["last"])]
        assert len(lines) == 1, f"Congress {c}: lines {lines}"
        data = json.loads((MAPS / f"{lines[0]}.json").read_text(encoding="utf-8"))
        assert districts <= {int(d) for d in data["districts"]}, f"Congress {c}"
        for d in data["zoom"]:
            assert d in data["detail"]["districts"], f"{lines[0]}: district {d} zoomed without detail"
