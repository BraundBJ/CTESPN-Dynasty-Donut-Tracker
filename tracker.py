"""Build the CTESPN Dynasty zero-point starter leaderboard."""

import argparse
import csv
import gzip
import html
import io
import json
import re
import time
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
LEAGUE_ID = "1313707182407024640"
SEASON = 2026
SLEEPER = "https://api.sleeper.app/v1/"
NFLVERSE = "https://github.com/nflverse/nflverse-data/releases/download/"
SECOND_HALF = {"3", "4"}
SCRIMMAGE = {"run", "pass", "qb_kneel", "qb_spike"}
LIVE_PLAYS = SCRIMMAGE | {"punt", "field_goal", "extra_point", "kickoff"}
PLAYER_ID = re.compile(r"00-\d{7}")


def download(url, snapshot, filename, optional=False):
    if snapshot:
        path = snapshot / filename
        return path.read_bytes() if path.exists() or not optional else None
    for attempt in range(3):
        try:
            request = Request(url, headers={"User-Agent": "CTESPN-Dynasty-tracker"})
            with urlopen(request, timeout=30) as response:
                return response.read()
        except HTTPError as error:
            if optional and error.code == 404:
                return None
            if error.code < 500 and error.code != 429:
                raise
            if attempt == 2:
                raise
        except (URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(2 ** attempt)


def sleeper(endpoint, snapshot):
    return json.loads(download(SLEEPER + endpoint, snapshot, endpoint.replace("/", "_") + ".json"))


def nfl_csv(endpoint, filename, snapshot, optional=False):
    data = download(NFLVERSE + endpoint, snapshot, filename, optional)
    if data is None:
        return iter(())
    if filename.endswith(".gz"):
        stream = gzip.GzipFile(fileobj=io.BytesIO(data))
    else:
        stream = io.BytesIO(data)
    return csv.DictReader(io.TextIOWrapper(stream, encoding="utf-8-sig", newline=""))


def load_matchups(league, teams, snapshot):
    games = defaultdict(list)
    for row in nfl_csv("schedules/games.csv", "games.csv", snapshot):
        if row["season"] == str(SEASON) and row["game_type"] == "REG":
            games[int(row["week"])].append(row)
    settings = league["settings"]
    matchups = {}
    for week in range(settings.get("start_week", 1), min(settings["last_scored_leg"], 18) + 1):
        if not games[week] or any(not (game["home_score"] and game["away_score"]) for game in games[week]):
            raise ValueError(f"NFL schedule has missing results for Sleeper's completed Week {week}")
        rows = sleeper(f"league/{LEAGUE_ID}/matchups/{week}", snapshot)
        if sorted(row["roster_id"] for row in rows) != sorted(teams):
            raise ValueError(f"Incomplete Sleeper matchups for Week {week}")
        matchups[week] = rows
    return matchups, {
        game["game_id"]: game
        for week in matchups
        for game in games[week]
    }


def load_evidence(games, snapshot):
    identities = {}
    for row in nfl_csv(f"rosters/roster_{SEASON}.csv", "roster.csv", snapshot):
        if row["sleeper_id"] and row["gsis_id"]:
            identities[row["sleeper_id"]] = (row["gsis_id"], row["pfr_id"])

    snaps = defaultdict(list)
    team_totals = {}
    for row in nfl_csv(f"snap_counts/snap_counts_{SEASON}.csv", "snaps.csv", snapshot):
        game_id = row["game_id"]
        if game_id not in games:
            continue
        week = int(row["week"])
        snaps[week, row["pfr_player_id"]].append(row)
        count = int(row["offense_snaps"])
        # With percentages rounded to two decimals, a count below 199
        # can only display 1.00 if the player took every offensive snap.
        if row["offense_pct"] and Decimal(row["offense_pct"]) == 1 and 0 < count < 199:
            team_totals[game_id, row["team"]] = count

    game_plays = {}
    by_player = defaultdict(dict)
    second_half_offense = Counter()
    for row in nfl_csv(f"pbp/play_by_play_{SEASON}.csv.gz", "pbp.csv.gz", snapshot):
        game_id = row["game_id"]
        if game_id not in games or row["play_deleted"] == "1" or row["play_type"] not in LIVE_PLAYS:
            continue
        key = (game_id, row["play_id"])
        week = int(row["week"])
        if row["qtr"] in SECOND_HALF and key not in game_plays:
            game_plays[key] = row
            if row["play_type"] in SCRIMMAGE and row["special_teams_play"] != "1" and row["two_point_attempt"] != "1":
                team = "LAR" if row["posteam"] == "LA" else row["posteam"]
                second_half_offense[game_id, team] += 1
        for field, value in row.items():
            if field.endswith("_id") and field != "penalty_player_id" and PLAYER_ID.fullmatch(value or ""):
                by_player[week, value][key] = (row, "play-by-play")

    participation_path = f"pbp_participation/pbp_participation_{SEASON}.csv"
    for row in nfl_csv(participation_path, "participation.csv", snapshot, optional=True):
        key = (row["nflverse_game_id"], row["play_id"])
        play = game_plays.get(key)
        if play:
            for player_id in PLAYER_ID.findall(row["players_on_play"]):
                by_player[int(play["week"]), player_id].setdefault(key, (play, "FTN on-field charting"))
    return identities, snaps, team_totals, second_half_offense, by_player


def game_link(game):
    if game["espn"]:
        return "https://www.espn.com/nfl/playbyplay/_/gameId/" + game["espn"]
    return "https://www.nfl.com/schedules/" + game["season"] + "/by-week/week-" + game["week"]


def classify(week, player_id, player, score_verified, reviews, games, evidence):
    if player.get("position") == "DEF":
        return "Excluded", "D/ST is a team unit.", ""
    review = reviews.get(f"{week}:{player_id}")
    if review and review["status"] == "Excluded":
        return "Excluded", review["evidence"], review["source"]
    if not score_verified:
        return "Uncertain", "Sleeper shows 0.00 in the lineup slot without a matching player score.", ""
    if review:
        return review["status"], review["evidence"], review["source"]

    identities, snaps, team_totals, second_half_offense, by_player = evidence
    gsis, pfr = identities.get(player_id, (player.get("gsis_id"), player.get("pfr_id")))
    known_plays = by_player.get((week, gsis), {})
    for play, source in known_plays.values():
        if play["qtr"] in SECOND_HALF:
            label = play["play_type"].replace("_", " ")
            detail = f'Q{play["qtr"]} {play["time"]} {label} ({source})'
            return "Confirmed", detail, game_link(games[play["game_id"]])

    for row in snaps.get((week, pfr), []):
        game_id, team = row["game_id"], row["team"]
        team = "LAR" if team == "LA" else team
        key = (game_id, team)
        offense = int(row["offense_snaps"])
        total = team_totals.get(key)
        second_half = second_half_offense[key]
        if total is not None and 0 < second_half <= total and total - second_half < offense <= total:
            detail = f"{offense} offensive snaps. At most {total - second_half} could have been outside Q3–Q4."
            return "Confirmed", detail, game_link(games[game_id])

    rows = snaps.get((week, pfr), [])
    if len(rows) == 1:
        row = rows[0]
        game_id = row["game_id"]
        count = sum(int(row[key]) for key in ("offense_snaps", "defense_snaps", "st_snaps"))
        first_half = sum(
            play["qtr"] in {"1", "2"} and play["game_id"] == game_id
            for play, source in known_plays.values()
        )
        if count > 0 and first_half == count:
            detail = "The only recorded snap was in Q1–Q2." if count == 1 else f"All {count} recorded snaps were in Q1–Q2."
            return "Excluded", detail, game_link(games[game_id])
        return "Uncertain", f"{count} game snaps. Quarter of participation remains unverified.", game_link(games[game_id])
    return "Uncertain", "No verified Q3–Q4 participation or game snap record.", ""


def audit(snapshot):
    league = sleeper(f"league/{LEAGUE_ID}", snapshot)
    if league["season"] != str(SEASON) or league["sport"] != "nfl":
        raise ValueError(f"Expected a {SEASON} NFL league")
    users = {user["user_id"]: user for user in sleeper(f"league/{LEAGUE_ID}/users", snapshot)}
    teams = {}
    for roster in sleeper(f"league/{LEAGUE_ID}/rosters", snapshot):
        roster_id = roster["roster_id"]
        metadata = users.get(roster["owner_id"], {}).get("metadata") or {}
        teams[roster_id] = metadata.get("team_name") or f"Team {roster_id}"
    if len(teams) != league["total_rosters"]:
        raise ValueError("Sleeper roster data is incomplete")
    matchups, games = load_matchups(league, teams, snapshot)
    if not matchups:
        return teams, {}, []
    evidence = load_evidence(games, snapshot)
    players = sleeper("players/nfl", snapshot)
    reviews = json.loads((ROOT / "reviews.json").read_text(encoding="utf-8"))
    entries = []
    for week, rows in matchups.items():
        for row in rows:
            roster_id = row["roster_id"]
            seen = set()
            starters, points_by_slot = row["starters"], row["starters_points"]
            if len(starters) != len(points_by_slot):
                raise ValueError(f"Starter scores are missing for roster {roster_id} in Week {week}")
            for player_id, points in zip(starters, points_by_slot):
                if player_id in (None, "", "0") or player_id in seen or points != 0:
                    continue
                seen.add(player_id)
                player = players.get(player_id, {})
                name = player.get("full_name") or player.get("last_name") or f"Player {player_id}"
                score_verified = (row.get("players_points") or {}).get(player_id) == 0
                status, detail, source = classify(week, player_id, player, score_verified, reviews, games, evidence)
                entries.append((week, roster_id, name, status, detail, source))
    return teams, matchups, entries


def cell(value):
    return html.escape(str(value).replace("\n", " ").replace("\r", " "), quote=False).replace("|", "&#124;").replace("[", "&#91;").replace("]", "&#93;")


def render(teams, matchups, entries):
    counts = Counter(row[1] for row in entries if row[3] == "Confirmed")
    pending = Counter(row[1] for row in entries if row[3] == "Uncertain")
    team_order = sorted(teams, key=lambda roster: (-counts[roster], teams[roster].casefold(), roster))
    entries.sort(key=lambda row: (row[0], teams[row[1]].casefold(), row[2]))
    last_week = max(matchups, default=0)
    report = [
        "# CTESPN Dynasty Donut Tracker",
        "",
        f"**{SEASON} season · Through Week {last_week}**" if last_week else f"**{SEASON} season · Waiting for Week 1**",
        "",
        f"**{sum(counts.values())} confirmed starts** · **{sum(pending.values())} to verify**",
        "",
        "## Leaderboard",
        "",
        "| Rank | Fantasy team | Confirmed starts | To verify |",
        "| ---: | --- | ---: | ---: |",
    ]
    for position, roster in enumerate(team_order, 1):
        if position == 1 or counts[roster] != counts[team_order[position - 2]]:
            rank = position
        report.append(f"| {rank} | {cell(teams[roster])} | {counts[roster]} | {pending[roster]} |")
    report.extend(["", "## Weekly log", "", "| Week | Confirmed | To verify | Excluded |", "| ---: | ---: | ---: | ---: |"])
    totals = Counter((row[0], row[3]) for row in entries)
    for week in matchups:
        report.append(f"| {week} | {totals[week, 'Confirmed']} | {totals[week, 'Uncertain']} | {totals[week, 'Excluded']} |")
    for status, heading in (("Confirmed", "Confirmed starts"), ("Uncertain", "Cases to verify"), ("Excluded", "Reviewed exclusions")):
        report.extend(["", f"### {heading}", ""])
        selected = [row for row in entries if row[3] == status]
        if not selected:
            report.append("None.")
            continue
        report.extend(["| Week | Fantasy team | Player | Evidence |", "| ---: | --- | --- | --- |"])
        for week, roster, name, _, detail, source in selected:
            citation = f" [Source]({source})" if source else ""
            report.append(f"| {week} | {cell(teams[roster])} | {cell(name)} | {cell(detail)}{citation} |")
    report.extend([
        "",
        "## How the tracker works",
        "",
        "Each entry is one individual starter with exactly 0.00 points in both Sleeper scoring fields and evidence of a Q3 or Q4 snap. Special-teams plays count. Overtime, empty slots, and team D/ST slots are excluded. Ranks reflect confirmed entries and ties share a rank.",
        "",
        "Confirmation comes from an identified play, on-field charting when available, or a snap total that exceeds the maximum possible outside Q3–Q4. A full-game snap count alone can leave a case open. Sourced manual decisions are in `reviews.json`, keyed by week and Sleeper player ID.",
        "",
        "GitHub Actions checks daily for completed weeks and stat corrections. Run **Refresh leaderboard** from the Actions tab to refresh on demand, or run `python3 tracker.py` locally with Python 3.9 or later.",
        "",
        "Sources: [Sleeper](https://docs.sleeper.com/), [nflverse play-by-play](https://nflreadr.nflverse.com/reference/load_pbp.html), [Pro Football Reference snap counts via nflverse](https://nflreadr.nflverse.com/reference/load_snap_counts.html), and [FTN Data on-field charting via nflverse](https://nflreadr.nflverse.com/reference/load_participation.html) ([CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/)).",
        "",
    ])
    return "\n".join(report)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, help="Read downloaded input files from this folder.")
    args = parser.parse_args()
    report = render(*audit(args.snapshot))
    temporary = ROOT / "README.md.tmp"
    temporary.write_text(report, encoding="utf-8")
    temporary.replace(ROOT / "README.md")
