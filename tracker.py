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
from functools import lru_cache
from pathlib import Path
from string import Template
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
LEAGUE_ID = "1313707182407024640"
SEASON = 2026
SLEEPER = "https://api.sleeper.app/v1/"
NFLVERSE = "https://github.com/nflverse/nflverse-data/releases/download/"
AFTER_HALFTIME = {"3", "4", "5"}
GAMEBOOKS = "https://static.www.nfl.com/image/upload/gamecenter/"
QUARTERS = {"First": 1, "Second": 2, "Third": 3, "Fourth": 4, "Overtime": 5}
BOOK_TEAMS = {"ARZ": "ARI", "BLT": "BAL", "CLV": "CLE", "HST": "HOU", "LA": "LAR"}
INJURY_EVENT = re.compile(
    r"\b([A-Z]{2,3})-(?:\d{1,2}-)?([\w]+)\.([\w.'-]+?) "
    r"(was injured during the play|return is (?:Questionable|Doubtful)|has returned to the game|is Out of the game)\b",
    re.IGNORECASE,
)
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


def load_appearances(games, snapshot):
    identities = {}
    for row in nfl_csv(f"rosters/roster_{SEASON}.csv", "roster.csv", snapshot):
        if row["sleeper_id"] and row["gsis_id"]:
            identities[row["sleeper_id"]] = row

    snaps = defaultdict(dict)
    for row in nfl_csv(f"snap_counts/snap_counts_{SEASON}.csv", "snaps.csv", snapshot):
        if row["game_id"] in games and row["pfr_player_id"] and any(
            int(row[field]) > 0 for field in ("offense_snaps", "defense_snaps", "st_snaps")
        ):
            snaps[int(row["week"]), row["pfr_player_id"]][row["game_id"]] = row["team"]

    played = defaultdict(set)
    later = defaultdict(set)
    books = {}
    for row in nfl_csv(f"pbp/play_by_play_{SEASON}.csv.gz", "pbp.csv.gz", snapshot):
        game_id = row["game_id"]
        if game_id not in games:
            continue
        if row["nfl_api_id"]:
            books[game_id] = row["nfl_api_id"]
        if row["play_deleted"] == "1" or row["play_type"] not in LIVE_PLAYS:
            continue
        for field, value in row.items():
            if field.endswith("_id") and field != "penalty_player_id" and PLAYER_ID.fullmatch(value or ""):
                key = (int(row["week"]), value)
                played[key].add(game_id)
                if row["qtr"] in AFTER_HALFTIME:
                    later[key].add(game_id)
    return identities, snaps, played, later, books


@lru_cache(maxsize=None)
def injury_events(game_id, book_id, snapshot):
    if not book_id:
        return ()
    data = download(f"{GAMEBOOKS}{book_id}.pdf", snapshot, f"{game_id}.pdf", optional=True)
    if data is None:
        return ()

    from pypdf import PdfReader

    events = []
    quarter = 0
    for page in PdfReader(io.BytesIO(data)).pages:
        for line in page.extract_text(extraction_mode="layout").splitlines():
            heading = re.search(r"Play By Play\s+(First|Second|Third|Fourth|Overtime)", line)
            if heading:
                quarter = QUARTERS[heading[1]]
            if quarter:
                for team, first, last, event in INJURY_EVENT.findall(line):
                    events.append((BOOK_TEAMS.get(team, team), first, last, event.lower(), quarter))
    return events


def name_key(name):
    return "".join(character for character in name.casefold() if character.isalnum())


def injury_ended_game(events, team, identity):
    first = name_key(identity.get("football_name") or identity.get("first_name") or "")
    last = name_key(re.sub(r"\s+(?:Jr\.?|Sr\.?|II|III|IV)$", "", identity.get("last_name") or ""))
    team = BOOK_TEAMS.get(team, team)
    hurt_before_half = False
    out_before_second_half = False
    for club, initial, surname, event, quarter in events:
        if club != team or not first.startswith(name_key(initial)) or name_key(surname) != last:
            continue
        if event == "has returned to the game":
            hurt_before_half = out_before_second_half = False
        elif event == "is out of the game":
            out_before_second_half |= quarter <= 2 or hurt_before_half
        elif quarter <= 2:
            hurt_before_half = True
    return out_before_second_half


