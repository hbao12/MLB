import requests
import time
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import boto3

# Get current time in the Eastern Time Zone (handles both EST and EDT)
eastern_time = datetime.now(ZoneInfo("America/New_York"))

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
        date_string = datetime.now().strftime("%Y-%m-%d")

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
            print(game_status)
            if game_status in ("In Progress", "Manager's Challenge"):
                game_ids.append((game_id, home_team, away_team))

        return game_ids

    except requests.exceptions.RequestException as e:
        print(f"An error occurred while fetching data: {e}")
        return []


def get_pitcher_last_5_v1_1(player_id, team_id, season=2026, as_of_date=None):
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
    season_era = '-.--'
    season_whip = '-.--'
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
                print(player_data)
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
                        season_era = season_stats.get("era", "-.--")
                        season_whip = season_stats.get("whip", "-.--")
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
    elif day1 > 0 and day3 > 0 and day4 > 0:
        return False
    elif day1 > 75 or day2 > 75 or day3 > 75 or day4 > 75 or day5 > 75:
        return False
    elif gamesPitched == gamesStarted:
        return False
    else:
        return True


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

    for side in ["home", "away"]:
        team_data = boxscore_teams.get(side, {})
        team_info = team_data.get("team", {})
        team_name = team_info.get("name", f"Unknown {side.capitalize()}")
        team_id = team_info.get("id")
        team_ids[team_name] = team_id

        # Get all pitcher IDs (those who pitched and those in bullpen)
        pitchers_ids = team_data.get("pitchers", [])
        bullpen_ids = team_data.get("bullpen", [])

        df = pd.DataFrame(bullpen_ids, columns=['player_id'])
        df['name'] = df.apply(lambda x: players_dict[f'ID{x.player_id}']['fullName'], axis=1)
        df[['day1', 'day2', 'day3', 'day4', 'day5', 'gamesPitched', 'era', 'whip', 'inningsPitched',
            'gamesStarted']] = df.apply(lambda x: get_pitcher_last_5_v1_1(x.player_id, team_id, 2026), axis=1,
                                        result_type='expand')
        df['availability'] = df.apply(
            lambda x: availability(x.day1, x.day2, x.day3, x.day4, x.day5, x.gamesPitched, x.gamesStarted), axis=1)
        df_list.append(df)

    return df_list




# 2. Loop through the dictionary to convert each DataFrame into an HTML component
html_tables_string = ""

for game, home, away in get_mlb_games():
    home_df, away_df = getBullpenData(game)

    home_df = home_df[home_df['availability']].drop(columns=['player_id', 'availability'])
    away_df = away_df[away_df['availability']].drop(columns=['player_id', 'availability'])

    home_df.style.set_properties(**{'text-align': 'left'}).set_table_styles(
        [{'selector': 'th', 'props': [('text-align', 'left')]}]
    )
    away_df.style.set_properties(**{'text-align': 'left'}).set_table_styles(
        [{'selector': 'th', 'props': [('text-align', 'left')]}]
    )

    # Convert dataframe to HTML table with clean Bootstrap styling classes
    home_table = home_df.to_html(classes='table table-striped table-hover', justify='left' ,index=False)
    away_table = away_df.to_html(classes='table table-striped table-hover', justify='left', index=False)



    # Wrap each table block in structural HTML divs
    html_tables_string += f"""
    <div class="content-section card p-4 mb-5 shadow-sm">
        <h2 class="mb-3 text-secondary">{home}</h2>
        <div class="table-responsive">
            {home_table}
        </div>
    </div>
    """
    # Wrap each table block in structural HTML divs
    html_tables_string += f"""
        <div class="content-section card p-4 mb-5 shadow-sm">
            <h2 class="mb-3 text-secondary">{away}</h2>
            <div class="table-responsive">
                {away_table}
            </div>
        </div>
        """

# 3. Embed all generated components into a single webpage template
full_page_html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MLB Live Bullpen Data Dashboard</title>
    <!-- Include Bootstrap CSS Framework via CDN -->
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body {{
            background-color: #f4f6f9;
        }}
        .dashboard-header {{
            background: linear-gradient(135deg, #1e3c72 0%, #2a5298 100%);
            color: white;
            padding: 40px 20px;
            margin-bottom: 40px;
            border-bottom: 4px solid #0056b3;
        }}
        /* Cleans up the default header alignment from Pandas generation */
        table th {{
            background-color: #e9ecef !important;
            color: #495057 !important;
            font-weight: 600;
        }}
    </style>
</head>
<body>

    <!-- Header Section -->
    <div class="dashboard-header text-center shadow">
        <h1 class="display-5 fw-bold">MLB Live Bullpen Data Dashboard</h1>
        <p class="lead mb-0">Last updated: {eastern_time.strftime("%B %-d, %Y %H:%M:%S %Z")}</p>
    </div>

    <!-- Main Content Container -->
    <div class="container">
        <div class="row justify-content-center">
            <div class="col-lg-10">
                <!-- Our loop-generated HTML blocks are injected here -->
                {html_tables_string}
            </div>
        </div>
    </div>

</body>
</html>
"""

# 4. Save the finalized string code into a standalone HTML file
output_filename = "MLBBP.html"
with open(output_filename, "w", encoding="utf-8") as file:
    file.write(full_page_html)

# upload file to s3 bucket
s3 = boto3.client('s3')
s3.upload_file(
    Filename='MLBBP.html',
    Bucket='rs3-bucket-721529235014-ca-central-1-an',
    Key='MLBBP.html',
    ExtraArgs={'ContentType': 'text/html'}
)