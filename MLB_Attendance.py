import xgboost as xgb
import pandas as pd
import numpy as np
from sklearn.model_selection import train_test_split, TimeSeriesSplit, RandomizedSearchCV
from sklearn.metrics import mean_absolute_percentage_error, r2_score, mean_absolute_error, mean_squared_error
from scipy.stats import randint, uniform
from sqlalchemy import create_engine
from dotenv import load_dotenv
import os
import json
import shap
import matplotlib.pyplot as plt
import plotly.graph_objects as go
import requests
import boto3

load_dotenv()

# 1. Load your data using SQLAlchemy
engine = create_engine(os.environ['SQLA_CONN_STRING_MLB'])  # Replace with your connection string
pdf = pd.read_sql("SELECT * FROM stadiums", engine)

def get_mlb_games(season=2026, team_id=141, home_only=True):
    """Fetch remaining (scheduled, unplayed) MLB games for the given team/season.

    Returns a list of dicts, one per scheduled game, with the fields needed to
    build model features (date, away/home team, gamePk, gameDate).
    """
    url = "https://statsapi.mlb.com/api/v1/schedule"
    params = {"sportId": 1, "teamId": team_id, "season": season}
    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        data = response.json()

        if not data.get("dates"):
            print(f"No games found for season {season}.")
            return []

        games = []
        for date_entry in data.get("dates", []):
            for game in date_entry.get("games", []):
                # statusCode "S" = Scheduled (i.e. unplayed / remaining)
                if game.get("status", {}).get("statusCode", "") != "S":
                    continue
                away = game.get("teams", {}).get("away", {}).get("team", {})
                home = game.get("teams", {}).get("home", {}).get("team", {})
                if home_only and home.get("id") != team_id:
                    continue
                games.append({
                    "officialDate": game.get("officialDate"),
                    "game_id": game.get("gamePk"),
                    "home_team": home.get("name"),
                    "away_team": away.get("name"),
                    "gameDate": game.get("gameDate"),
                })
        return games

    except requests.exceptions.RequestException as e:
        print(f"An error occurred while fetching data: {e}")
        return []



# filter out 0 values
pdf = pdf[pdf["attendance"] != 0]
pdf = pdf[pdf['stadium_name'] == "Rogers Centre"]

# Extract year and calculate game number
pdf['date'] = pd.to_datetime(pdf['date'])
pdf['year'] = pdf['date'].dt.year

# Historical Rogers Centre seating capacity by year. The 2022-24 renovations
# reduced baseball capacity (2023 -> 41,500; 2024-present -> 39,150).
# Source: Wikipedia "Rogers Centre" seating-capacity table.
def capacity_for_year(year):
    if year <= 1998: return 50516
    if year <= 2002: return 45100
    if year <= 2004: return 50516
    if year <= 2006: return 50598
    if year == 2007: return 48900
    if year <= 2010: return 49539
    if year <= 2012: return 49260
    if year <= 2022: return 49282
    if year == 2023: return 41500
    return 39150  # 2024-present

pdf['capacity_adj'] = pdf['year'].map(capacity_for_year)

# ... existing code ...
pdf = pdf.sort_values('date').reset_index(drop=True)
pdf['game_number'] = pdf.groupby('year').cumcount() + 1

# Use only the first 81 home games of each season (regular season; excludes postseason).
n_before = len(pdf)
pdf = pdf[pdf['game_number'] <= 81].reset_index(drop=True)
print(f"Excluded {n_before - len(pdf)} postseason/extra games (game_number > 81).")

# Identify home openers and weekend flags
pdf['is_opener_weekend'] = 0
pdf['home_opener_attendance'] = np.nan

