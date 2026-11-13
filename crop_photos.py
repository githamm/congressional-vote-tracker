"""Member photos for the page: square crops centered on the face.

Put originals, at any size and framing, in photos/originals/ (git-ignored:
only the crops are published), named by member ID (B000825.jpg) or by name
as in members.json (Lauren-Boebert.jpg). Then run, on a Mac:

    python crop_photos.py            # crop every original
    python crop_photos.py B000825    # just these members

For each member it writes photos/<id>.jpg (400px, for the member card) and
photos/<id>-sm.jpg (100px, for the overview table), and lists the members
with photos in photos/index.json, which the page reads. To check the crops,
open .cache/contact-sheet.html through a local server (see README).

The face is found with macOS's Vision framework (photo_faces.swift) and
the crop sized so every face takes up the same share of its circle. If one
looks off, adjust it in photos/crops.json, which survives reruns:

    {"B000825": {"zoom": 1.1, "up": 0.05, "credit": "Courtesy of ..."}}

zoom > 1 shows more around the face; up/right move the face up/right by a
share of the crop. credit is kept in index.json.
"""

import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PHOTOS = ROOT / "photos"
ORIGINALS = PHOTOS / "originals"
SETTINGS = PHOTOS / "crops.json"
INDEX = PHOTOS / "index.json"
FACES_SRC = ROOT / "photo_faces.swift"
FACES_BIN = ROOT / ".cache" / "photo_faces"

SIZES = {"": 400, "-sm": 100}
FACE_SHARE = 0.48      # the face box's height as a share of the crop
FACE_CENTER = 0.42     # where the face's center sits, from the top of the crop
MIN_SOURCE = 240       # a crop smaller than this (pixels) will look soft on the card
EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".tif", ".tiff"}


def slug(name: str) -> str:
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")


def member_ids() -> dict[str, str]:
    """Slugs of every member's name, and every ID, to that member's ID."""
    config = json.loads((ROOT / "members.json").read_text(encoding="utf-8"))
    ids = {}
    for info in config["congresses"].values():
        for m in info["members"]:
            ids[m["id"].lower()] = m["id"]
            for n in (m.get("fullName"), m.get("displayName")):
                if n:
                    ids.setdefault(slug(n), m["id"])
    return ids


def find_faces(paths: list[Path]) -> dict[Path, dict]:
    if not FACES_BIN.exists() or FACES_BIN.stat().st_mtime < FACES_SRC.stat().st_mtime:
        FACES_BIN.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["swiftc", "-O", str(FACES_SRC), "-o", str(FACES_BIN)], check=True)
    out = subprocess.run([str(FACES_BIN), *map(str, paths)], check=True, capture_output=True, text=True).stdout
    return {Path(d["file"]): d for d in map(json.loads, out.splitlines())}


def crop_box(info: dict, adjust: dict) -> tuple[int, int, int]:
    """(left, top, side) of the square crop, kept inside the image."""
    w, h = info["w"], info["h"]
    if "face" in info:
        fx, fy, fw, fh = info["face"]
        side = fh / FACE_SHARE
        cx = fx + fw / 2
        top = fy + fh / 2 - FACE_CENTER * side
    else:   # no face found: the upper middle, where portraits put the head
        side = min(w, h)
        cx, top = w / 2, 0.0
    zoom = adjust.get("zoom", 1.0)
    new_side = side * zoom
    top -= (new_side - side) * FACE_CENTER
    side = new_side
    cx -= adjust.get("right", 0.0) * side
    top += adjust.get("up", 0.0) * side
    side = min(side, w, h)
    left = min(max(cx - side / 2, 0), w - side)
    top = min(max(top, 0), h - side)
    return round(left), round(top), round(side)


