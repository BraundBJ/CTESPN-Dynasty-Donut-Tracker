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
    teams_by_week = {
        week: {BOOK_TEAMS.get(game[field], game[field]) for game in schedule for field in ("home_team", "away_team")}
        for week, schedule in games.items()
    }
    nfl_teams = set().union(*teams_by_week.values())
    bye_teams = {week: nfl_teams - teams_by_week[week] for week in matchups if teams_by_week[week] != nfl_teams}
    return matchups, {
        game["game_id"]: game
        for week in matchups
        for game in games[week]
    }, bye_teams


def load_bye_players(bye_teams, players, snapshot):
    if not bye_teams:
        return set()
    history = defaultdict(lambda: defaultdict(set))
    for row in nfl_csv(f"weekly_rosters/roster_weekly_{SEASON}.csv", "weekly_roster.csv", snapshot):
        if row["game_type"] != "REG" or not row["sleeper_id"] or not row["week"]:
            continue
        team = row["team"] if row["status"] in {"ACT", "INA", "RES", "DEV", "EXE"} else None
        history[row["sleeper_id"]][int(row["week"])].add(BOOK_TEAMS.get(team, team))

    bye_players = set()
    for player_id, roster in history.items():
        for week, byes in bye_teams.items():
            teams = roster.get(week)
            if teams is None:
                # Bye teams can be missing from weekly rosters. Use the
                # adjacent records only when they agree on the player's team.
                previous = max((number for number in roster if number < week), default=None)
                if previous is None:
                    continue
                following = min((number for number in roster if number > week), default=None)
                current = players.get(player_id, {}).get("team")
                next_teams = roster[following] if following else {BOOK_TEAMS.get(current, current)}
                if roster[previous] != next_teams:
                    continue
                teams = next_teams
            if len(teams) == 1 and teams <= byes:
                bye_players.add((week, player_id))
    return bye_players


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


def counts_as_donut(week, player_id, player, data, early_injuries, bye_players, snapshot):
    if player.get("position") == "DEF":
        return False
    identities, snaps, played, later, books = data
    identity = identities.get(player_id, player)
    player_key = (week, identity.get("gsis_id"))
    snap_games = snaps.get((week, identity.get("pfr_id")), {})
    games_played = snap_games.keys() | played.get(player_key, set())
    if not games_played:
        return (week, player_id) in bye_players
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
    matchups, games, bye_teams = load_matchups(league, teams, snapshot)
    if not matchups:
        return teams, {}, []
    appearances = load_appearances(games, snapshot)
    players = sleeper("players/nfl", snapshot)
    bye_players = load_bye_players(bye_teams, players, snapshot)
    early_injuries = json.loads((ROOT / "early_injuries.json").read_text(encoding="utf-8"))
    entries = []
    for week, rows in matchups.items():
        for row in rows:
            roster_id = row["roster_id"]
            seen = set()
            player_points = row.get("players_points") or {}
            starters, points_by_slot = row["starters"], row["starters_points"]
            if len(starters) != len(points_by_slot):
                raise ValueError(f"Starter scores are missing for roster {roster_id} in Week {week}")
            for player_id, points in zip(starters, points_by_slot):
                if player_id in (None, "", "0") or player_id in seen or points != 0:
                    continue
                # Require a matching player score. A zero attached to the
                # lineup slot alone does not establish who earned it.
                if player_points.get(player_id) != 0:
                    continue
                seen.add(player_id)
                player = players.get(player_id, {})
                name = player.get("full_name") or player.get("last_name") or f"Player {player_id}"
                if counts_as_donut(week, player_id, player, appearances, early_injuries, bye_players, snapshot):
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


def render_podium(standings):
    podium = []
    trophy = (
        '<svg class="trophy" viewBox="0 0 80 80" fill="none" aria-hidden="true">'
        '<defs><linearGradient id="cup-gold" x1="24" y1="17" x2="57" y2="66" gradientUnits="userSpaceOnUse">'
        '<stop stop-color="#fff2b6"/><stop offset=".5" stop-color="#f4bb56"/>'
        '<stop offset="1" stop-color="#b57227"/></linearGradient></defs>'
        '<circle cx="40" cy="40" r="35" fill="#291e19" stroke="#75532d"/>'
        '<path d="M25 24H16v5c0 8 5 13 13 14m26-19h9v5c0 8-5 13-13 14" '
        'stroke="#efb65c" stroke-width="3" stroke-linejoin="round"/>'
        '<path d="M24 17h32v17c0 9-7 17-16 17s-16-8-16-17V17Zm12 33h8v10h8v7H28v-7h8V50Z" '
        'fill="url(#cup-gold)"/>'
        '<path d="M30 23v10c0 5 2 9 6 12" stroke="#fff5d6" stroke-opacity=".75" stroke-width="2" stroke-linecap="round"/>'
        '<path d="m13 12 1.5 4 4 1.5-4 1.5-1.5 4-1.5-4-4-1.5 4-1.5L13 12Zm54 35 1.5 4 4 1.5-4 1.5-1.5 4-1.5-4-4-1.5 4-1.5 1.5-4Z" '
        'fill="#f8c976"/></svg>'
    )
    for place, label in ((1, "1st"), (2, "2nd"), (3, "3rd")):
        teams = [(name, count) for rank, name, count, _ in standings if rank == place and count]
        if not teams:
            continue
        count = teams[0][1]
        if len(teams) == 1:
            names = f'<p class="podium-name">{html.escape(teams[0][0])}</p>'
        else:
            tied_names = "".join(f"<li>{html.escape(name)}</li>" for name, _ in teams)
            names = f'<details class="podium-tie"><summary>{len(teams)} teams tied</summary><ul>{tied_names}</ul></details>'
        podium.append(
            f'<div class="podium-place place-{place}">'
            f'{trophy if place == 1 else ""}{names}'
            f'<div class="podium-step"><strong>{label}</strong>'
            f'<span>{count} {"donut" if count == 1 else "donuts"}</span></div></div>'
        )
    return "\n".join(podium)


