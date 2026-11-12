"""Each Colorado member's record across Congresses, for the page's
"Across Congresses" view: data/history.json.

Built from files the other scripts already write, so it needs no network:
house_loyalty.json (party loyalty, rank, missed votes) and legislation.json
(bills sponsored or co-led, and how many became law) for every Congress in
members.json. vote_scraper.py and fetch_legislation.py rebuild it after
updating a Congress; run it directly with `python history.py`.
"""

import json
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
HISTORY_NAME = "history.json"


def _read(path: Path) -> Optional[dict]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def build_history(root: Path = ROOT) -> dict:
    config = json.loads((root / "members.json").read_text(encoding="utf-8"))
    members: dict[str, dict] = {}
    for c in sorted(config["congresses"], key=int):
        folder = root / "data" / c
        loyalty = _read(folder / "house_loyalty.json")
        if not loyalty:
            continue  # no votes yet
        house = {e["id"]: e for e in loyalty["variants"]["all"]}
        leg = (_read(folder / "legislation.json") or {}).get("members", {})
        for m in config["congresses"][c]["members"]:
            h = house.get(m["id"])
            if not h:
                continue
            entry = members.setdefault(m["id"], {"name": m.get("fullName") or m["displayName"], "congresses": {}})
            record = {
                "party": m["party"],
                "district": m.get("district"),
                "withParty": h["withParty"],
                "counted": h["counted"],
                "rank": h["rank"],
                "of": h["of"],
                "missed": h["missed"],
                "total": h["total"],
            }
            bills = leg.get(m["id"])
            if bills:
                coled = bills.get("coled") or {"count": 0, "becameLaw": 0}
                record["bills"] = bills["sponsored"]["count"] + coled["count"]
                record["becameLaw"] = bills["sponsored"]["becameLaw"] + coled["becameLaw"]
            entry["congresses"][c] = record
            entry["name"] = m.get("fullName") or entry["name"]   # latest wins
    return {
        "_comment": "Each Colorado member's record in every Congress in this repo: party loyalty "
                    "(withParty of counted yes-or-no votes; rank of 'of' in their party), missed votes "
                    "(of total while in office), and bills sponsored or co-led and how many became law. "
                    "Built by history.py from house_loyalty.json and legislation.json.",
        "members": members,
    }


def write_history(root: Path = ROOT) -> Path:
    path = root / "data" / HISTORY_NAME
    path.write_text(json.dumps(build_history(root), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


if __name__ == "__main__":
    print("Wrote", write_history().relative_to(ROOT))
