"""Colorado's congressional district maps for the member pages:
data/maps/<era>.json, one file per set of district lines.

Colorado has had three sets of lines in the years this tracker covers:

    2002 lines   108th-112th Congresses (2003-2012), seven districts
    2012 lines   113th-117th Congresses (2013-2022), seven districts
    2022 lines   118th Congress on (2023-), eight districts

Each comes from a Census Bureau cartographic boundary file (public domain),
downloaded once into .cache/census/ (git-ignored). This script keeps
Colorado, projects it flat (fine at a state's scale), thins the outlines
and writes SVG path data, so the page draws the maps without any map code:

    districts   every district at state scale
    detail      the Denver area at finer detail, for districts too small to
                see on the state map; "zoom" gives each such district's view
    capital     Denver, in the same coordinates

data/maps/index.json says which lines each Congress used. Run it after adding a new set of lines (after a census, add an entry to
ERAS): `python district_maps.py`. It needs the network only for files not
already in the cache.
"""

import io
import json
import math
import re
import struct
import sys
import zipfile
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "maps"
CACHE = ROOT / ".cache" / "census"
CO_FIPS = "08"

CB = "https://www2.census.gov/geo/tiger/"
ERAS = {
    "2002": {"first": 108, "last": 112, "years": "2003–2012",
             "url": CB + "GENZ2010/gz_2010_08_500_11_500k.zip"},   # 111th Congress
    "2012": {"first": 113, "last": 117, "years": "2013–2022",
             "url": CB + "GENZ2018/shp/cb_2018_us_cd116_500k.zip"},  # 116th
    "2022": {"first": 118, "last": None, "years": "2023–",
             "url": CB + "GENZ2024/shp/cb_2024_us_cd119_500k.zip"},  # 119th
}

# Colorado's corners, a little padded, and the flat projection's scale.
LON0, LON1, LAT0, LAT1 = -109.07, -102.03, 36.98, 41.02
WIDTH = 1000.0
KX = WIDTH / (LON1 - LON0)
KY = KX / math.cos(math.radians(39.0))
HEIGHT = round((LAT1 - LAT0) * KY, 1)

STATE_TOLERANCE = 2.5    # drop points closer than this (map units) on the state map
DETAIL_TOLERANCE = 0.3   # ... and on the Denver-area map
SMALL = 110              # a district narrower and shorter than this is zoomed in,
THIN = 80                # ... and so is one narrower or shorter than this
ZOOM_ASPECT = 180 / 150  # width / height of the zoomed view on the page

# Denver, marked on every map as the capital.
CAPITAL = (-104.99, 39.74)


def project(lon: float, lat: float) -> tuple[float, float]:
    return (lon - LON0) * KX, (LAT1 - lat) * KY


# ---- reading the Census files

def download(url: str) -> bytes:
    path = CACHE / url.rsplit("/", 1)[1]
    if not path.exists():
        r = requests.get(url, timeout=120)
        r.raise_for_status()
        CACHE.mkdir(parents=True, exist_ok=True)
        path.write_bytes(r.content)
    return path.read_bytes()


def read_dbf(data: bytes) -> list[dict]:
    n, header_len, record_len = struct.unpack("<IHH", data[4:12])
    fields, off = [], 32
    while data[off] != 0x0D:
        fields.append((data[off:off + 11].split(b"\0")[0].decode(), data[off + 16]))
        off += 32
    records = []
    for i in range(n):
        raw, pos, rec = data[header_len + i * record_len: header_len + (i + 1) * record_len], 1, {}
        for name, size in fields:
            rec[name] = raw[pos:pos + size].decode("latin-1").strip()
            pos += size
        records.append(rec)
    return records


def read_shp(data: bytes) -> list[list[list[tuple[float, float]]]]:
    """Each shape as a list of rings of (lon, lat). Polygons only."""
    shapes, pos = [], 100
    while pos < len(data):
        _, words = struct.unpack(">ii", data[pos:pos + 8])
        body = data[pos + 8: pos + 8 + words * 2]
        pos += 8 + words * 2
        if struct.unpack("<i", body[:4])[0] != 5:
            shapes.append([])
            continue
        nparts, npoints = struct.unpack("<ii", body[36:44])
        starts = list(struct.unpack(f"<{nparts}i", body[44:44 + 4 * nparts])) + [npoints]
        p0 = 44 + 4 * nparts
        points = [struct.unpack("<dd", body[p0 + 16 * k: p0 + 16 * k + 16]) for k in range(npoints)]
        shapes.append([points[starts[j]:starts[j + 1]] for j in range(nparts)])
    return shapes


def colorado_districts(zipped: bytes) -> dict[int, list[list[tuple[float, float]]]]:
    """District number -> rings in map units."""
    with zipfile.ZipFile(io.BytesIO(zipped)) as z:
        names = z.namelist()
        shp = next(n for n in names if n.endswith(".shp"))
        records = read_dbf(z.read(shp[:-4] + ".dbf"))
        shapes = read_shp(z.read(shp))
    state_field = "STATEFP" if "STATEFP" in records[0] else "STATE"
    cd_field = next(k for k in records[0] if re.fullmatch(r"CD\d*(FP)?", k))
    out: dict[int, list] = {}
    for rec, rings in zip(records, shapes):
        if rec[state_field] != CO_FIPS or not rec[cd_field].isdigit() or not rings:
            continue
        out.setdefault(int(rec[cd_field]), []).extend([project(*p) for p in ring] for ring in rings)
    return out


