import requests
from datetime import datetime, timedelta, date
import time
from sqlalchemy import MetaData, Table, insert, create_engine, text
from dotenv import load_dotenv
import os

load_dotenv()

# SQLAlchemy
engine = create_engine(os.environ["SQLA_CONN_STRING_MLB"])
metadata = MetaData()

today = datetime.now()
yesterday = today - timedelta(days=1)

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
        date_string = yesterday.strftime("%Y-%m-%d")

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
            game_id = game["gamePk"]

            # Add game ID to the list
            game_ids.append(game_id)

        return game_ids

    except requests.exceptions.RequestException as e:
        print(f"An error occurred while fetching data: {e}")
        return []

def get_pitcher_data(game_pk):

    time.sleep(1)
    boxscore_url = f"https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live"
    response = requests.get(boxscore_url)

    if response.status_code != 200:
        print(f"Error: Unable to fetch data (Status code: {response.status_code})")
        return None

    box_data = response.json()
    game_data = box_data.get('gameData', {})
    game_date = game_data.get('datetime', {}).get('officialDate')
    
    if not game_date:
        print(f"Error: Could not find officialDate for game {game_pk}")
        return None
        
    live_data = box_data.get("liveData", {})
    boxscore = live_data.get("boxscore", {})
    teams = boxscore.get("teams", {})

    # Check both home and away teams for our pitcher
    for team_side in ["home", "away"]:
        players = teams.get(team_side, {}).get("players", {})
        for player in players:
            player_data = players[player]
            # if player is pitcher and had pitching boxscore data
            if player_data['position']['code'] == "1" and player_data['stats']['pitching']:
                # Load player data
                table = Table('pitching', metadata, autoload_with=engine)
                stmt = insert(table).values(player_id=player_data['person']['id'],
                                            date=game_date,
                                            name=player_data['person']['fullName'],
                                             team_id=player_data.get('parentTeamId'),
                                            pitchesThrown=player_data['stats']['pitching']['numberOfPitches'],
                                            gamesPitched=player_data['seasonStats']['pitching'].get("gamesPitched",0),
                                            era=player_data['seasonStats']['pitching'].get("era",0.0),
                                            whip=player_data['seasonStats']['pitching'].get("whip",0.0),
                                            inningsPitched=player_data['seasonStats']['pitching'].get("inningsPitched",0.0),
                                            gamesStarted=player_data['seasonStats']['pitching'].get("gamesStarted",0)
                                            )
                with engine.connect() as conn:
                    conn.execute(stmt)
                    conn.commit()

def main():

    # Get list of MLB games for a specific day (default is yesterday)
    scheduled_games = get_mlb_games(yesterday.strftime("%Y-%m-%d"))

    for game in scheduled_games:
        # Loop through the games and load pitching data into db
        get_pitcher_data(game)

if __name__ == "__main__":
    main()