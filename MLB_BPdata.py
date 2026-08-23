import requests
import time
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import boto3
import os
from sqlalchemy import MetaData, Table, insert, create_engine, text
from dotenv import load_dotenv

load_dotenv()


# SQLAlchemy
engine = create_engine(os.environ["SQLA_CONN_STRING_MLB"])
metadata = MetaData()

# Get current time in the Eastern Time Zone (handles both EST and EDT)
eastern_time = datetime.now(ZoneInfo("America/New_York"))

def get_mlb_games(date_string=None, live_only=False):
    """
    Fetches MLB games for a specific date and returns a list of game IDs.
        print(pd.DataFrame(game_ids))    :param date_string: Date formatted as 'YYYY-MM-DD'
    :param live_only: Only return games currently in progress, instead of every
        game that has not finished yet
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
            #if game_status in ("Pre-Game"):
            if live_only:
                # "Live" covers In Progress, Warmup and delays, but not games
                # that already wrapped up or never started
                if game["status"]["abstractGameState"] == "Live":
                    game_ids.append((game_id, home_team, away_team))
            elif game_status not in ("Final",):
                game_ids.append((game_id, home_team, away_team))

        return game_ids

    except requests.exceptions.RequestException as e:
        print(f"An error occurred while fetching data: {e}")
        return []


def get_pitcher_last_5_v1_2(player_id):

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



    # Return list: [day1, day2, day3, day4, day5, gamesPitched, era, whip]
    pitches_list = [pitches_by_date[date] for date in last_5_dates] + [season_games_pitched, season_era, season_whip,
                                                                       season_innings, season_games_started]
    return pitches_list

# Stats pulled per pitcher, in the order getBullpenData expands them into columns
LAST5_COLUMNS = ['day1', 'day2', 'day3', 'day4', 'day5', 'gamesPitched', 'era', 'whip',
                 'inningsPitched', 'gamesStarted', 'availability']

def get_pitcher_last_5_v2(player_id, game_date):
    """Look up a pitcher's stored row for this date.

    Always returns one value per LAST5_COLUMNS entry, so result_type='expand'
    yields the same columns whether or not the row exists. A pitcher we have no
    rows for is still listed, with zeroed stats: no recorded outings means
    nothing to be resting from."""
    # day1 is yesterday, day5 is five days back. A doubleheader puts two rows on
    # the same date, so the counts are summed rather than picked.
    days = [(datetime.now() - timedelta(days=i + 1)).strftime("%Y-%m-%d") for i in range(5)]
    params = {"player_id": player_id, **{f"day{i + 1}": day for i, day in enumerate(days)}}

    with engine.connect() as connection:
        pitch_counts = connection.execute(text("""
        SELECT
            COALESCE(SUM(CASE WHEN "date" = :day1 THEN "pitchesThrown" END), 0) AS day1,
            COALESCE(SUM(CASE WHEN "date" = :day2 THEN "pitchesThrown" END), 0) AS day2,
            COALESCE(SUM(CASE WHEN "date" = :day3 THEN "pitchesThrown" END), 0) AS day3,
            COALESCE(SUM(CASE WHEN "date" = :day4 THEN "pitchesThrown" END), 0) AS day4,
            COALESCE(SUM(CASE WHEN "date" = :day5 THEN "pitchesThrown" END), 0) AS day5
        FROM pitching
        WHERE player_id = :player_id AND "date" BETWEEN :day5 AND :day1
        """), params).one()

        # Season stats are cumulative, so take them from the pitcher's most recent
        # outing. That can be older than day5 for someone who has not pitched lately.
        season = connection.execute(text("""
        SELECT "gamesPitched", era, whip, "inningsPitched", "gamesStarted"
        FROM pitching
        WHERE player_id = :player_id AND "date" <= :day1
        ORDER BY "date" DESC
        LIMIT 1
        """), params).first()

    day1, day2, day3, day4, day5 = (int(count) for count in pitch_counts)

    # No stored outing at all: a call-up, or someone who has not appeared yet.
    # Same zeroed season line the MLB feed gives a pitcher with no appearances.
    if season is None:
        gamesPitched, era, whip, inningsPitched, gamesStarted = 0, 0.0, 0.0, "0.0", 0
    else:
        gamesPitched, era, whip, inningsPitched, gamesStarted = season

    return [day1, day2, day3, day4, day5, gamesPitched, era, whip, inningsPitched, gamesStarted,
            availability(day1, day2, day3, day4, day5, gamesPitched, gamesStarted)]

def availability(day1, day2, day3, day4, day5, gamesPitched, gamesStarted):
    if day1 > 35:
        return False
    elif day1 > 0 and day2 > 0 and (day1+day2) > 20:
        return False
    elif day1 > 0 and day3 > 0 and day4 > 0:
        return False
    elif day1 > 75 or day2 > 75 or day3 > 75 or day4 > 75 or day5 > 75:
        return False
    elif gamesPitched > 0 and gamesPitched == gamesStarted:
        # Every appearance was a start, so this is a starter, not a bullpen arm.
        # Needs the gamesPitched guard: 0 == 0 is a pitcher we have no rows for.
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
    game_date = game_data.get("datetime", {}).get("officialDate", "")

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
        df[LAST5_COLUMNS] = df.apply(lambda x: get_pitcher_last_5_v2(x.player_id, game_date), axis=1,
                                     result_type='expand')
        print(df)
        df_list.append(df)
    print(df_list)
    return df_list




def games_to_show():
    """Today's slate, plus any of the previous day's games still in progress.

    A game that runs past midnight keeps the previous day's officialDate, so it
    drops off today's schedule while still being played. Dates are taken in
    Eastern time because that is what the officialDate is based on.

    Returns (game_id, home_team, away_team, note) tuples, where note labels the
    carryover games so they are distinguishable in the dashboard."""
    today = eastern_time.strftime("%Y-%m-%d")
    yesterday = (eastern_time - timedelta(days=1)).strftime("%Y-%m-%d")

    carryover = get_mlb_games(yesterday, live_only=True)
    if carryover:
        print(f"{len(carryover)} game(s) from {yesterday} still in progress")

    # Carryover games first: they are being played right now
    return ([(*game, f" (from {yesterday}, still in progress)") for game in carryover]
            + [(*game, "") for game in get_mlb_games(today, live_only=True)])


def available_only(df):
    """Keep only the pitchers flagged available.

    Pitchers with no bplast5 row for this date come back as NaN from the expand
    in getBullpenData, which makes 'availability' an object column that cannot be
    used as a boolean mask. Treat those as unavailable, but say who they were."""
    missing = df['availability'].isna()
    if missing.any():
        print(f"No bplast5 data, excluding: {', '.join(df.loc[missing, 'name'])}")

    mask = df['availability'].fillna(False).astype(bool)
    return df[mask].drop(columns=['player_id', 'availability'])


# 2. Loop through the dictionary to convert each DataFrame into an HTML component
html_tables_string = ""

for game, home, away, note in games_to_show():
    home_df, away_df = getBullpenData(game)

    home_df = available_only(home_df)
    away_df = available_only(away_df)

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
        <h2 class="mb-3 text-secondary">{home}{note}</h2>
        <div class="table-responsive">
            {home_table}
        </div>
    </div>
    """
    # Wrap each table block in structural HTML divs
    html_tables_string += f"""
        <div class="content-section card p-4 mb-5 shadow-sm">
            <h2 class="mb-3 text-secondary">{away}{note}</h2>
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