def counts_as_donut(week, player_id, player, data, early_injuries, snapshot):
    if player.get("position") == "DEF":
        return False
    identities, snaps, played, later, books = data
    identity = identities.get(player_id, player)
    player_key = (week, identity.get("gsis_id"))
    snap_games = snaps.get((week, identity.get("pfr_id")), {})
    games_played = snap_games.keys() | played.get(player_key, set())
    if not games_played:
        return False
    if later.get(player_key):
        return True
    if player_id in early_injuries.get(str(week), ()):
        return False
    return any(
        not injury_ended_game(
            injury_events(game_id, books.get(game_id), snapshot),
            snap_games.get(game_id, identity.get("team")),
            identity,
        )
        for game_id in sorted(games_played)
    )


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
    appearances = load_appearances(games, snapshot)
    players = sleeper("players/nfl", snapshot)
    early_injuries = json.loads((ROOT / "early_injuries.json").read_text(encoding="utf-8"))
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
                if (row.get("players_points") or {}).get(player_id) == 0 and counts_as_donut(
                    week, player_id, player, appearances, early_injuries, snapshot
                ):
                    entries.append((week, roster_id, name))
    return teams, matchups, entries


def cell(value):
    return html.escape(str(value).replace("\n", " ").replace("\r", " "), quote=False).replace("|", "&#124;").replace("[", "&#91;").replace("]", "&#93;")


def standings(teams, matchups, entries):
    by_week = defaultdict(list)
    counts = Counter()
    for week, roster, player in entries:
        by_week[roster, week].append(player)
        counts[roster] += 1

    weeks = sorted(matchups)
    team_order = sorted(teams, key=lambda roster: (-counts[roster], teams[roster].casefold(), roster))
    rows = []
    for position, roster in enumerate(team_order, 1):
        if position == 1 or counts[roster] != counts[team_order[position - 2]]:
            rank = position
        weekly = [sorted(by_week[roster, week], key=str.casefold) for week in weeks]
        rows.append((rank, teams[roster], counts[roster], weekly))
    return weeks, rows


def render(weeks, standings, total):
    coverage = f"Through Week {weeks[-1]}" if weeks else "Waiting for Week 1"
    headings = ["Rank", "Fantasy team", "Donuts", *(f"W{week}" for week in weeks)]
    report = [
        "# CTESPN Dynasty Donut Tracker",
        "",
        "[Open the Donut Tracker](https://braundbj.github.io/CTESPN-Dynasty-Donut-Tracker/)",
        "",
        f"**{SEASON} season · {coverage} · {total} donuts**",
        "",
        "| " + " | ".join(headings) + " |",
        "| " + " | ".join(["---:", "---", "---:", *["---"] * len(weeks)]) + " |",
    ]
    for rank, team, count, weekly in standings:
        row = [str(rank), cell(team), str(count), *[", ".join(map(cell, players)) or "—" for players in weekly]]
        report.append("| " + " | ".join(row) + " |")
    return "\n".join(report) + "\n"


def render_page(weeks, standings, total):
    escape = html.escape
    table_rows = []
    for rank, team, count, weekly in standings:
        leader = ' class="leader"' if rank == 1 and count else ""
        cells = [
            f'<th scope="row"><span class="rank" aria-label="Rank {rank}">{rank:02d}</span> {escape(team)}</th>',
            f'<td class="score">{count}</td>',
        ]
        for players in weekly:
            if players:
                names = "".join(f"<li>{escape(name)}</li>" for name in players)
                cells.append(f'<td class="has-donut"><ul>{names}</ul></td>')
            else:
                cells.append('<td class="no-donut">—</td>')
        table_rows.append(f"<tr{leader}>" + "".join(cells) + "</tr>")

    template = Template((ROOT / "leaderboard.html.template").read_text(encoding="utf-8"))
    return template.substitute(
        season=SEASON,
        coverage=f"Through Week {weeks[-1]}" if weeks else "Waiting for Week 1",
        total=total,
        teams=len(standings),
        week_headers="".join(f'<th scope="col">Week {week}</th>' for week in weeks),
        leaderboard="\n".join(table_rows),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, help="Read downloaded input files from this folder.")
    parser.add_argument("--site", type=Path, help="Write the standalone web leaderboard to this directory.")
    args = parser.parse_args()
    teams, matchups, entries = audit(args.snapshot)
    weeks, rows = standings(teams, matchups, entries)
    report = render(weeks, rows, len(entries))
    if args.site:
        args.site.mkdir(parents=True, exist_ok=True)
        (args.site / "index.html").write_text(render_page(weeks, rows, len(entries)), encoding="utf-8")
    temporary = ROOT / "README.md.tmp"
    temporary.write_text(report, encoding="utf-8")
    temporary.replace(ROOT / "README.md")
