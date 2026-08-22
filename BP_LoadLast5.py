import requests
from datetime import datetime, timedelta
import pandas as pd
import time
from sqlalchemy import MetaData, Table, insert, create_engine, text
import os
from dotenv import load_dotenv
from tqdm import tqdm

tqdm.pandas()
load_dotenv()

# SQLAlchemy
engine = create_engine(os.environ["SQLA_CONN_STRING_MLB"])
metadata = MetaData()

todays_date = datetime.now().strftime("%Y-%m-%d")

mlb_team_ids = {
    'Arizona Diamondbacks': 109,
    'Atlanta Braves': 144,
    'Baltimore Orioles': 110,
    'Boston Red Sox': 111,
    'Chicago Cubs': 112,
    'Chicago White Sox': 145,
    'Cincinnati Reds': 113,
    'Cleveland Guardians': 114,
    'Colorado Rockies': 115,
    'Detroit Tigers': 116,
    'Houston Astros': 117,
    'Kansas City Royals': 118,
    'Los Angeles Angels': 108,
    'Los Angeles Dodgers': 119,
    'Miami Marlins': 146,
    'Milwaukee Brewers': 158,
    'Minnesota Twins': 142,
    'New York Mets': 121,
    'New York Yankees': 147,
    'Athletics': 133,
    'Philadelphia Phillies': 143,
    'Pittsburgh Pirates': 134,
    'San Diego Padres': 135,
    'San Francisco Giants': 137,
    'Seattle Mariners': 136,
    'St. Louis Cardinals': 138,
    'Tampa Bay Rays': 139,
    'Texas Rangers': 140,
    'Toronto Blue Jays': 141,
    'Washington Nationals': 120
}

def get_mlb_games(date_string=None):
    """
    Fetches MLB games for a specific date and returns a list of game IDs.
        print(pd.DataFrame(game_ids))    :param date_string: Date formatted as 'YYYY-MM-DD'
    :return: List of game IDs (gamePk values)
    """
    # Base URL for the official MLB schedule endpoint
    url = "https://statsapi.mlb.com/api/v1/schedule"

    # If no as_of_date provided, use today's date
    if not date_string:
        date_string = todays_date

    # Query parameters: sportId=1 specifies Major League Baseball
    params = {
        "sportId": 1,
        "date": date_string
    }

    try:
        # Send the GET request to the API
        response = requests.get(url, params=params)

        # Raise an exception if the request failed (e.g., 404, 500)
        response.raise_for_status()

        # Parse the JSON response
        data = response.json()

        # Check if there are any dates returned in the schedule
        if not data.get("dates"):
            print(f"No games found or scheduled for {date_string}.")
            return []

        # Extract the list of games from the first date entry
        games_list = data["dates"][0].get("games", [])

        # Collect game IDs
        game_ids = []

        # Loop through each game and extract game IDs
        for game in games_list:
            away_team = game["teams"]["away"]["team"]["name"]
            home_team = game["teams"]["home"]["team"]["name"]
            game_status = game["status"]["detailedState"]
            game_id = game["gamePk"]

            # Add game ID to the list

            game_ids.append((game_id, mlb_team_ids[home_team], mlb_team_ids[away_team]))

        return game_ids

    except requests.exceptions.RequestException as e:
        print(f"An error occurred while fetching data: {e}")
        return []