for year, group in pdf.groupby('year'):
    first_date = group['date'].min()
    # If the home opener falls on a Friday (dayofweek 4), include the weekend dates
    if first_date.dayofweek == 4:
        friday = first_date
        saturday = first_date + pd.Timedelta(days=1)
        sunday = first_date + pd.Timedelta(days=2)
        opener_dates = [friday, saturday, sunday]
    else:
        opener_dates = [first_date]
    
    # Mark the dates and capture the attendance of the actual opener
    pdf.loc[pdf['date'].isin(opener_dates), 'is_opener_weekend'] = 1
    opener_row = group[group['date'] == first_date]
    if not opener_row.empty:
        pdf.loc[pdf['date'] == first_date, 'home_opener_attendance'] = opener_row['attendance'].values[0]

# Extract hour from game_time (format: "HH:MM")
# ... existing code ...
pdf['game_hour'] = pd.to_datetime(pdf['game_time'], format='%H:%M').dt.hour

# Weather (numeric) and holiday / long-weekend signals
pdf['weather_temp_num'] = pd.to_numeric(pdf['weather_temp'], errors='coerce')
pdf['is_holiday_flag'] = (pdf['is_holiday'] == 'Yes').astype(int)
holiday_dates = set(pdf.loc[pdf['is_holiday'] == 'Yes', 'date'])

def long_weekend(d):
    """1 if the date is a holiday or bridges a weekend with a nearby holiday."""
    for off in range(-3, 4):
        if (d + pd.Timedelta(days=off)) in holiday_dates:
            return 1
    return 0

pdf['long_weekend'] = pdf['date'].apply(long_weekend)

# Add promo feature - only map dates that exist in the dataset
pdf['date_str'] = pdf['date'].dt.strftime('%Y-%m-%d')

# Get the set of actual game dates in the dataset
actual_game_dates = set(pdf['date_str'].unique())

# Open the file and load the contents
with open('promos.json', 'r', encoding='utf-8') as file:
    promotions = json.load(file)

# List of keywords you want to keep
keep_keywords = ["GIVEAWAY", "LOONIE DOGS"]

# Keep the entry if ANY of the keywords are found in the promo text
filtered_promotions = {}
for date, promo in promotions.items():
    if any(keyword in promo for keyword in keep_keywords):
        filtered_promotions[date] = promo

print(f"Promos kept: {len(filtered_promotions)}")

# Only use promo schedule entries that match actual game dates
filtered_promo_schedule = {date: promo for date, promo in filtered_promotions.items() if date in actual_game_dates}

# Map promo features to actual games only
pdf['has_giveaway'] = pdf['date_str'].map(lambda x: 1 if x in filtered_promo_schedule and "GIVEAWAY" in filtered_promo_schedule.get(x, "") else 0)
pdf['has_loonie_dogs'] = pdf['date_str'].map(lambda x: 1 if x in filtered_promo_schedule and "LOONIE DOGS" in filtered_promo_schedule.get(x, "") else 0)
pdf = pdf.drop('date_str', axis=1)

# Define Y variable - predictor variable
pdf['attendance_pct'] = pdf['attendance'] / pdf['capacity_adj']

# Add lagged features and moving averages
pdf['lag_1'] = pdf['attendance_pct'].shift(1)
pdf['lag_2'] = pdf['attendance_pct'].shift(2)
pdf['lag_3'] = pdf['attendance_pct'].shift(3)
pdf['lag_4'] = pdf['attendance_pct'].shift(4)
pdf['lag_5'] = pdf['attendance_pct'].shift(5)
pdf['lag_6'] = pdf['attendance_pct'].shift(6)
pdf['lag_7'] = pdf['attendance_pct'].shift(7)
pdf['ma_7'] = pdf['attendance_pct'].rolling(window=7).mean()
pdf['ma_14'] = pdf['attendance_pct'].rolling(window=14).mean()
pdf['ma_28'] = pdf['attendance_pct'].rolling(window=28).mean()
pdf['attendance_std_7'] = pdf['attendance_pct'].rolling(window=7).std()
pdf['attendance_std_28'] = pdf['attendance_pct'].rolling(window=28).std()
pdf['ewma_7'] = pdf['attendance_pct'].ewm(span=7, adjust=False).mean()

