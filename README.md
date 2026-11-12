# Colorado Congressional Vote Tracker

How Colorado's members of the U.S. House vote, from the 108th Congress
(2003) to today: every roll call, each member's party loyalty and
attendance compared with the whole House, the votes where they broke
ranks, and the bills they sponsored. It's built as a historical record,
not just a view of the current delegation.

A Denver Post project by Kevin Hamm. Vote data comes from the
[Clerk of the U.S. House](https://clerk.house.gov/Votes); bill data from
the [Congress.gov API](https://api.congress.gov/).

## How it fits together

`index.html` is the whole page: one file, no build step. Published, it
reads its data from this repository's `main` branch on GitHub, so the
nightly data update shows up without redeploying the page. Run locally,
it reads the files next to it.

| File | What it holds | Written by |
|---|---|---|
| `members.json` | Colorado's delegation for each Congress: IDs, names, parties, districts, notes. `current` is the Congress the page opens on. | Hand, or `backfill.py` |
| `data/<c>/all_votes.csv` | One row per roll call: the question, result, House and party totals, and each Colorado member's vote | `vote_scraper.py` |
| `data/<c>/house_votes.csv` | Every House member's vote on every roll call | `vote_scraper.py` |
| `data/<c>/house_loyalty.json` | Party loyalty, attendance and rank for every House member | `vote_scraper.py` |
| `data/<c>/house_matches.json` | The House members who vote most like each Colorado member | `vote_scraper.py` |
| `data/<c>/legislation.json` | Bills each Colorado member sponsored or co-led, and how far they got | `fetch_legislation.py` |
| `data/<c>/bill_topics.json` | Each voted-on bill's title and Congress.gov topic; which bills each debate rule set up | `fetch_legislation.py` |
| `data/<c>/bill_progress.json` | Cache of bill actions, so later runs make fewer API calls (not read by the page) | `fetch_legislation.py` |
| `data/<c>/vote_info.json` | How each roll call is classified (see below) | `vote_rules.py`, called by both scripts |
| `data/house_names.json` | Full names for House members shown as closest matches | `vote_scraper.py` |

The root `all_votes.csv` is a copy of the current Congress's file for the
previously deployed page; it can go once that page is retired.

## The vote rules

Every decision about what a vote *means* lives in `vote_rules.py`, and its
results are written to each Congress's `vote_info.json`. The page reads
that file and never applies the rules itself, so a rule changes in one
place. After changing one, rebuild every Congress with
`python vote_rules.py` (and run the tests).

- **Final vote:** the vote that decided a bill or resolution: passage,
  adoption, agreeing to the Senate's changes, a veto override, an article
  of impeachment. Amendments, motions to recommit and votes on debate
  rules aren't final.
- **Set aside:** an impeachment, censure or other privileged resolution
  killed by a successful motion to table or refer, with no up-or-down
  vote of its own. That motion counts as the final vote, and the page
  calls the outcome "Tabled" or "Sent to committee" rather than "Passed".
- **Debate rule:** a resolution setting the terms of debate for other
  bills. Its votes are listed under each bill it set up, never as final.
  The surest sign is Congress.gov's link from the rule to those bills;
  the wording ("Providing for consideration of ...") is the fallback.
- **Broke with party:** voted yes or no against the majority of their own
  party in the whole House. Ties, quorum calls and Speaker elections
  don't count. **Party loyalty** is the share of a member's yes-or-no
  votes that matched their party's majority; ranks include members with
  at least 100 such votes. Delegates from D.C. and the territories are
  left out of all comparisons.
- **House business:** votes on no bill (quorum calls, the Journal,
  adjournment, Speaker elections), grouped together on the page.
- **Not in office:** a blank vote means the member wasn't serving yet or
  any longer. Those roll calls are left out of the member's figures; a
  "Not Voting" is a missed vote.

## Running it locally

Python 3.12 or later (the nightly job uses 3.12).

```sh
python3 -m venv vote-tracker
vote-tracker/bin/pip install -r requirements.txt
```

The bill fetch needs a free [Congress.gov API key](https://api.congress.gov/sign-up/),
from the `CONGRESS_API_KEY` environment variable or a git-ignored `.env`
file containing `CONGRESS_API_KEY=...`.

```sh
vote-tracker/bin/python vote_scraper.py --year 2026 --incremental   # new roll calls
vote-tracker/bin/python fetch_legislation.py                        # bills, current Congress
vote-tracker/bin/python fetch_legislation.py --congress 118 119     # bills, other Congresses
vote-tracker/bin/python vote_rules.py                               # rebuild vote_info.json
python3 -m http.server                                              # then open http://localhost:8000/
vote-tracker/bin/python -m pytest -q                                # tests
```

## Tests

`tests/` has unit tests for the vote rules, the Clerk parsing (real Clerk
files, trimmed, in `tests/fixtures/`), the loyalty math and the bill
fetch, plus checks on the real data:

- no roll calls missing from a year, including at the end of a finished year
- every member has votes (an empty column means a wrong ID)
- `vote_info.json` matches the votes and the current rules
- bill lists for finished Congresses aren't cut short
- the page's script parses (needs Node; skipped without it)

If a data check fails, its message says what to look at.

## The nightly job

`.github/workflows/nightly_run.yml` runs at 22:00 and 04:00 UTC, so votes
taken in the evening Eastern time land the same night. It:

1. scrapes new roll calls for the current year, plus the previous year
   during the first week of January;
2. fetches bills for those Congresses (needs the `CONGRESS_API_KEY`
   repository secret; skipped without it, and a failure doesn't stop the
   run);
3. runs the tests, and **commits nothing if they fail**, so bad data
   doesn't publish;
4. commits and pushes any changed data.

A missing delegation in `members.json` (the start of a new Congress) is
reported as a warning, not a failure. On a long recess the job pushes an
empty commit so GitHub doesn't switch off the schedule.

## Adding a past Congress

`backfill.py` adds Congresses end to end: the delegation in
`members.json` (from the public-domain
[congress-legislators](https://github.com/unitedstates/congress-legislators)
dataset), the roll calls and the bills. It's resumable and logs to the
git-ignored `backfill.log`, with a check of each Congress at the end.

```sh
vote-tracker/bin/python backfill.py              # 117th back to 108th
vote-tracker/bin/python backfill.py 112 111      # just these
vote-tracker/bin/python backfill.py --dry-run 110
```

- The Clerk's files identify members by ID only from 2003 (the 108th).
  Earlier Congresses would need matching members by name (1990-2002) or
  another source such as Voteview (before 1990).
- Congress.gov allows 5,000 API calls an hour; a first bill fetch for a
  Congress takes about 2,000-3,000. The script waits out the limit, so a
  full backfill runs for hours: start it in your own terminal (with
  `caffeinate -i` on a Mac).
- Some roll calls have no XML file at the Clerk, e.g. 2007's 1000-1186;
  the scraper reads the Clerk's HTML vote page for those instead.

## Starting a new Congress

See [NEW_CONGRESS.md](NEW_CONGRESS.md). In short: add the new delegation
to `members.json` before Jan. 3, and set `current` once the first votes
are in.

## Editorial conventions

- A member's `note` (an extended absence, a late start) and a Congress's
  `note` (such as proxy voting in 2020-22) appear on the page. Write one
  only from a source you've confirmed, and link it with `noteUrl`.
- The page shows when a member missed votes, never why. Explanations come
  only from sourced notes.
- `csvName` becomes a column name in the vote files: never change it for a
  Congress that already has data.