def load_video_submissions():
    path = ROOT / "video_submissions.json"
    if not path.exists():
        return {"missing": [], "links": [], "special": []}
    return json.loads(path.read_text(encoding="utf-8"))


def render_video_log(weeks, standings, config):
    missing = {(item["team"], item["player"]) for item in config.get("missing", [])}
    links = {(item["team"], item["player"]): item["url"] for item in config.get("links", [])}
    collection = config.get("collection_url", "")
    entries = []
    for _, team, _, weekly in standings:
        for week, players in zip(weeks, weekly):
            for player in players:
                entries.append((week, team, player, (team, player) in missing, False))
    for item in config.get("special", []):
        entries.append((item["week"], item["team"], item["player"], False, True))
        links[item["team"], item["player"]] = item["url"]

    cards = []
    for week, team, player, is_missing, is_special in sorted(
        entries, key=lambda item: (-item[0], item[1].casefold(), item[2].casefold())
    ):
        classes = ["video-card"]
        if is_missing:
            classes.append("video-missing-card")
        if is_special:
            classes.append("video-special-card")
        if is_missing:
            action = '<span class="video-status video-status-missing">Missing</span>'
        else:
            url = links.get((team, player), collection)
            action = (
                f'<a class="video-status" href="{html.escape(url, quote=True)}" '
                'target="_blank" rel="noopener">Watch</a>'
            ) if url else '<span class="video-status">Submitted</span>'
        cards.append(
            f'<li class="{" ".join(classes)}">'
            f'<span class="video-week">Week {week}</span>'
            f'<strong>{html.escape(player)}</strong>'
            f'<span class="video-team">{html.escape(team)}</span>{action}</li>'
        )

    missing_count = sum(is_missing for _, _, _, is_missing, _ in entries)
    submitted_count = len(entries) - missing_count
    metrics = (
        f'<div><strong>{submitted_count}</strong><span>Submitted</span></div>'
        f'<div class="video-missing-metric"><strong>{missing_count}</strong><span>Missing</span></div>'
    )
    collection_link = (
        f'<a class="collection-link" href="{html.escape(collection, quote=True)}" '
        'target="_blank" rel="noopener">All videos</a>'
    ) if collection else ""
    return metrics, "".join(cards), collection_link, missing


def render_page(weeks, standings, total):
    escape = html.escape
    video_config = load_video_submissions()
    video_metrics, video_log, video_collection, missing_videos = render_video_log(
        weeks, standings, video_config
    )
    overdue_teams = {team for team, _ in missing_videos}
    table_rows = []
    for rank, team, count, weekly in standings:
        row_classes = []
        if rank == 1 and count:
            row_classes.append("leader")
        if team in overdue_teams:
            row_classes.append("video-overdue")
        row_class = f' class="{" ".join(row_classes)}"' if row_classes else ""
        cells = [
            f'<th scope="row"><span class="rank" aria-label="Rank {rank}">{rank:02d}</span> '
            f'<span class="team-name">{escape(team)}</span></th>',
            f'<td class="score">{count}</td>',
        ]
        for players in weekly:
            if players:
                player_items = []
                for name in players:
                    missing_attributes = (
                        ' class="video-missing" title="Video missing"'
                        if (team, name) in missing_videos else ""
                    )
                    player_items.append(
                        f'<li data-player="{escape(name, quote=True)}"{missing_attributes}>'
                        f'{escape(name)}</li>'
                    )
                names = "".join(player_items)
                cells.append(f'<td class="has-donut"><ul>{names}</ul></td>')
            else:
                cells.append('<td class="no-donut">—</td>')
        table_rows.append(f"<tr{row_class}>" + "".join(cells) + "</tr>")

    template = Template((ROOT / "leaderboard.html.template").read_text(encoding="utf-8"))
    podium = render_podium(standings)
    return template.substitute(
        season=SEASON,
        coverage=f"Through Week {weeks[-1]}" if weeks else "Waiting for Week 1",
        total=total,
        teams=len(standings),
        podium=podium,
        podium_hidden="" if podium else " hidden",
        week_headers="".join(f'<th scope="col">Week {week}</th>' for week in weeks),
        leaderboard="\n".join(table_rows),
        video_metrics=video_metrics,
        video_log=video_log,
        video_collection=video_collection,
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