# filter out rows with NaN from rolling windows and lags
pdf = pdf.dropna(subset=['lag_1', 'lag_2', 'lag_3', 'lag_4', 'lag_5', 'lag_6', 'lag_7',
                         'ma_7', 'ma_14', 'ma_28', 'attendance_std_7',
                         'attendance_std_28', 'ewma_7'])

# Mean weather temp used to fill future (unplayed) games in the forecast
weather_temp_num_mean = pdf['weather_temp_num'].mean()

# Core columns always dropped from the feature set
CORE_DROP = ["capacity", "capacity_adj", "attendance", "game_id", "stadium_name", "weather_temp",
             "weather_temp_num", "game_time", "date", "is_holiday", "is_holiday_flag", "is_opener_weekend",
              "home_opener_attendance", "weather_condition", "has_loonie_dogs",
              "ma_14", "ma_28", "long_weekend", "attendance_std_28", "attendance_pct"]

# --- Explicit Split for 2024 Prediction ---
# Training data: all years < 2024 + last 3 games of 2023 to provide lags for 2024
# Test data: all games in 2024, 2025, and 2026
# Training window: 2015 through the most recent completed season (2025).
# 2026 is held out as the forecast/evaluation target.
TRAIN_START_YEAR = 2015
FORECAST_YEAR = 2026
train_indices = sorted(pdf[(pdf['year'] >= TRAIN_START_YEAR) & (pdf['year'] < FORECAST_YEAR)].index)
test_indices = sorted(pdf[pdf['year'] == FORECAST_YEAR].index)

# Time-series cross-validation splitter (used for hyperparameter tuning)
tscv = TimeSeriesSplit(n_splits=5)


def aggregate_group(shap_values_to_plot, X_test, prefix):
    """Combine one-hot/dummy group columns into a single variable for the SHAP plot."""
    cols = [c for c in X_test.columns if c.startswith(prefix)]
    if not cols:
        return shap_values_to_plot, X_test
    idx = [X_test.columns.get_loc(c) for c in cols]
    summed = np.sum(shap_values_to_plot[:, idx], axis=1)

    combined_name = prefix.rstrip('_') + '_combined'
    X_test_plot = X_test.copy()
    X_test_plot[combined_name] = X_test[cols].sum(axis=1)
    X_test_plot = X_test_plot.drop(columns=cols)

    new_shap = np.zeros((X_test.shape[0], X_test_plot.shape[1]))
    new_shap[:, X_test_plot.columns.get_loc(combined_name)] = summed
    for col in X_test_plot.columns:
        if col != combined_name:
            new_shap[:, X_test_plot.columns.get_loc(col)] = shap_values_to_plot[:, X_test.columns.get_loc(col)]
    return new_shap, X_test_plot


