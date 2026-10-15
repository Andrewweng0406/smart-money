"""Import Cboe EOD Calcs CSVs into the research schema without invented OI or IV."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import tempfile
import zipfile
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from data_fetcher import StrikeLegRaw
from gex_engine import OptionLeg, compute_net_gex_by_strike
from market_calendar import get_market_session
from smart_money import assess_chain_data_quality
from options_strategy_engine import select_strategy
from volatility_research import forecast_at, load_prices


REQUIRED = {"underlying_symbol", "quote_date", "root", "expiration", "strike", "option_type",
            "bid_1545", "ask_1545", "active_underlying_price_1545", "implied_volatility_1545",
            "open_interest", "bid_size_1545", "ask_size_1545"}


def _number(row, column, *, positive=False):
    value = float(row[column])
    if not math.isfinite(value) or value < 0 or (positive and value <= 0):
        raise ValueError(f"Invalid {column}")
    return value


def import_cboe(rows, symbols=None, marks=None, *, quote_minutes_before_close=15,
                quote_source="cboe_eod_calcs_1545"):
    groups = defaultdict(dict)
    spots = defaultdict(list)
    rejected, counts = Counter(), Counter()
    expected_expiries = defaultdict(set)
    session_cache = {}
    for row in rows:
        if not REQUIRED <= set(row):
            raise ValueError(f"Cboe Calcs schema missing: {sorted(REQUIRED - set(row))}")
        symbol = row["underlying_symbol"]
        if symbols and symbol not in symbols:
            continue
        counts["input_rows"] += 1
        if symbol.startswith("^") or row["root"] != symbol:
            rejected["index_or_nonstandard_contract"] += 1
            continue
        try:
            day, expiry = date.fromisoformat(row["quote_date"]), date.fromisoformat(row["expiration"])
            if day not in session_cache:
                session_cache[day] = get_market_session(day)
            if session_cache[day] is None:
                raise ValueError("Quote date is not an exchange session")
            if expiry < day:
                raise ValueError("Contract already expired")
            expected_expiries[(symbol, day)].add(expiry.isoformat())
            side = {"C": "call", "P": "put"}.get(row["option_type"])
            if side is None:
                raise ValueError("Invalid option type")
            strike = _number(row, "strike", positive=True)
            spot = _number(row, "active_underlying_price_1545", positive=True)
            bid, ask = _number(row, "bid_1545"), _number(row, "ask_1545")
            iv, oi = _number(row, "implied_volatility_1545"), _number(row, "open_interest")
            if ask < bid or (bid > 0 and (_number(row, "bid_size_1545") <= 0 or _number(row, "ask_size_1545") <= 0)):
                raise ValueError("Crossed or sizeless quote")
            key = (expiry.isoformat(), strike, side)
            existing = groups[(symbol, day)].get(key)
            values = {"bid": bid, "ask": ask, "iv": iv, "oi": oi}
            if existing is not None:
                if existing != values:
                    raise ValueError("Conflicting duplicate contract")
                counts["identical_duplicate_rows"] += 1
                continue
            groups[(symbol, day)][key] = values
            spots[(symbol, day)].append(spot)
            counts["accepted_rows"] += 1
        except (ValueError, KeyError) as exc:
            if "Conflicting duplicate" in str(exc):
                raise
            rejected[str(exc)] += 1
    frames = []
    for (symbol, day), contracts in sorted(groups.items()):
        observed_at = session_cache[day].market_close - timedelta(minutes=quote_minutes_before_close)
        spot_values = spots[(symbol, day)]
        if max(spot_values) / min(spot_values) > 1.001:
            raise ValueError(f"Inconsistent underlying prices in {symbol} {day}")
        spot = sum(spot_values) / len(spot_values)
        if marks is not None:
            for (expiry, strike, side), values in sorted(contracts.items()):
                marks.append({"symbol": symbol, "date": day.isoformat(), "observed_at": observed_at.isoformat(),
                              "expiry": expiry, "strike": strike, "option_type": side.upper(),
                              "bid": values["bid"], "ask": values["ask"],
                              "quote_source": quote_source})
        strikes = sorted({(expiry, strike) for expiry, strike, _ in contracts})
        legs = []
        for expiry, strike in strikes:
            sides = {side: contracts.get((expiry, strike, side)) for side in ("call", "put")}
            if any(value is None for value in sides.values()):
                counts["unpaired_strikes"] += 1
                continue
            # Full-day volume is unavailable at 15:45. Never leak it into an earlier snapshot.
            kwargs = {f"{side}_{field}": values[field] for side, values in sides.items()
                      for field in ("bid", "ask", "iv", "oi")}
            legs.append(StrikeLegRaw(expiry, strike, call_volume=0, put_volume=0, **kwargs))
        if not legs:
            continue
        quality = assess_chain_data_quality(legs, sorted(expected_expiries[(symbol, day)]))
        gex_legs = [OptionLeg(l.strike, l.call_oi, l.call_iv, l.put_oi, l.put_iv,
                              max((date.fromisoformat(l.expiry) - day).days + quote_minutes_before_close/1440,
                                  15/1440) / 365)
                    for l in legs]
        gex = compute_net_gex_by_strike(gex_legs, spot)
        put_wall = max(gex, key=lambda r: r["put_oi"])["strike"]
        call_wall = max(gex, key=lambda r: r["call_oi"])["strike"]
        total = sum(r["net_gex"] for r in gex)
        for expiry in sorted({l.expiry for l in legs}):
            dte = (date.fromisoformat(expiry) - day).days
            if not 25 <= dte <= 45:
                continue
            frames.append({"symbol": symbol, "observed_at": observed_at.isoformat(), "expiry": expiry,
                           "spot": spot, "put_wall": put_wall, "call_wall": call_wall,
                           "alert": "negative_gex_proxy" if total < 0 else None, "total_net_gex": total,
                           "data_usable": quality["usable"] and quality["oi"]["total_oi"] > 0,
                           "data_quality": quality, "quote_source": quote_source,
                           "volume_available": False,
                           "legs": [asdict(l) for l in legs if l.expiry == expiry]})
    report = {"counts": dict(counts), "rejections": dict(rejected), "frames": len(frames),
              "symbols": sorted({f["symbol"] for f in frames}),
              "quote_dates": sorted({f["observed_at"][:10] for f in frames}),
              "source_sessions": [[symbol, day.isoformat()] for symbol, day in sorted(groups)],
              "limitations": ["Only standard stock/ETF roots are admitted",
                              "15:45 quotes and IV are used together; never combine EOD quotes with earlier IV",
                              "Complete source files across dates and expiries are required for GEX",
                              "Expiry closes and daily option marks must be acquired separately"]}
    if marks is not None:
        report["mark_rows"] = len(marks)
    return frames, report


def read_cboe(path: Path, member=None):
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            choices = [name for name in archive.namelist() if name.lower().endswith(".csv")]
            if member is None and len(choices) != 1:
                raise ValueError("ZIP contains multiple CSVs; select one with --member")
            with archive.open(member or choices[0]) as stream:
                yield from csv.DictReader(io.TextIOWrapper(stream, encoding="utf-8-sig"))
    else:
        with path.open(newline="", encoding="utf-8-sig") as stream:
            yield from csv.DictReader(stream)


def attach_forecasts(frames, directory: Path, validation, histories=None):
    """Use only the calibration/selection fixed before the snapshot's fold."""
    histories = {} if histories is None else histories
    enriched = []
    for original in frames:
        frame = dict(original)
        symbol = frame["symbol"]
        observed = datetime.fromisoformat(frame["observed_at"])
        day = observed.astimezone(ZoneInfo("America/New_York")).date().isoformat()
        folds = validation.get("symbols", {}).get(symbol, {}).get("validation", {}).get("folds", [])
        fold = next((fold for fold in folds if fold["start"] <= day < fold["end_exclusive"] and fold["status"] == "evaluated"), None)
        if fold is not None:
            if symbol not in histories:
                histories[symbol], _ = load_prices(directory, symbol)
            forecast = forecast_at(histories[symbol], observed, fold["selected_on_training"],
                                   fold["training_scales"][fold["selected_on_training"]])
            if forecast is not None:
                frame.update(forecast, forecast_training_cutoff=fold["start"],
                             forecast_validation_source="expanding_window_training_only")
        enriched.append(frame)
    return enriched


