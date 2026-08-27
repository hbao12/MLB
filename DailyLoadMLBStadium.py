import os
import requests
import calendar
import time
from datetime import datetime, timedelta, date
from sqlalchemy import create_engine, Column, Integer, String, Float, Date
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.dialects.postgresql import insert
from dotenv import load_dotenv

load_dotenv()


# ============================================
# CONFIGURATION
# ============================================

MODE = "BATCH"  # Options: "BATCH" or "DAILY"
LOAD_YEAR = 2015
LOAD_MONTHS = [8]

# SQLAlchemy Setup
Base = declarative_base()
engine = create_engine(os.environ["SQLA_CONN_STRING_MLB"])
SessionLocal = sessionmaker(bind=engine)

# ============================================
# MODELS
# ============================================


class StadiumRaw(Base):
    __tablename__ = "stadiums_raw"
    game_id = Column(Integer, primary_key=True)
    date = Column(String)
    day_of_week = Column(String)
    game_time = Column(String)
    stadium_name = Column(String)
    home_team = Column(String)
    away_team = Column(String)
    attendance = Column(Integer)
    capacity = Column(Integer)
    home_win_pct = Column(Float)
    weather_condition = Column(String)
    weather_temp = Column(String)
    is_holiday = Column(String)


class Stadium(Base):
    __tablename__ = "stadiums"
    game_id = Column(Integer, primary_key=True)
    date = Column(String)
    day_of_week = Column(String)
    game_time = Column(String)
    stadium_name = Column(String)
    home_team = Column(String)
    away_team = Column(String)
    attendance = Column(Integer)
    capacity = Column(Integer)
    home_win_pct = Column(Float)
    weather_condition = Column(String)
    weather_temp = Column(String)
    is_holiday = Column(String)


# Create tables
Base.metadata.create_all(engine)

# ============================================
# HELPERS
# ============================================
TEAM_LOCATIONS = {
    'Baltimore Orioles': 'MD', 'Boston Red Sox': 'MA', 'New York Yankees': 'NY',
    'Tampa Bay Rays': 'FL', 'Toronto Blue Jays': 'ON', 'Chicago White Sox': 'IL',
    'Cleveland Guardians': 'OH', 'Detroit Tigers': 'MI', 'Kansas City Royals': 'MO',
    'Minnesota Twins': 'MN', 'Houston Astros': 'TX', 'Los Angeles Angels': 'CA',
    'Athletics': 'CA', 'Seattle Mariners': 'WA', 'Texas Rangers': 'TX',
    'Atlanta Braves': 'GA', 'Miami Marlins': 'FL', 'New York Mets': 'NY',
    'Philadelphia Phillies': 'PA', 'Washington Nationals': 'DC', 'Chicago Cubs': 'IL',
    'Cincinnati Reds': 'OH', 'Milwaukee Brewers': 'WI', 'Pittsburgh Pirates': 'PA',
    'St. Louis Cardinals': 'MO', 'Arizona Diamondbacks': 'AZ', 'Colorado Rockies': 'CO',
    'Los Angeles Dodgers': 'CA', 'San Diego Padres': 'CA', 'San Francisco Giants': 'CA'
}


def get_us_federal_holidays(year):
    holidays = set()
    holidays.add(date(year, 1, 1))
    holidays.add(date(year, 7, 4))
    holidays.add(date(year, 11, 11))
    holidays.add(date(year, 12, 25))
    jan_first = date(year, 1, 1)
    days_until_monday = (7 - jan_first.weekday()) % 7
    first_monday = jan_first + timedelta(days=days_until_monday)
    holidays.add(first_monday + timedelta(weeks=2))
    feb_first = date(year, 2, 1)
    days_until_monday = (7 - feb_first.weekday()) % 7
    first_monday = feb_first + timedelta(days=days_until_monday)
    holidays.add(first_monday + timedelta(weeks=2))
    may_last = date(year, 5, 31)
    days_back_to_monday = (may_last.weekday() - 0) % 7
    holidays.add(may_last - timedelta(days=days_back_to_monday))
    sep_first = date(year, 9, 1)
    days_until_monday = (7 - sep_first.weekday()) % 7
    holidays.add(sep_first + timedelta(days=days_until_monday))
    nov_first = date(year, 11, 1)
    days_until_thursday = (3 - nov_first.weekday()) % 7
    first_thursday = nov_first + timedelta(days=days_until_thursday)
    holidays.add(first_thursday + timedelta(weeks=3))
    return holidays