def run_model(extra_drop, shap_filename, label, train_idx=None, test_idx=None, make_shap=True):
    """Train an XGBoost model, evaluate it, and produce a SHAP bar plot."""
    ti = train_idx if train_idx is not None else train_indices
    tei = test_idx if test_idx is not None else test_indices
    X = pdf.drop(CORE_DROP + extra_drop, axis=1)
    y = pdf["attendance_pct"]
    X = pd.get_dummies(X, drop_first=True)

    X_train = X.loc[ti]
    y_train = y.loc[ti]
    X_test = X.loc[tei]
    y_test = y.loc[tei]

    # Tuned model: random search over hyperparameters with time-series CV
    param_dist = {
        'n_estimators': randint(100, 400),
        'learning_rate': uniform(0.01, 0.20),
        'max_depth': randint(3, 9),
        'subsample': uniform(0.6, 0.4),
        'colsample_bytree': uniform(0.6, 0.4),
        'reg_lambda': uniform(0.0, 5.0),
        'min_child_weight': randint(1, 7),
    }
    search = RandomizedSearchCV(
        xgb.XGBRegressor(random_state=42),
        param_distributions=param_dist,
        n_iter=20, cv=tscv, scoring='r2', random_state=42, n_jobs=-1)
    search.fit(X_train, y_train)
    model = search.best_estimator_
    print(f"  Best CV params: {search.best_params_}")

    predictions = model.predict(X_test)

    test_years = pdf.loc[X_test.index, 'year']
    test_actuals = y.loc[X_test.index]
    mape_by_year = {}
    for year in test_years.unique():
        mape_by_year[year] = mean_absolute_percentage_error(test_actuals[test_years == year],
                                                            predictions[test_years == year])

    pooled_mape = mean_absolute_percentage_error(y_test, predictions)
    weighted_mape = np.mean(list(mape_by_year.values()))
    r2 = r2_score(y_test, predictions)
    mae = mean_absolute_error(y_test, predictions)
    mse = mean_squared_error(y_test, predictions)

    print(f"\n=== {label} ===")
    print("MAPE by Year:")
    for year, m in sorted(mape_by_year.items(), reverse=True):
        print(f"  {year}: {m:.4f}")
    print(f"Pooled MAPE: {pooled_mape:.4f}")
    print(f"Year-weighted MAPE: {weighted_mape:.4f}")
    print(f"R2: {r2:.4f}  MAE: {mae:.4f}  MSE: {mse:.4f}")

    # --- SHAP Analysis ---
    if not (make_shap and shap_filename):
        return {
            'model': model, 'train_columns': train_cols, 'predictions': predictions,
            'r2': r2, 'mae': mae, 'mse': mse, 'pooled_mape': pooled_mape,
            'weighted_mape': weighted_mape, 'mape_by_year': mape_by_year,
        }
    background = X_train.sample(50, random_state=42)
    explainer = shap.KernelExplainer(lambda x: model.predict(x), background)
    shap_values = explainer.shap_values(X_test)
    if isinstance(shap_values, list):
        sv = shap_values[0]
    else:
        sv = shap_values

    sv, X_test_plot = aggregate_group(sv, X_test, 'lag_')
    sv, X_test_plot = aggregate_group(sv, X_test_plot, 'away_team_')
    sv, X_test_plot = aggregate_group(sv, X_test_plot, 'day_of_week_')
    sv, X_test_plot = aggregate_group(sv, X_test_plot, 'weather_condition_')

    plt.figure(figsize=(10, 10))
    shap.summary_plot(sv, X_test_plot, plot_type="bar", show=False)
    plt.title(f"SHAP Feature Importance ({label})", pad=20)
    plt.tight_layout()
    plt.savefig(shap_filename, bbox_inches='tight')
    plt.close()
    print(f"SHAP bar plot saved to {shap_filename}")

    return {
        'label': label,
        'pooled_mape': pooled_mape,
        'weighted_mape': weighted_mape,
        'r2': r2,
        'mae': mae,
        'mse': mse,
        'mape_by_year': mape_by_year,
        'predictions': predictions,
        'y_test': y_test,
        'model': model,
        'train_columns': list(X_train.columns),
    }


# --- Model: trained without home_win_pct (this is now the only model) ---
res = run_model(["home_win_pct"], "shap_bar_plot.png", "Model (no home_win_pct)")

# upload file to s3 bucket
s3 = boto3.client('s3')
s3.upload_file(
    Filename='shap_bar_plot.png',
    Bucket='rs3-bucket-721529235014-ca-central-1-an',
    Key='shap_bar_plot.png',
    ExtraArgs={'ContentType': 'image/png'}
)


remaining_games = get_mlb_games()

# Export predictions to Excel
results = pd.DataFrame({
    'Actual_Attendance_Pct': res['y_test'],
    'Predicted_Attendance_Pct': res['predictions']
}, index=test_indices)
results['date'] = pdf.loc[test_indices, 'date']
results['year'] = pdf.loc[test_indices, 'year']
results['game_number'] = pdf.loc[test_indices, 'game_number']
results['Abs_Pct_Error'] = np.abs((results['Actual_Attendance_Pct'] - results['Predicted_Attendance_Pct']) / results['Actual_Attendance_Pct'])
results.to_excel("temp.xlsx")
print("\nPredictions exported to temp.xlsx")

