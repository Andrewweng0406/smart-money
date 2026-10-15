"""Audit licensed free TSLA option sample without copying raw quotes into results."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path


SPLIT_DATE = date(2022, 8, 25)
JAN_2023 = date(2023, 1, 20)
JAN_2024 = date(2024, 1, 19)


def valid_quote(row):
    try:
        bid, ask = float(row["bid"]), float(row["ask"])
        return math.isfinite(bid) and math.isfinite(ask) and 0 <= bid <= ask and ask > 0
    except (ValueError, TypeError):
        return False


def standard_contract(row):
    try:
        expiry = date.fromisoformat(row["expiration"])
        encoded = round(float(row["strike"]) * 1000)
        side = {"call": "C", "put": "P"}[row["type"]]
        return (row["underlying"] == "TSLA" and row["style"] == "A"
                and abs(float(row["strike"]) * 1000 - encoded) < 1e-6
                and row["contract"] == f"TSLA{expiry:%y%m%d}{side}{encoded:08d}")
    except (KeyError, TypeError, ValueError):
        return False


def local_closes(path):
    with path.open(newline="") as handle:
        return {row["date"]: float(row["close"]) for row in csv.DictReader(handle)
                if row["symbol"] == "TSLA"}


def audit(sample_dir, price_file):
    files = sorted((sample_dir / "day_by_date").glob("*_options.csv"))
    if not files:
        raise ValueError("No option sample files")
    closes = local_closes(price_file)
    totals = Counter()
    day_rows = []
    observed_contracts = defaultdict(set)
    selected_contract = None
    for path in files:
        day = date.fromisoformat(path.name[:10])
        by_day = Counter()
        unique_contracts = set()
        vendor_close = None
        sample_candidates = []
        with path.open(newline="") as handle:
            for row in csv.DictReader(handle):
                if row["underlying"] != "TSLA":
                    continue
                by_day["tsla_rows"] += 1
                if vendor_close is None and row["underlying_close"]:
                    vendor_close = float(row["underlying_close"])
                side = row["type"]
                by_day[f"{side}_rows"] += 1
                if row["quote_time"]:
                    by_day["timestamped_quotes"] += 1
                if not standard_contract(row):
                    by_day["nonstandard_rows"] += 1
                    continue
                if row["contract"] in unique_contracts:
                    by_day["duplicate_contract_rows"] += 1
                    continue
                unique_contracts.add(row["contract"])
                if not valid_quote(row):
                    by_day["invalid_or_missing_quotes"] += 1
                    continue
                bid, ask, strike = float(row["bid"]), float(row["ask"]), float(row["strike"])
                expiry = date.fromisoformat(row["expiration"])
                observed_contracts[row["contract"]].add(day)
                by_day["valid_quotes"] += 1
                if expiry == JAN_2023 and side == "call":
                    by_day["jan_2023_call_quotes"] += 1
                if expiry == JAN_2024 and side == "call":
                    by_day["jan_2024_call_quotes"] += 1
                if day >= SPLIT_DATE and vendor_close is not None:
                    dte = (expiry - day).days
                    if (side == "call" and expiry == JAN_2024
                            and .70 * vendor_close <= strike <= .85 * vendor_close
                            and bid > 0 and (ask - bid) / ask <= .10):
                        by_day["liquid_jan_2024_deep_itm_call"] += 1
                        if day == date(2022, 8, 31):
                            sample_candidates.append((abs(strike - .80 * vendor_close), row["contract"]))
                    if (side == "put" and 45 <= dte <= 90
                            and .90 * vendor_close <= strike <= vendor_close
                            and bid > 0 and (ask - bid) / ask <= .25):
                        by_day["liquid_45_90d_put"] += 1
                    if (side == "call" and 21 <= dte <= 45 and strike >= vendor_close
                            and bid > 0 and (ask - bid) / ask <= .25):
                        by_day["liquid_21_45d_short_call"] += 1
        if vendor_close is None:
            raise ValueError(f"No TSLA underlying close on {day}")
        local = closes.get(day.isoformat())
        if local is None:
            raise ValueError(f"No independent TSLA close on {day}")
        comparable = vendor_close / (3 if day < SPLIT_DATE else 1)
        by_day["underlying_close_abs_diff"] = abs(comparable - local)
        if sample_candidates:
            selected_contract = min(sample_candidates)[1]
        day_rows.append((day, by_day))
        totals.update({key: value for key, value in by_day.items() if key != "underlying_close_abs_diff"})
    post_split_days = [day for day, _ in day_rows if day >= SPLIT_DATE]
    selected_coverage = (sum(day in observed_contracts[selected_contract] for day in post_split_days)
                         if selected_contract is not None else None)
    return {"first_day": day_rows[0][0].isoformat(), "last_day": day_rows[-1][0].isoformat(),
            "sample_days": len(day_rows), "post_split_days": len(post_split_days),
            "counts": dict(totals),
            "days_with_both_sides": sum(counts["call_rows"] > 0 and counts["put_rows"] > 0
                                        for _, counts in day_rows),
            "post_split_days_with_all_three_candidate_types": sum(
                all(counts[key] > 0 for key in ("liquid_jan_2024_deep_itm_call",
                                               "liquid_45_90d_put", "liquid_21_45d_short_call"))
                for day, counts in day_rows if day >= SPLIT_DATE),
            "vendor_vs_split_adjusted_local_close_max_abs_diff": round(
                max(counts["underlying_close_abs_diff"] for _, counts in day_rows), 4),
            "representative_aug31_jan2024_call": {
                "contract": selected_contract,
                "post_split_days_with_valid_quote": selected_coverage,
                "post_split_days_total": len(post_split_days)},
            "limitations": ["Secondary-source EOD standing quotes, not verified execution fills",
                            "quote_time is unavailable for this 2022 sample",
                            "2022 sample starts in July: January-March entry cannot be tested",
                            "Pre-split positions require contract-adjustment handling; supplier excludes adjusted roots before August 2026"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-dir", type=Path, default=Path("/tmp/options_sample_2022H2"))
    parser.add_argument("--zip", type=Path, default=Path("/tmp/options_sample_2022H2.zip"))
    parser.add_argument("--prices", type=Path, default=Path("research/market_data/TSLA.csv"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with args.zip.open("rb") as handle:
        source_hash = hashlib.file_digest(handle, "sha256").hexdigest()
    output = {"source": "https://historicaldata.net/file/options_sample_2022H2.zip",
              "zip_sha256": source_hash, **audit(args.sample_dir, args.prices)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, allow_nan=False) + "\n")
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
