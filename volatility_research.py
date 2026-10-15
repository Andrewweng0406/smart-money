"""Offline volatility forecast research using observed daily prices, never option P&L."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pandas_market_calendars as mcal
import yfinance as yf

from data_fetcher import _throttle


MODELS = ("rv20", "rv60", "ewma94")


def prepare_prices(history: pd.DataFrame, symbol: str, as_of: datetime) -> tuple[pd.DataFrame, dict]:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    if history.empty:
        raise ValueError("No daily prices")
    required = {"Close", "Adj Close"}
    if not required <= set(history.columns):
        raise ValueError("Require Close and Adj Close; adjustment semantics must be explicit")
    frame = history.copy()
    frame.index = pd.Index([d.date().isoformat() for d in frame.index], name="date")
    if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
        raise ValueError("Daily prices must have unique increasing dates")
    calendar = mcal.get_calendar("NYSE")
    schedule = calendar.schedule(start_date=frame.index[0], end_date=frame.index[-1])
    closes = {d.date().isoformat(): close for d, close in schedule["market_close"].items()}
    invalid_dates = [day for day in frame.index if day not in closes]
    if invalid_dates:
        raise ValueError(f"Prices outside exchange sessions: {invalid_dates[:5]}")
    frame = frame.loc[[day for day in frame.index if closes[day] <= as_of]]
    if frame.empty:
        raise ValueError("No completed sessions available at as_of")
    for column in ("Close", "Adj Close"):
        if not np.isfinite(frame[column]).all() or (frame[column] <= 0).any():
            raise ValueError(f"Invalid {column} prices")
    output = pd.DataFrame({"symbol": symbol, "close": frame["Close"],
                           "adj_close": frame["Adj Close"],
                           "available_at": [closes[day].isoformat() for day in frame.index]}, index=frame.index)
    for source, target in (("Dividends", "dividends"), ("Stock Splits", "stock_splits"), ("Volume", "volume")):
        output[target] = frame[source] if source in frame else 0.0
    expected = [d.date().isoformat() for d in schedule.index
                if frame.index[0] <= d.date().isoformat() <= frame.index[-1]]
    missing = sorted(set(expected) - set(frame.index))
    audit = {"symbol": symbol, "rows": len(output), "first": output.index[0], "last": output.index[-1],
             "missing_sessions": missing, "split_events": int((output.stock_splits > 0).sum()),
             "status": "usable" if not missing else "incomplete_sessions",
             "price_semantics": "Yahoo Close is split-adjusted; Adj Close also adjusts distributions",
             "contract_settlement_ready": False}
    return output, audit


def download_prices(symbol: str, start: str, end: str, as_of: datetime, directory: Path):
    if re.fullmatch(r"[A-Z^][A-Z0-9.^-]{0,19}", symbol) is None:
        raise ValueError("Invalid symbol")
    _throttle()
    history = yf.Ticker(symbol).history(start=start, end=end, auto_adjust=False,
                                         actions=True, timeout=20)
    prices, audit = prepare_prices(history, symbol, as_of)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{symbol}.csv"
    prices.to_csv(path, float_format="%.12g")
    manifest = {"provider": "Yahoo Finance via yfinance", "downloaded_at": datetime.now(timezone.utc).isoformat(),
                "as_of": as_of.isoformat(), "start": start, "end_exclusive": end,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "audit": audit}
    path.with_suffix(".manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return prices, manifest


def load_prices(directory: Path, symbol: str, as_of: datetime | None = None):
    if re.fullmatch(r"[A-Z^][A-Z0-9.^-]{0,19}", symbol) is None:
        raise ValueError("Invalid symbol")
    path = directory / f"{symbol}.csv"
    manifest = json.loads(path.with_suffix(".manifest.json").read_text())
    if hashlib.sha256(path.read_bytes()).hexdigest() != manifest["sha256"]:
        raise ValueError("Cached data checksum mismatch")
    prices = pd.read_csv(path, index_col="date")
    if as_of is not None:
        prices = prices.loc[pd.to_datetime(prices.available_at, utc=True) <= as_of]
    return prices, manifest


def build_origins(prices: pd.DataFrame, horizon: int = 21):
    """Forecast at completed close t, score realized squared log returns t+1..t+h."""
    if horizon <= 0:
        raise ValueError("horizon must be positive")
    if prices.index.has_duplicates or not prices.index.is_monotonic_increasing:
        raise ValueError("Require unique increasing prices")
    if len(prices) < 61 + horizon:
        return pd.DataFrame()
    calendar = mcal.get_calendar("NYSE")
    sessions = calendar.valid_days(start_date=prices.index[0], end_date=prices.index[-1])
    expected = [day.date().isoformat() for day in sessions]
    if list(prices.index) != expected:
        raise ValueError("Missing sessions: never bridge gaps as single-day returns")
    close = prices["adj_close"].to_numpy(dtype=float)
    if not np.isfinite(close).all() or (close <= 0).any():
        raise ValueError("Invalid adjusted close")
    returns = pd.Series(np.diff(np.log(close)), index=prices.index[1:])
    variance = returns.pow(2) * 252
    predictions = {"rv20": variance.rolling(20).mean(), "rv60": variance.rolling(60).mean(),
                   "ewma94": variance.ewm(alpha=.06, adjust=False, min_periods=60).mean()}
    rows = []
    for i in range(59, len(returns) - horizon):
        row = {"origin": returns.index[i], "available_at": prices.loc[returns.index[i], "available_at"],
               "target_end": returns.index[i + horizon],
               "realized_variance": float(variance.iloc[i + 1:i + horizon + 1].mean())}
        row.update({model: float(values.iloc[i]) for model, values in predictions.items()})
        if all(np.isfinite(row[model]) and row[model] > 0 for model in MODELS) and row["realized_variance"] > 0:
            rows.append(row)
    return pd.DataFrame(rows)


def qlike(realized, predicted):
    realized, predicted = np.asarray(realized), np.asarray(predicted)
    if (realized <= 0).any() or (predicted <= 0).any():
        raise ValueError("QLIKE requires positive variances")
    ratio = realized / predicted
    return ratio - np.log(ratio) - 1


def nonoverlapping(origins: pd.DataFrame):
    rows, previous_end = [], ""
    for _, row in origins.iterrows():
        # The return after target_end is not reused by the next evaluation target.
        if row.origin >= previous_end:
            rows.append(row)
            previous_end = row.target_end
    return pd.DataFrame(rows, columns=origins.columns)


def paired_interval(differences, seed=7, resamples=1000):
    differences = np.asarray(differences, dtype=float)
    if len(differences) < 12:
        return None
    generator = np.random.default_rng(seed)
    n, length = len(differences), min(3, len(differences))
    starts = generator.integers(0, n, size=(resamples, int(np.ceil(n / length))))
    indices = (starts[:, :, None] + np.arange(length)[None, None, :]) % n
    means = differences[indices.reshape(resamples, -1)[:, :n]].mean(axis=1)
    return [float(v) for v in np.quantile(means, [.025, .975])]


def evaluate_forecasts(prices: pd.DataFrame, boundaries, horizon=21):
    if len(boundaries) < 2 or list(boundaries) != sorted(set(boundaries)):
        raise ValueError("Require increasing fold boundaries")
    for boundary in boundaries:
        date.fromisoformat(boundary)
    origins = build_origins(prices, horizon)
    if origins.empty:
        return {"status": "insufficient_history", "folds": [], "live_approved": False}
    folds, combined = [], []
    for start, end in zip(boundaries, boundaries[1:]):
        training = nonoverlapping(origins.loc[origins.target_end < start])
        holdout = nonoverlapping(origins.loc[(origins.origin >= start) & (origins.target_end < end)])
        if len(training) < 30 or holdout.empty:
            folds.append({"start": start, "end_exclusive": end, "status": "insufficient_samples",
                          "train_count": len(training), "holdout_count": len(holdout)})
            continue
        # Calibration sees only targets fully realized before the holdout boundary.
        scales = {model: float(training.realized_variance.sum() / training[model].sum()) for model in MODELS}
        losses = {model: float(qlike(training.realized_variance, training[model] * scales[model]).mean()) for model in MODELS}
        selected = min(MODELS, key=losses.get)
        candidates = {}
        for model in MODELS:
            prediction = holdout[model].to_numpy() * scales[model]
            candidates[model] = {"qlike": float(qlike(holdout.realized_variance, prediction).mean()),
                                 "volatility_mae": float(np.abs(np.sqrt(prediction) - np.sqrt(holdout.realized_variance)).mean()),
                                 "realized_to_forecast_variance_ratio": float(holdout.realized_variance.sum() / prediction.sum())}
        selected_loss = qlike(holdout.realized_variance, holdout[selected] * scales[selected])
        baseline_loss = qlike(holdout.realized_variance, holdout.rv20)
        improvement = baseline_loss - selected_loss
        combined.extend(improvement.tolist())
        folds.append({"start": start, "end_exclusive": end, "status": "evaluated",
                      "train_count": len(training), "holdout_count": len(holdout),
                      "selected_on_training": selected, "training_scales": scales, "training_qlike": losses,
                      "candidate_holdout": candidates, "raw_rv20_holdout_qlike": float(baseline_loss.mean()),
                      "selected_holdout_qlike": float(selected_loss.mean()),
                      "improvement_vs_raw_rv20": float(improvement.mean())})
    interval = paired_interval(combined)
    return {"status": "evaluated" if combined else "insufficient_samples", "horizon_sessions": horizon,
            "folds": folds, "nonoverlapping_holdout_count": len(combined),
            "mean_qlike_improvement_vs_raw_rv20": float(np.mean(combined)) if combined else None,
            "paired_block_bootstrap_95pct_interval": interval,
            "consistent_positive_improvement": bool(interval and interval[0] > 0),
            "live_approved": False,
            "limitations": ["Forecast accuracy study, not option strategy returns",
                            "Revised adjusted daily closes are not a tick-level point-in-time archive",
                            "21-session variance is a proxy for the 25-45 calendar-day trade horizon",
                            "Fold candidates are reported for diagnostics, not post-hoc holdout selection",
                            "Intraday variance, skew, jumps and event forecasts are not modeled"]}


def forecast_at(prices: pd.DataFrame, observed_at: datetime, model="rv20", scale=1.0):
    """Research-only forecast using completed prices available at observation."""
    if observed_at.tzinfo is None or model not in MODELS or not np.isfinite(scale) or scale <= 0:
        raise ValueError("Invalid forecast specification")
    available = prices.loc[pd.to_datetime(prices.available_at, utc=True) <= observed_at]
    if len(available) < 61:
        return None
    if available.index.has_duplicates or not available.index.is_monotonic_increasing:
        raise ValueError("Require unique increasing prices")
    expected = [day.date().isoformat() for day in mcal.get_calendar("NYSE").valid_days(
        start_date=available.index[0], end_date=available.index[-1])]
    if list(available.index) != expected:
        raise ValueError("Missing sessions in forecast inputs")
    if not np.isfinite(available.adj_close).all() or (available.adj_close <= 0).any():
        raise ValueError("Invalid forecast prices")
    values = np.diff(np.log(available.adj_close.to_numpy(dtype=float)))**2 * 252
    if model == "rv20":
        variance = values[-20:].mean()
    elif model == "rv60":
        variance = values[-60:].mean()
    else:
        variance = pd.Series(values).ewm(alpha=.06, adjust=False).mean().iloc[-1]
    return {"forecast_vol": float(np.sqrt(variance * scale)),
            "forecast_asof": available.available_at.iloc[-1], "forecast_model": model,
            "forecast_scale": scale, "live_approved": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", default=["TSLA", "SOXL", "SPCX", "SPY"])
    parser.add_argument("--start", default="2015-01-01")
    parser.add_argument("--as-of", default=datetime.now(timezone.utc).isoformat())
    parser.add_argument("--boundaries", nargs="+", default=["2020-01-01", "2022-01-01", "2024-01-01", "2026-01-01"])
    parser.add_argument("--directory", type=Path, default=Path("research/market_data"))
    parser.add_argument("--output", type=Path, default=Path("research/volatility_validation.json"))
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    as_of = datetime.fromisoformat(args.as_of)
    if as_of.tzinfo is None:
        parser.error("--as-of requires timezone")
    boundaries = list(args.boundaries)
    final = as_of.date().isoformat()
    if boundaries[-1] < final:
        boundaries.append(final)
    report = {"as_of": args.as_of, "symbols": {}, "study": "volatility_forecast_validation_not_option_returns"}
    for symbol in args.symbols:
        try:
            if args.offline:
                prices, manifest = load_prices(args.directory, symbol, as_of)
            else:
                prices, manifest = download_prices(symbol, args.start, final, as_of, args.directory)
            report["symbols"][symbol] = {"data": manifest, "validation": evaluate_forecasts(prices, boundaries)}
            print(json.dumps({"symbol": symbol, "rows": len(prices), "status": report["symbols"][symbol]["validation"]["status"]}), flush=True)
        except Exception as exc:
            report["symbols"][symbol] = {"status": "failed", "error": str(exc)}
            print(json.dumps({"symbol": symbol, "status": "failed", "error": str(exc)}), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(str(args.output))


if __name__ == "__main__":
    main()
