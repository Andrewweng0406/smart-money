"""Stream and audit locally downloaded optionsDX TSLA EOD files.

Raw licensed files stay outside Git. This validates coverage and quote fields;
it does not convert standing quotes into executable fills.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import math
import zipfile
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

from market_calendar import get_market_session


EASTERN = ZoneInfo("America/New_York")
FIELDS = ("QUOTE_UNIXTIME", "QUOTE_READTIME", "QUOTE_DATE", "QUOTE_TIME_HOURS",
          "UNDERLYING_LAST", "EXPIRE_DATE", "EXPIRE_UNIX", "DTE", "C_DELTA", "C_GAMMA",
          "C_VEGA", "C_THETA", "C_RHO", "C_IV", "C_VOLUME", "C_LAST", "C_SIZE",
          "C_BID", "C_ASK", "STRIKE", "P_BID", "P_ASK", "P_SIZE", "P_LAST",
          "P_DELTA", "P_GAMMA", "P_VEGA", "P_THETA", "P_RHO", "P_IV", "P_VOLUME",
          "STRIKE_DISTANCE", "STRIKE_DISTANCE_PCT")


@contextmanager
def text_stream(path, member=None):
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            with archive.open(member) as binary, io.TextIOWrapper(binary, encoding="utf-8-sig", newline="") as text:
                yield text
    elif path.suffix.lower() == ".gz":
        with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as text:
            yield text
    else:
        with path.open(encoding="utf-8-sig", newline="") as text:
            yield text


def source_members(path):
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            members = [name for name in archive.namelist()
                       if not name.startswith("__MACOSX/") and name.lower().endswith((".csv", ".txt"))]
        if not members:
            raise ValueError(f"No CSV/TXT members in {path}")
        return members
    if path.suffix.lower() not in (".csv", ".txt", ".gz"):
        raise ValueError(f"Unsupported file: {path}")
    return [None]


def records(stream):
    reader = csv.reader(stream)
    first = next(reader, None)
    if first is None:
        return
    header = tuple(cell.strip().strip("[]").strip() for cell in first)
    if set(FIELDS) <= set(header) and len(header) == len(set(header)):
        fieldnames = header
    elif len(first) == len(FIELDS) and first[0].strip().isdigit():
        fieldnames = FIELDS
        yield dict(zip(fieldnames, (value.strip() for value in first)))
    else:
        raise ValueError(f"Unknown optionsDX schema: {header[:5]}")
    for row in reader:
        if len(row) != len(fieldnames):
            raise ValueError(f"Expected {len(fieldnames)} fields, got {len(row)}")
        yield dict(zip(fieldnames, (value.strip() for value in row)))


def good_quote(bid_text, ask_text):
    try:
        bid, ask = float(bid_text), float(ask_text)
        return math.isfinite(bid) and math.isfinite(ask) and 0 <= bid <= ask and ask > 0
    except (TypeError, ValueError):
        return False


def local_closes(path):
    if path is None:
        return {}
    with path.open(newline="") as stream:
        return {row["date"]: float(row["close"]) for row in csv.DictReader(stream)
                if row["symbol"] == "TSLA"}


def audit(paths, closes=None):
    closes = {} if closes is None else closes
    counts = Counter()
    by_year = defaultdict(Counter)
    date_set = set()
    times = Counter()
    ratios_by_date = {}
    inputs = []
    all_sources_have_oi = True
    oi_columns = {"C_OPEN_INTEREST", "P_OPEN_INTEREST"}
    for path in paths:
        for member in source_members(path):
            label = f"{path}!{member}" if member else str(path)
            file_counts = Counter()
            source_has_oi = None
            with text_stream(path, member) as stream:
                for row in records(stream):
                    if source_has_oi is None:
                        source_has_oi = oi_columns <= row.keys()
                    file_counts["rows"] += 1
                    try:
                        day = date.fromisoformat(row["QUOTE_DATE"])
                        expiry = date.fromisoformat(row["EXPIRE_DATE"])
                        strike = float(row["STRIKE"])
                        spot = float(row["UNDERLYING_LAST"])
                        timestamp = datetime.fromtimestamp(int(row["QUOTE_UNIXTIME"]), EASTERN)
                        if not (math.isfinite(strike) and strike > 0 and math.isfinite(spot) and spot > 0):
                            raise ValueError("Nonpositive strike or spot")
                    except (OverflowError, TypeError, ValueError):
                        file_counts["invalid_identity_rows"] += 1
                        continue
                    if timestamp.date() != day or timestamp.strftime("%Y-%m-%d %H:%M") != row["QUOTE_READTIME"]:
                        file_counts["timestamp_mismatch_rows"] += 1
                    if expiry <= day:
                        file_counts["same_day_or_expired_rows"] += 1
                    file_counts["valid_identity_rows"] += 1
                    by_year[day.year]["rows"] += 1
                    date_set.add(day)
                    times[timestamp.strftime("%H:%M")] += 1
                    for side in ("C", "P"):
                        if good_quote(row[f"{side}_BID"], row[f"{side}_ASK"]):
                            file_counts[f"valid_{side.lower()}_quotes"] += 1
                        else:
                            file_counts[f"invalid_{side.lower()}_quotes"] += 1
                    if source_has_oi:
                        try:
                            call_oi = float(row["C_OPEN_INTEREST"])
                            put_oi = float(row["P_OPEN_INTEREST"])
                            if not (math.isfinite(call_oi) and math.isfinite(put_oi)
                                    and call_oi >= 0 and put_oi >= 0):
                                raise ValueError("Invalid open interest")
                            file_counts["valid_side_specific_oi_rows"] += 1
                            if call_oi > 0 or put_oi > 0:
                                file_counts["positive_oi_rows"] += 1
                        except (TypeError, ValueError):
                            file_counts["invalid_side_specific_oi_rows"] += 1
                    close = closes.get(day.isoformat())
                    if close and day not in ratios_by_date:
                        ratios_by_date[day] = spot / close
            if file_counts["rows"] == 0:
                file_counts["empty_sources"] += 1
            all_sources_have_oi &= bool(source_has_oi)
            inputs.append({"source": label, "rows": file_counts["rows"]})
            counts.update(file_counts)
    if not inputs:
        raise ValueError("No optionsDX files supplied")
    for day in date_set:
        by_year[day.year]["days_with_rows"] += 1
    years = {}
    for year, year_counts in sorted(by_year.items()):
        cursor = date(year, 1, 1)
        expected = []
        while cursor.year == year:
            if get_market_session(cursor) is not None:
                expected.append(cursor)
            cursor += timedelta(days=1)
        ratios = [value for day, value in ratios_by_date.items() if day.year == year]
        plausible_ratios = sum(min(abs(value / factor - 1) for factor in (1, 3, 15)) <= .10
                               for value in ratios)
        observed = {day for day in date_set if day.year == year}
        missing = sorted(set(expected) - observed)
        non_market = sorted(observed - set(expected))
        years[str(year)] = {**year_counts, "expected_market_sessions": len(expected),
                            "missing_market_sessions": len(missing),
                            "missing_market_dates": [day.isoformat() for day in missing],
                            "non_market_dates": [day.isoformat() for day in non_market],
                            "complete_market_year": not missing and not non_market,
                            "vendor_spot_to_local_split_adjusted_close_median": round(median(ratios), 4)
                            if ratios else None,
                            "days_with_local_close": len(ratios),
                            "days_with_plausible_tsla_split_scale": plausible_ratios}
    return {"format": "optionsdx_us_chain_33_fields", "inputs": inputs,
            "counts": dict(counts), "years": years,
            "top_quote_times_new_york": times.most_common(12),
            "has_side_specific_open_interest_fields": all_sources_have_oi,
            "can_attempt_oi_walls": (all_sources_have_oi and counts["positive_oi_rows"] > 0
                                     and counts["invalid_side_specific_oi_rows"] == 0),
            "limitations": ["Published 33-field US schema does not include side-specific open interest",
                            "The published US schema has no underlying-symbol field; price-scale checks cannot prove identity",
                            "A free EOD archive still needs completeness and timestamp checks",
                            "Bid/ask rows do not prove executable fills or adjusted-contract handling",
                            "Spot/stock-close ratios are diagnostic, not direct quote validation"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, nargs="+", required=True)
    parser.add_argument("--prices", type=Path, default=Path("research/market_data/TSLA.csv"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    paths = []
    for item in args.input:
        if item.is_dir():
            paths.extend(sorted(path for path in item.rglob("*") if path.suffix.lower() in
                                (".csv", ".txt", ".gz", ".zip")))
        else:
            paths.append(item)
    result = audit(paths, local_closes(args.prices))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key != "inputs"}, indent=2))


if __name__ == "__main__":
    main()