# --- Model performance ---
print("\n=== Model Performance ===")
print(f"R2: {res['r2']:.4f}")
print(f"MAE: {res['mae']:.4f}")
print(f"MSE: {res['mse']:.4f}")
print(f"Pooled MAPE: {res['pooled_mape']:.4f}")
print(f"Year-weighted MAPE: {res['weighted_mape']:.4f}")
print("MAPE by Year:")
for year, m in sorted(res['mape_by_year'].items(), reverse=True):
    print(f"  {year}: {m:.4f}")

# --- Forecast remaining games and plot ---
model = res['model']
train_cols = res['train_columns']

if remaining_games:
    # Sort remaining games chronologically
    remaining_sorted = sorted(remaining_games, key=lambda g: g['officialDate'])

    # Rolling history of attendance_pct used to compute lags / moving average.
    # Start from the actual (chronological) history we have.
    buffer = list(pdf['attendance_pct'].values)

    # Continue the per-year game counter for 2026
    max_gn_2026 = pdf[pdf['year'] == 2026]['game_number'].max()
    gn_2026 = int(max_gn_2026) if pd.notna(max_gn_2026) else 0

    pred_dates = []
    pred_vals = []
    for i, g in enumerate(remaining_sorted):
        dt = pd.to_datetime(g['officialDate'])
        gd = pd.to_datetime(g['gameDate'])
        ds = dt.strftime('%Y-%m-%d')

        lag_1 = buffer[-1]
        lag_2 = buffer[-2]
        lag_3 = buffer[-3]
        lag_4 = buffer[-4]
        lag_5 = buffer[-5]
        lag_6 = buffer[-6]
        lag_7 = buffer[-7]
        ma_7 = float(np.mean(buffer[-7:]))
        ma_14 = float(np.mean(buffer[-14:]))
        ma_28 = float(np.mean(buffer[-28:]))
        std_7 = float(pd.Series(buffer[-7:]).std())
        std_28 = float(pd.Series(buffer[-28:]).std())
        ewma_7 = float(pd.Series(buffer).ewm(span=7, adjust=False).mean().iloc[-1])
        is_holiday_flag = 1 if dt in holiday_dates else 0
        lw = long_weekend(dt)

        row = pd.DataFrame([{
            'day_of_week': dt.strftime('%a'),
            'away_team': g['away_team'],
            'year': dt.year,
            'game_number': int(gn_2026 + 1 + i),
            'game_hour': int(gd.hour),
            'has_giveaway': 1 if (ds in filtered_promo_schedule and "GIVEAWAY" in filtered_promo_schedule.get(ds, "")) else 0,
            'lag_1': lag_1, 'lag_2': lag_2, 'lag_3': lag_3, 'lag_4': lag_4,
            'lag_5': lag_5, 'lag_6': lag_6, 'lag_7': lag_7,
            'ma_7': ma_7,
            'attendance_std_7': std_7, 'ewma_7': ewma_7,
        }])

        row_d = pd.get_dummies(row, drop_first=True)
        row_d = row_d.reindex(columns=train_cols, fill_value=0)

        p = float(model.predict(row_d)[0])
        pred_dates.append(dt)
        pred_vals.append(p)

        # Update rolling history so subsequent games use the forecast as lag input
        buffer.append(p)

    # Combine model predictions for played 2026 games with the remaining-game forecast,
    # converting attendance % to actual headcount (rounded to nearest integer).
    cap_2026 = float(pdf[pdf['year'] == 2026]['capacity_adj'].mean())

    test_2026_idx = [idx for idx in test_indices if pdf.loc[idx, 'year'] == 2026]
    played_gn = list(pdf.loc[test_2026_idx, 'game_number'])
    played_dates = list(pdf.loc[test_2026_idx, 'date'])
    played_actual = [int(round(a)) for a in pdf.loc[test_2026_idx, 'attendance']]
    pred_played = [int(round(p * pdf.loc[idx, 'capacity_adj']))
                   for idx, p in zip(test_2026_idx,
                                      [res['predictions'][test_indices.index(idx)] for idx in test_2026_idx])]

    pred_rem = [int(round(p * cap_2026)) for p in pred_vals]
    pred_rem_gn = list(range(int(gn_2026) + 1, int(gn_2026) + 1 + len(pred_vals)))

    all_pred_gn = played_gn + pred_rem_gn
    all_pred_dates = played_dates + pred_dates
    all_pred_vals = pred_played + pred_rem

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=played_gn, y=played_actual,
        customdata=[d.strftime('%Y-%m-%d') for d in played_dates],
        mode='lines', name='Actual (2026)',
        line=dict(width=2),
        hovertemplate='Game %{x}<br>%{customdata}<br>Actual: %{y:,.0f}<extra></extra>'))
    fig.add_trace(go.Scatter(
        x=all_pred_gn, y=all_pred_vals,
        customdata=[d.strftime('%Y-%m-%d') for d in all_pred_dates],
        mode='lines', name='Predicted (Model)',
        line=dict(width=2, dash='dot'),
        hovertemplate='Game %{x}<br>%{customdata}<br>Predicted: %{y:,.0f}<extra></extra>'))
    fig.update_layout(
        title='Rogers Centre 2026 Season: Actual vs Predicted Attendance',
        xaxis_title='Home Game # (2026 season)',
        yaxis_title='Attendance (headcount)',
        hovermode='x unified'
    )
    fig.write_html('attendance_remaining_forecast.html')

    # upload file to s3 bucket
    s3 = boto3.client('s3')
    s3.upload_file(
        Filename='attendance_remaining_forecast.html',
        Bucket='rs3-bucket-721529235014-ca-central-1-an',
        Key='attendance_remaining_forecast.html',
        ExtraArgs={'ContentType': 'text/html'}
    )

    print(f"\nForecasted {len(pred_dates)} remaining games with the model.")
    print("Plotly line graph saved to attendance_remaining_forecast.html")

    # --- Second graph: per-game MAPE for the 2026 forecasts ---
    y_true_2026 = pdf.loc[test_2026_idx, 'attendance_pct'].values
    pred_2026 = np.array([res['predictions'][test_indices.index(idx)] for idx in test_2026_idx])
    ape_2026 = np.abs(y_true_2026 - pred_2026) / y_true_2026

    mape_2026 = res['mape_by_year'].get(2026, float('nan'))

    fig2 = go.Figure()
    fig2.add_trace(go.Scatter(
        x=played_gn, y=ape_2026,
        customdata=[d.strftime('%Y-%m-%d') for d in played_dates],
        mode='lines+markers', name='Actual MAPE (played games)',
        line=dict(width=2),
        hovertemplate='Game %{x}<br>%{customdata}<br>MAPE: %{y:.1%}<extra></extra>'))
    fig2.add_trace(go.Scatter(
        x=pred_rem_gn, y=[mape_2026] * len(pred_rem_gn),
        mode='lines', name='Expected MAPE (future, actuals unavailable)',
        line=dict(width=2, dash='dot'),
        hovertemplate='Game %{x}<br>Expected MAPE: %{y:.1%}<extra></extra>'))
    fig2.add_hline(y=mape_2026, line=dict(color='gray', dash='dash'),
                   annotation_text=f'2026 mean MAPE {mape_2026:.1%}', annotation_position='top left')
    fig2.update_layout(
        title='Rogers Centre 2026 Season: Model MAPE per Game',
        xaxis_title='Home Game # (2026 season)',
        yaxis_title='MAPE (per game)',
        hovermode='x unified'
    )
    fig2.write_html('attendance_mape_per_game.html')
    print("Plotly MAPE-per-game graph saved to attendance_mape_per_game.html")