def get_pitcher_last_5_v1_1(player_id, team_id, season=2026, as_of_date=None):
    time.sleep(1)
    # 1. Fetch the team schedule using v1 to get recent game IDs (gamePks)
    schedule_url = "https://statsapi.mlb.com/api/v1/schedule"
    schedule_params = {
        "teamId": team_id,
        "sportId": 1,
        "season": season,
        "gameType": "R"  # Regular season
    }

    sched_resp = requests.get(schedule_url, params=schedule_params)
    if sched_resp.status_code != 200:
        return "Error fetching schedule."

    # If no as_of_date provided, use today's date
    if not as_of_date:
        as_of_date = datetime.now().strftime("%Y-%m-%d")

    # Calculate the last 5 days (including as_of_date)
    base_date = datetime.strptime(as_of_date, "%Y-%m-%d")
    last_5_dates = [(base_date - timedelta(days=i + 1)).strftime("%Y-%m-%d") for i in range(5)]

    # Initialize pitches for each of the last 5 days
    pitches_by_date = {date: 0 for date in last_5_dates}

    # Store season stats (will be updated with most recent game)
    season_games_pitched = 0
    season_era = 0
    season_whip = 0
    season_innings = "0.0"
    season_games_started = 0

    # Gather all completed games up to the as_of_date
    games_to_check = []
    for date_obj in sched_resp.json().get("dates", []):
        game_date = date_obj.get("date", "")

        # Only include games within the last 5 days
        if game_date in last_5_dates:
            for game in date_obj.get("games", []):
                # Only count games that have actually been played
                if game.get("status", {}).get("abstractGameState") == "Final":
                    games_to_check.append((game["gamePk"], game_date))

    str_player_id = f"ID{player_id}"

    # 2. Loop through the games and call the v1.1 Boxscore API
    for game_pk, game_date in games_to_check:
        time.sleep(1)
        boxscore_url = f"https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live"
        box_resp = requests.get(boxscore_url)

        if box_resp.status_code != 200:
            continue

        box_data = box_resp.json()
        # The v1.1 API structure has players under liveData.boxscore.teams
        live_data = box_data.get("liveData", {})
        boxscore = live_data.get("boxscore", {})
        teams = boxscore.get("teams", {})

        # Check both home and away teams for our pitcher
        for team_side in ["home", "away"]:
            players = teams.get(team_side, {}).get("players", {})

            if str_player_id in players:
                player_data = players[str_player_id]

                # Check if the player actually pitched in this game
                if "pitching" in player_data.get("stats", {}) and player_data["stats"]["pitching"]:
                    pitching_stats = player_data["stats"]["pitching"]

                    # Verify they recorded at least some game activity
                    if pitching_stats.get("gamesPlayed", 0) > 0 or pitching_stats.get("inningsPitched", "0.0") != "0.0":
                        # Get number of pitches thrown (could be 'numberOfPitches' or 'pitchesThrown')
                        pitches = pitching_stats.get("numberOfPitches", pitching_stats.get("pitchesThrown", 0))

                        # Add pitches to the corresponding date
                        pitches_by_date[game_date] += pitches

                        # Get season stats from this game (use most recent game's season stats)
                        season_stats = player_data.get("seasonStats", {}).get("pitching", {})
                        season_games_pitched = season_stats.get("gamesPitched", 0)
                        season_era = season_stats.get("era", "0.00")
                        season_whip = season_stats.get("whip", "0.00")
                        season_innings = season_stats.get("inningsPitched", "0.0")
                        season_games_started = season_stats.get("gamesStarted", 0)

                        break  # Found the player, move to next game

    # Return list: [day1, day2, day3, day4, day5, gamesPitched, era, whip]
    pitches_list = [pitches_by_date[date] for date in last_5_dates] + [season_games_pitched, season_era, season_whip,
                                                                       season_innings, season_games_started]
    return pitches_list

def availability(day1, day2, day3, day4, day5, gamesPitched, gamesStarted):
    if day1 > 35:
        return False
    elif day1 > 0 and day2 > 0 and (day1+day2) > 20:
        return False
    elif day1 > 0 and (day2 > 0 or day3 > 0) and day4 > 0:
        return False
    elif day1 > 75 or day2 > 75 or day3 > 75 or day4 > 75 or day5 > 75:
        return False
    elif (gamesPitched == gamesStarted) and (gamesPitched > 0) and (gamesStarted > 0):
        return False
    else:
        return True

# 2. Establish PostgreSQL database engine connection
# Format: postgresql://username:password@host:port/database_name
engine = create_engine(os.environ['SQLA_CONN_STRING_MLB'])

def existing_player_ids(date):
    """player_ids already stored in bplast5 for this date.

    Queried once per game so the MLB stats API is only hit for pitchers we have
    no row for yet. Re-queried per game on purpose: a doubleheader repeats the
    same bullpen, and the first game's inserts are committed by then."""
    with engine.connect() as connection:
        query = text("SELECT player_id FROM bplast5 WHERE date = CAST(:date AS date)")
        result = connection.execute(query, {"date": date})

        return {row[0] for row in result}