def get_canadian_holidays(year):
    holidays = set()
    holidays.add(date(year, 1, 1))
    holidays.add(date(year, 7, 1))
    holidays.add(date(year, 12, 25))
    holidays.add(date(year, 12, 26))
    may_24 = date(year, 5, 24)
    days_back_to_monday = (may_24.weekday() - 0) % 7
    if days_back_to_monday == 0:
        holidays.add(may_24)
    else:
        holidays.add(may_24 - timedelta(days=days_back_to_monday))
    sep_first = date(year, 9, 1)
    days_until_monday = (7 - sep_first.weekday()) % 7
    holidays.add(sep_first + timedelta(days=days_until_monday))
    oct_first = date(year, 10, 1)
    days_until_monday = (7 - oct_first.weekday()) % 7
    first_monday = oct_first + timedelta(days=days_until_monday)
    holidays.add(first_monday + timedelta(weeks=1))
    return holidays


def check_holiday(game_date_str, home_team):
    try:
        game_date = datetime.strptime(game_date_str, '%Y-%m-%d').date()
        year = game_date.year
        location = TEAM_LOCATIONS.get(home_team)
        if not location: return None
        if location == 'ON':
            if game_date in get_canadian_holidays(year): return 'Yes'
        else:
            if game_date in get_us_federal_holidays(year): return 'Yes'
        return 'No'
    except:
        return None


def get_mlb_games(date_string=None):
    url = "https://statsapi.mlb.com/api/v1/schedule"
    if not date_string:
        date_string = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    params = {"sportId": 1, "date": date_string}
    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        data = response.json()
        if not data.get("dates"):
            print(f"No games found or scheduled for {date_string}.")
            return []
        return [game["gamePk"] for game in data["dates"][0].get("games", [])]
    except requests.exceptions.RequestException as e:
        print(f"An error occurred while fetching data: {e}")
        return []


def get_stadium_data(game_pk):
    url = f"https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live"
    response = requests.get(url)
    if response.status_code != 200:
        print(f"Error: Unable to fetch data (Status code: {response.status_code})")
        return None
    box_data = response.json()
    try:
        game_data = box_data.get('gameData', {})
        game_info = game_data.get('gameInfo', {})
        attendance = game_info.get('attendance')
        if attendance is None:
            print(f"No attendance data for game {game_pk}, skipping")
            return None
        game_date = game_data.get('datetime', {}).get('officialDate')
        if not game_date:
            print(f"Error: Could not find officialDate for game {game_pk}")
            return None
        datetime_info = game_data.get('datetime', {})
        game_time_str = datetime_info.get('dateTime')
        game_time = None
        if game_time_str:
            try:
                dt = datetime.fromisoformat(game_time_str.replace('Z', '+00:00'))
                game_time = dt.strftime('%H:%M')
            except:
                pass
        stadium_name = game_data.get('venue', {}).get('name')
        capacity = game_data.get('venue', {}).get('fieldInfo', {}).get('capacity',0)
        if not stadium_name:
            print(f"Error: Could not find stadium name for game {game_pk}")
            return None
        teams = game_data.get('teams', {})
        home_team = teams.get('home', {}).get('name', 'Unknown')
        home_win_pct_raw = teams.get('home', {}).get('record', {}).get('winningPercentage', None)
        home_win_pct = float(home_win_pct_raw) if home_win_pct_raw is not None else None
        away_team = teams.get('away', {}).get('name', 'Unknown')
        weather = game_data.get('weather', {})
        weather_condition = weather.get('condition', None)
        weather_temp = weather.get('temp', None)
        day_of_week = None
        try:
            date_obj = datetime.strptime(game_date, '%Y-%m-%d')
            day_of_week = date_obj.strftime('%a')
        except:
            pass
        is_holiday = check_holiday(game_date, home_team)
        return {
            "game_id": game_pk, "date": game_date, "day_of_week": day_of_week,
            "game_time": game_time, "stadium_name": stadium_name,
            "home_team": home_team, "away_team": away_team,
            "attendance": attendance, "capacity": capacity, "home_win_pct": home_win_pct,

            "weather_condition": weather_condition, "weather_temp": weather_temp,
            "is_holiday": is_holiday
        }
    except Exception as e:
        print(f"Error parsing data for game {game_pk}: {e}")
        return None