def crop(src: Path, member_id: str, box: tuple[int, int, int]) -> None:
    left, top, side = box
    PHOTOS.mkdir(exist_ok=True)
    # sips resizes before it crops when given both, so crop first, separately.
    square = ROOT / ".cache" / f"{member_id}-square.png"
    square.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["sips", "-s", "format", "png", "--cropToHeightWidth", str(side), str(side),
                    "--cropOffset", str(top), str(left), str(src), "--out", str(square)],
                   check=True, capture_output=True)
    for suffix, size in SIZES.items():
        subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "82",
                        "--resampleHeightWidth", str(size), str(size),
                        str(square), "--out", str(PHOTOS / f"{member_id}{suffix}.jpg")],
                       check=True, capture_output=True)
    square.unlink()


def contact_sheet(index: dict) -> Path:
    """Every current member's crops at card and table size, to check by eye."""
    config = json.loads((ROOT / "members.json").read_text(encoding="utf-8"))
    members = config["congresses"][config["current"]]["members"]
    cells = []
    for m in members:
        if m["id"] in index:
            img = f'<img class="big" src="../photos/{m["id"]}.jpg"><img class="sm" src="../photos/{m["id"]}-sm.jpg">'
        else:
            img = '<span class="big none">no photo yet</span><span class="sm none"></span>'
        cells.append(f'<figure>{img}<figcaption>{m.get("fullName") or m["displayName"]}</figcaption></figure>')
    path = ROOT / ".cache" / "contact-sheet.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "<!doctype html><meta charset=utf-8><title>Photo crops</title><style>"
        "body{font-family:-apple-system,sans-serif;margin:20px}div{display:grid;grid-template-columns:repeat(4,160px);gap:22px}"
        "figure{margin:0;text-align:center}img,.none{display:inline-block;border-radius:50%;border:3px solid #C9C7C0;"
        "vertical-align:middle;background:#F7F6F2}.big{width:120px;height:120px}"
        ".sm{width:42px;height:42px;border-width:2px;margin-left:-14px}.none{font-size:11px;line-height:120px;color:#888}"
        ".sm.none{line-height:42px}figcaption{font-size:12px;font-weight:600;margin-top:6px}</style>"
        "<p>Card size (120px) and table size (42px)</p><div>" + "".join(cells) + "</div>\n", encoding="utf-8")
    return path


def main(argv: list[str]) -> int:
    ids = member_ids()
    settings = json.loads(SETTINGS.read_text(encoding="utf-8")) if SETTINGS.exists() else {}
    originals = {}
    for p in sorted(ORIGINALS.glob("*")):
        if p.suffix.lower() not in EXTENSIONS:
            continue
        mid = ids.get(p.stem.lower()) or ids.get(slug(p.stem))
        if not mid:
            print(f"  ? {p.name}: no member with that ID or name in members.json; skipped")
            continue
        originals[mid] = p
    wanted = set(argv) or set(originals)
    todo = {m: p for m, p in originals.items() if m in wanted}
    if not todo:
        print(f"No originals to crop in {ORIGINALS.relative_to(ROOT)}/")
        return 1
    faces = find_faces(list(todo.values()))
    index = json.loads(INDEX.read_text(encoding="utf-8")).get("photos", {}) if INDEX.exists() else {}
    for mid, src in sorted(todo.items()):
        info = faces[src]
        if "error" in info:
            print(f"  ! {src.name}: couldn't read it")
            continue
        box = crop_box(info, settings.get(mid, {}))
        crop(src, mid, box)
        note = "" if info.get("faces") else "  (no face found: cropped the upper middle; check it)"
        if box[2] < MIN_SOURCE:
            note += f"  (crop is only {box[2]}px: a larger original would be sharper)"
        print(f"  {mid}  {src.name}  crop {box[2]}px at ({box[0]}, {box[1]}){note}")
        index[mid] = {k: v for k, v in {"credit": settings.get(mid, {}).get("credit")}.items() if v}
    INDEX.write_text(json.dumps({
        "_comment": "Members with a photo: photos/<id>.jpg (400px) and photos/<id>-sm.jpg (100px). "
                    "Built by crop_photos.py.",
        "photos": dict(sorted(index.items())),
    }, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Check the crops: {contact_sheet(index).relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