def addToDB(player_id, date, name, team_id, day1, day2, day3, day4, day5, gamesPitched, era,
            whip, inningsPitched, gamesStarted, availability):

    # 2. Open a connection and execute the query
    with engine.connect() as connection:
        query = text(f"SELECT player_id, date FROM bplast5 WHERE player_id = {player_id} and date = '{date}'")

        # Execute with safely bound parameters to prevent SQL injection
        result = connection.execute(query, {"status_param": "active"})

        if len(pd.DataFrame(result)) == 0:
            table = Table('bplast5', metadata, autoload_with=engine)
            stmt = insert(table).values(player_id=player_id,
                                        date=date,
                                        name=name,
                                        team_id = team_id,
                                        day1 = day1,
                                        day2 = day2,
                                        day3 = day3,
                                        day4 = day4,
                                        day5 = day5,
                                        gamesPitched = gamesPitched,
                                        era = era,
                                        whip = whip,
                                        inningsPitched = inningsPitched,
                                        gamesStarted = gamesStarted,
                                        availability = availability
                                        )
            with engine.connect() as conn:
                conn.execute(stmt)
                conn.commit()

def getBullpenData(game_pk):
    """Get team lineup, starting pitcher, relief pitchers, and result for a game.
    Fixed to identify starting pitcher by who actually pitched the most innings in THIS game."""
    time.sleep(1)

    # Construct the official MLB GUMBO live feed URL
    url = f"https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live"

    response = requests.get(url)
    if response.status_code != 200:
        print(f"Error: Unable to fetch data (Status code: {response.status_code})")
        return None

    data = response.json()

    # Extract players master dictionary and boxscore data
    game_data = data.get("gameData", {})

    # Extract game date
    datetime_info = game_data.get("datetime", {})
    game_date = datetime_info.get("officialDate", "")

    live_data = data.get("liveData", {})
    boxscore = live_data.get("boxscore", {})
    boxscore_teams = boxscore.get("teams", {})

    players_dict = game_data.get("players", {})

    lineups = []
    startingPitchers = {}
    reliefPitchers = {}
    team_ids = {}
    df_list = []

    stored_ids = existing_player_ids(todays_date)

    for side in ["home", "away"]:
        team_data = boxscore_teams.get(side, {})
        team_info = team_data.get("team", {})
        team_name = team_info.get("name", f"Unknown {side.capitalize()}")
        team_id = team_info.get("id")
        team_ids[team_name] = team_id

        # Get all pitcher IDs (those who pitched and those in bullpen)
        pitchers_ids = team_data.get("pitchers", [])
        bullpen_ids = team_data.get("bullpen", [])

        # Only fetch from the stats API for pitchers not already stored for today
        new_ids = [player_id for player_id in bullpen_ids if player_id not in stored_ids]
        if not new_ids:
            print(f"{team_name}: all {len(bullpen_ids)} bullpen arms already stored for {todays_date}")
            continue

        df = pd.DataFrame(new_ids, columns=['player_id'])
        df['date'] = todays_date
        df['name'] = df.apply(lambda x: players_dict[f'ID{x.player_id}']['fullName'], axis=1)
        df['team_id'] = team_id
        df[['day1', 'day2', 'day3', 'day4', 'day5', 'gamesPitched', 'era', 'whip', 'inningsPitched',
            'gamesStarted']] = df.apply(lambda x: get_pitcher_last_5_v1_1(x.player_id, team_id, 2026), axis=1,
                                        result_type='expand')
        df = df.astype({'inningsPitched': float, 'gamesPitched': int, 'gamesStarted': int})
        df['availability'] = df.apply(
            lambda x: availability(x.day1, x.day2, x.day3, x.day4, x.day5, x.gamesPitched, x.gamesStarted), axis=1)
        df_list.append(df)

    if not df_list:
        return None

    return pd.concat(df_list, ignore_index=True)

def update():

    for game, home, away in tqdm(get_mlb_games()):
        df_game = getBullpenData(game)

        # Nothing new to store for this game, or the feed was unavailable
        if df_game is None or df_game.empty:
            continue

        df_game = df_game.apply(lambda x: addToDB(x.player_id, x['date'], x['name'], x.team_id,
                                        x.day1, x.day2, x.day3, x.day4, x.day5, x.gamesPitched, x.era,
                                        x.whip, x.inningsPitched, x.gamesStarted, x.availability), axis=1)
        #df_list.append(df_game)

#df = pd.concat(df_list, ignore_index=True)

update()