# ============================================
# MAIN LOGIC
# ============================================
def run_pipeline():
    session = SessionLocal()
    try:
        records_to_insert = []

        if MODE == "DAILY":
            yesterday = date.today() - timedelta(days=1)
            print(f"Fetching games for {yesterday}...")
            game_ids = get_mlb_games(yesterday.strftime("%Y-%m-%d"))
            for gid in game_ids:
                data = get_stadium_data(gid)
                if data: records_to_insert.append(data)
        else:
            print(f"Running in BATCH mode for {LOAD_YEAR}...")
            for month in LOAD_MONTHS:
                print(f"Fetching data for {LOAD_YEAR}-{month:02d}...")
                first_day = date(LOAD_YEAR, month, 1)
                last_day = date(LOAD_YEAR, month, calendar.monthrange(LOAD_YEAR, month)[1])
                current_date = first_day
                while current_date <= last_day:
                    date_str = current_date.strftime("%Y-%m-%d")
                    game_ids = get_mlb_games(date_str)
                    for gid in game_ids:
                        data = get_stadium_data(gid)
                        if data: records_to_insert.append(data)
                    current_date += timedelta(days=1)

        if records_to_insert:
            # Insert into stadiums_raw
            for rec in records_to_insert:
                stmt = insert(StadiumRaw).values(**rec)
                stmt = stmt.on_conflict_do_update(
                    index_elements=['game_id'],
                    set_={k: v for k, v in rec.items() if k != 'game_id'}
                )
                session.execute(stmt)
            session.commit()
            print(f"Inserted {len(records_to_insert)} records into stadiums_raw.")

            # Deduplicate and move to stadiums
            raw_data = session.query(StadiumRaw).all()
            raw_data.sort(key=lambda x: x.date, reverse=True)

            processed_ids = set()
            for row in raw_data:
                if row.game_id not in processed_ids:
                    stmt = insert(Stadium).values({
                        "game_id": row.game_id, "date": row.date, "day_of_week": row.day_of_week,
                        "game_time": row.game_time, "stadium_name": row.stadium_name,
                        "home_team": row.home_team, "away_team": row.away_team,
                        "attendance": row.attendance, "capacity": row.capacity, "home_win_pct": row.home_win_pct,
                        "weather_condition": row.weather_condition, "weather_temp": row.weather_temp,
                        "is_holiday": row.is_holiday
                    })
                    stmt = stmt.on_conflict_do_update(
                        index_elements=['game_id'],
                        set_={k: v for k, v in row.__dict__.items() if k not in ['game_id', 'stadium_raw', '_sa_instance_state']}
                    )
                    session.execute(stmt)
                    processed_ids.add(row.game_id)

            session.commit()
            print("Updated stadiums table.")
        else:
            print("No new records to process.")

    except Exception as e:
        session.rollback()
        print(f"An error occurred: {e}")
    finally:
        session.close()


if __name__ == "__main__":
    run_pipeline()
