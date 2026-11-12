# Starting a new Congress

A new Congress begins at noon on Jan. 3 of every odd year (the 120th on
Jan. 3, 2027). The nightly job handles the switch on its own once the new
delegation is in `members.json`. What it does without you:

- **First week of January:** it also re-scrapes the previous year, so
  late-December votes and corrections still land in the old Congress.
- **No delegation listed yet:** the run shows a "Delegation missing"
  warning and carries on with everything else. Nothing is lost: the
  first run after you add the delegation scrapes the new Congress from
  roll call 1.
- **Delegation listed but no votes yet:** not an error; it just waits.
- **The page:** if `current` points at a Congress with no votes yet, the
  page opens on the previous Congress instead of showing an error.

## Checklist

1. **Before Jan. 3, add the new Congress to `members.json`** under
   `congresses` (e.g. `"120"`), with `"years": [2027, 2028]` and one
   entry per member:

   ```json
   { "id": "D000197", "csvName": "DeGette", "displayName": "DeGette",
     "fullName": "Diana DeGette", "party": "D", "district": 1,
     "inCongressSince": "1997-01-07" }
   ```

   - Returning members: copy their entries from the previous Congress.
   - New members: their Bioguide ID is on
     [bioguide.congress.gov](https://bioguide.congress.gov/) and in the
     Clerk's member data once assigned. A wrong ID shows up as a member
     with no votes.
   - `csvName` becomes a column name in the vote file: never change it
     once the Congress has data. Use a first initial if two members share
     a last name ("J. Smith").
   - Colorado has eight districts.

2. **Leave `current` alone until the first votes are in.** After the
   first nightly run that commits `data/<congress>/all_votes.csv`, set
   `"current"` to the new Congress so the page opens on it.

3. **Check the new Congress.** Every member should have votes:

   ```sh
   python -c "import backfill; print(backfill.check(120))"
   ```

   Then open the page with `?congress=120`.

4. **Later, as needed:**
   - A member who leaves or arrives mid-Congress: add or keep their entry
     and a `note` (backfill.py writes "Took office ..." / "Left office
     ..." notes the same way). Votes outside their tenure count as "not
     in office".
   - A Congress-wide caveat (like proxy voting in 2020-22): a `note` on
     the Congress, with `"noteOn": "missed"` if it explains missed votes.
   - After swearing-in, `python backfill.py --dry-run 120` builds the
     delegation from the congress-legislators dataset, which is a useful
     cross-check of IDs, districts and start dates.