# ---- geometry

def thin(ring: list[tuple[float, float]], tolerance: float) -> list[tuple[float, float]]:
    """Drop points closer than tolerance to the last one kept."""
    kept = [ring[0]]
    for p in ring[1:]:
        if abs(p[0] - kept[-1][0]) + abs(p[1] - kept[-1][1]) >= tolerance:
            kept.append(p)
    return kept


def clip(ring: list[tuple[float, float]], box: tuple[float, float, float, float]) -> list[tuple[float, float]]:
    """Sutherland-Hodgman: the part of a ring inside box (x0, y0, x1, y1)."""
    x0, y0, x1, y1 = box
    edges = [(lambda p: p[0] >= x0, lambda a, b: (x0, a[1] + (b[1] - a[1]) * (x0 - a[0]) / (b[0] - a[0]))),
             (lambda p: p[0] <= x1, lambda a, b: (x1, a[1] + (b[1] - a[1]) * (x1 - a[0]) / (b[0] - a[0]))),
             (lambda p: p[1] >= y0, lambda a, b: (a[0] + (b[0] - a[0]) * (y0 - a[1]) / (b[1] - a[1]), y0)),
             (lambda p: p[1] <= y1, lambda a, b: (a[0] + (b[0] - a[0]) * (y1 - a[1]) / (b[1] - a[1]), y1))]
    out = ring
    for inside, cross in edges:
        src, out = out, []
        for i, cur in enumerate(src):
            prev = src[i - 1]
            if inside(cur):
                if not inside(prev):
                    out.append(cross(prev, cur))
                out.append(cur)
            elif inside(prev):
                out.append(cross(prev, cur))
        if not out:
            break
    return out


def bbox(rings) -> tuple[float, float, float, float]:
    xs = [p[0] for r in rings for p in r]
    ys = [p[1] for r in rings for p in r]
    return min(xs), min(ys), max(xs), max(ys)


def path(rings, tolerance: float) -> str:
    parts = []
    for ring in rings:
        pts = thin(ring, tolerance)
        if len(pts) > 2:
            parts.append("M" + "L".join(f"{x:.1f},{y:.1f}" for x, y in pts) + "Z")
    return "".join(parts)


def zoom_view(b: tuple[float, float, float, float]) -> list[float]:
    """A view around a district's box: padded, at the page's aspect ratio."""
    x0, y0, x1, y1 = b
    w, h = (x1 - x0) * 1.3, (y1 - y0) * 1.3
    w, h = max(w, h * ZOOM_ASPECT), max(h, w / ZOOM_ASPECT)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return [round(cx - w / 2, 1), round(cy - h / 2, 1), round(w, 1), round(h, 1)]


def build_era(key: str, era: dict, districts: dict) -> dict:
    boxes = {n: bbox(r) for n, r in districts.items()}
    small = sorted(n for n, b in boxes.items()
                   if max(b[2] - b[0], b[3] - b[1]) < SMALL or min(b[2] - b[0], b[3] - b[1]) < THIN)
    zoom = {str(n): zoom_view(boxes[n]) for n in small}
    out = {
        "_comment": f"Colorado's congressional districts as drawn for {era['years']}, from the Census Bureau's "
                    "cartographic boundary files (public domain). Built by district_maps.py; paths are SVG "
                    "path data in the map's own units.",
        "lines": key, "years": era["years"], "first": era["first"], "last": era["last"],
        "width": WIDTH, "height": HEIGHT,
        "districts": {str(n): path(districts[n], STATE_TOLERANCE) for n in sorted(districts)},
        "zoom": zoom,
        "capital": [round(v, 1) for v in project(*CAPITAL)],
    }
    if zoom:
        views = list(zoom.values())
        box = (min(v[0] for v in views), min(v[1] for v in views),
               max(v[0] + v[2] for v in views), max(v[1] + v[3] for v in views))
        detail = {}
        for n in sorted(districts):
            rings = [c for c in (clip(r, box) for r in districts[n]) if len(c) > 2]
            d = path(rings, DETAIL_TOLERANCE)
            if d:
                detail[str(n)] = d
        out["detail"] = {"box": [round(v, 1) for v in box], "districts": detail}
    return out


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for key, era in ERAS.items():
        districts = colorado_districts(download(era["url"]))
        data = build_era(key, era, districts)
        path_out = OUT / f"{key}.json"
        path_out.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        print(f"{path_out.relative_to(ROOT)}: {len(districts)} districts, zoomed {sorted(data['zoom'], key=int)}, "
              f"{path_out.stat().st_size // 1024} KB")
    # Which lines each Congress used, so the page knows which file to load.
    index = {"_comment": "Which district lines each Congress used: data/maps/<lines>.json covers Congresses "
                         "first through last (null: still in use). Built by district_maps.py.",
             "lines": {k: {"first": e["first"], "last": e["last"], "years": e["years"]} for k, e in ERAS.items()}}
    (OUT / "index.json").write_text(json.dumps(index, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