def preview_candidates(frames):
    """Diagnostic snapshot screen, not a trade replay or return calculation."""
    results = []
    for frame in frames:
        day = datetime.fromisoformat(frame["observed_at"]).astimezone(ZoneInfo("America/New_York")).date()
        tte = (date.fromisoformat(frame["expiry"]) - day).days / 365
        recommendation = select_strategy([StrikeLegRaw(**leg) for leg in frame["legs"]], frame["spot"], tte,
                                         frame["put_wall"], frame["call_wall"], frame["alert"],
                                         total_net_gex=frame["total_net_gex"], forecast_vol=frame.get("forecast_vol"),
                                         data_usable=frame["data_usable"])
        results.append({"symbol": frame["symbol"], "observed_at": frame["observed_at"], "expiry": frame["expiry"],
                        "forecast_vol": frame.get("forecast_vol"), "strategy": recommendation.strategy_name,
                        "reason": recommendation.rationale, "detail_lines": recommendation.detail_lines,
                        "screen_passed": recommendation.legs is not None})
    return {"study": "same_snapshot_diagnostics_not_backtest", "candidates": results,
            "performance_validation_ready": False,
            "limitations": ["One date cannot support next-session entry or a track record",
                            "Model expectation is not observed or backtested profit"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, nargs="+", required=True,
                        help="Complete Cboe daily CSV/ZIP files, in any order")
    parser.add_argument("--member")
    parser.add_argument("--symbols", nargs="+", default=["TSLA", "SOXL", "SPCX", "SPY"])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--price-directory", type=Path)
    parser.add_argument("--forecast-validation", type=Path)
    parser.add_argument("--preview-output", type=Path)
    parser.add_argument("--marks-output", type=Path, help="All expiry/strike quotes for daily position valuation")
    args = parser.parse_args()
    if bool(args.price_directory) != bool(args.forecast_validation):
        parser.error("Provide --price-directory and --forecast-validation together")
    if args.member and len(args.input) != 1:
        parser.error("--member applies to a single ZIP")
    validation = json.loads(args.forecast_validation.read_text()) if args.forecast_validation else None
    histories, seen_sessions, sources = {}, set(), []
    totals, rejections = Counter(), Counter()
    symbols, dates, previews = set(), set(), []
    frame_count = mark_count = forecast_count = 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.marks_output:
        args.marks_output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=args.output.parent, prefix=".frames-", delete=False) as handle:
        temp_frames = Path(handle.name)
    temp_marks = None
    try:
        if args.marks_output:
            with tempfile.NamedTemporaryFile(mode="w", dir=args.marks_output.parent, prefix=".marks-", delete=False) as handle:
                temp_marks = Path(handle.name)
        with temp_frames.open("w") as frame_handle:
            mark_handle = temp_marks.open("w", newline="") if temp_marks else None
            try:
                writer = None
                if mark_handle:
                    writer = csv.DictWriter(mark_handle, fieldnames=("symbol", "date", "observed_at", "expiry", "strike",
                                                                    "option_type", "bid", "ask", "quote_source"))
                    writer.writeheader()
                for source in args.input:
                    marks = [] if writer else None
                    frames, chunk = import_cboe(read_cboe(source, args.member), set(args.symbols), marks=marks)
                    sessions = {tuple(session) for session in chunk["source_sessions"]}
                    if seen_sessions & sessions:
                        raise ValueError(f"Duplicate symbol/trading day: {sorted(seen_sessions & sessions)[:3]}")
                    seen_sessions.update(sessions)
                    if validation:
                        frames = attach_forecasts(frames, args.price_directory, validation, histories)
                    for frame in frames:
                        frame_handle.write(json.dumps(frame, allow_nan=False) + "\n")
                    if writer:
                        writer.writerows(marks)
                        mark_count += len(marks)
                    if args.preview_output:
                        previews.extend(preview_candidates(frames)["candidates"])
                    totals.update(chunk["counts"])
                    rejections.update(chunk["rejections"])
                    frame_count += len(frames)
                    forecast_count += sum("forecast_vol" in frame for frame in frames)
                    symbols.update(chunk["symbols"])
                    dates.update(chunk["quote_dates"])
                    with source.open("rb") as raw:
                        digest = hashlib.file_digest(raw, "sha256").hexdigest()
                    sources.append({"path": str(source), "sha256": digest, "member": args.member})
            finally:
                if mark_handle:
                    mark_handle.close()
        report = {"counts": dict(totals), "rejections": dict(rejections), "frames": frame_count,
                  "symbols": sorted(symbols), "quote_dates": sorted(dates),
                  "source_sessions": [list(session) for session in sorted(seen_sessions)],
                  "source_files": sources, "mark_rows": mark_count,
                  "forecasts_attached": forecast_count, "performance_validation_ready": False,
                  "limitations": ["Standard stock/ETF option classes only",
                                  "A symbol/trading day's complete chain must be in one source file",
                                  "Full daily marks and reviewed as-traded expiry closes are required for performance"]}
        if args.preview_output:
            args.preview_output.parent.mkdir(parents=True, exist_ok=True)
            args.preview_output.write_text(json.dumps({"study": "same_snapshot_diagnostics_not_backtest",
                                                      "candidates": previews, "performance_validation_ready": False},
                                                     indent=2, ensure_ascii=False, allow_nan=False) + "\n")
        temp_frames.replace(args.output)
        if temp_marks:
            temp_marks.replace(args.marks_output)
        args.output.with_suffix(".manifest.json").write_text(json.dumps(report, indent=2) + "\n")
    finally:
        temp_frames.unlink(missing_ok=True)
        if temp_marks:
            temp_marks.unlink(missing_ok=True)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