else:
    print("\nNo remaining (scheduled) games found to forecast.")

# --- Walk-forward evaluation: train 2015..(yr-1), test = yr ---
print("\n=== Walk-forward evaluation (train 2015..yr-1, test = yr) ===")
wf_results = {2026: res}  # 2026 already trained by main pipeline (2015-2025)
for yr in (2024, 2025):
    tr = sorted(pdf[(pdf['year'] >= TRAIN_START_YEAR) & (pdf['year'] < yr)].index)
    te = sorted(pdf[pdf['year'] == yr].index)
    wf_results[yr] = run_model(["home_win_pct"], None, f"Model ({yr})",
                               train_idx=tr, test_idx=te, make_shap=False)

print(f"{'Year':<6}{'Train window':<14}{'MAPE':>9}{'R2':>8}{'n':>6}")
all_act, all_pred = [], []
for yr in (2024, 2025, 2026):
    r = wf_results[yr]
    n = len(r['predictions'])
    print(f"{yr:<6}{f'2015-{yr-1}':<14}{r['mape_by_year'][yr]:>9.4f}{r['r2']:>8.3f}{n:>6}")
    yri = pdf[pdf['year'] == yr].index
    all_act.extend(pdf.loc[yri, 'attendance_pct'].values)
    all_pred.extend(r['predictions'])
print(f"\nPooled MAPE across folds: {mean_absolute_percentage_error(all_act, all_pred):.4f}")

# --- Write walk-forward MAPE table to HTML ---
html_rows = ""
for yr in (2024, 2025, 2026):
    r = wf_results[yr]
    mape = r['mape_by_year'][yr]
    html_rows += (f"      <tr>\n        <td>{yr}</td>\n"
                  f"        <td>2015&ndash;{yr - 1}</td>\n"
                  f"        <td>{mape * 100:.2f}%</td>\n      </tr>\n")
pooled_mape = mean_absolute_percentage_error(all_act, all_pred)
html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Walk-forward MAPE by Year</title>
  <style>
    body {{ font-family: Arial, Helvetica, sans-serif; margin: 40px; background: #000000; color: #eaeaea; }}
    h2 {{ font-weight: 600; color: #ca98ff; }}
    table {{ border-collapse: collapse; width: 420px; margin-top: 16px; }}
    th, td {{ border: 1px solid #8A2BE2; padding: 10px 14px; text-align: left; }}
    th {{ background: #8A2BE2; color: #ffffff; }}
    tbody tr:nth-child(even) {{ background: #1a0f2e; }}
    tbody tr:nth-child(odd) {{ background: #000000; }}
    tfoot td {{ font-weight: 700; background: #ca98ff; color: #000000; }}
  </style>
</head>
<body>
  <h2>Walk-forward Evaluation &mdash; Rogers Centre Attendance</h2>
  <p>Train window: 2015 through (year &minus; 1). Test: the indicated year.</p>
  <table>
    <thead>
      <tr>
        <th>Year</th>
        <th>Train window</th>
        <th>MAPE</th>
      </tr>
    </thead>
    <tbody>
{html_rows}    </tbody>
    <tfoot>
      <tr>
        <td colspan="2">Pooled MAPE (across folds)</td>
        <td>{pooled_mape * 100:.2f}%</td>
      </tr>
    </tfoot>
  </table>
</body>
</html>
"""
with open("walk_forward_mape.html", "w") as _f:
    _f.write(html)
print("Walk-forward MAPE table saved to walk_forward_mape.html")
# upload file to s3 bucket
s3 = boto3.client('s3')
s3.upload_file(
    Filename='walk_forward_mape.html',
    Bucket='rs3-bucket-721529235014-ca-central-1-an',
    Key='walk_forward_mape.html',
    ExtraArgs={'ContentType': 'text/html'}
)

print("\n=== Mean prediction bias by year (pred - actual) ===")
years = sorted({int(pdf.loc[i, 'year']) for i in test_indices})
for yr in years:
    idxs = [i for i in test_indices if int(pdf.loc[i, 'year']) == yr]
    act = np.array([pdf.loc[i, 'attendance_pct'] for i in idxs])
    pred = np.array([res['predictions'][test_indices.index(i)] for i in idxs])
    bias = (pred - act).mean()
    print(f"  {yr}: n={len(idxs):4d} actual={act.mean():.3f} pred={pred.mean():.3f} bias={bias:+.4f}")
