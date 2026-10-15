import csv
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from optionsdx_tsla_audit import FIELDS, audit, good_quote, records


def test_quote_validation():
    assert good_quote("0", "0.05")
    assert not good_quote("2", "1")
    assert not good_quote("nan", "1")
    assert not good_quote("", "1")


def test_audit_headered_file_and_missing_oi(tmp_path):
    path = tmp_path / "tsla_eod_202201.csv"
    at = datetime(2022, 1, 3, 15, 45, tzinfo=ZoneInfo("America/New_York"))
    row = dict.fromkeys(FIELDS, "")
    row.update(QUOTE_UNIXTIME=str(int(at.timestamp())), QUOTE_READTIME="2022-01-03 15:45",
               QUOTE_DATE="2022-01-03", UNDERLYING_LAST="400", EXPIRE_DATE="2023-01-20",
               STRIKE="320", C_BID="100", C_ASK="101", P_BID="0", P_ASK="1")
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerow(row)
    result = audit([path], {"2022-01-03": 400})
    assert result["counts"]["valid_c_quotes"] == 1
    assert result["counts"]["valid_p_quotes"] == 1
    assert result["counts"].get("timestamp_mismatch_rows", 0) == 0
    assert result["years"]["2022"]["days_with_rows"] == 1
    assert result["years"]["2022"]["vendor_spot_to_local_split_adjusted_close_median"] == 1
    assert result["years"]["2022"]["days_with_plausible_tsla_split_scale"] == 1
    assert not result["years"]["2022"]["complete_market_year"]
    assert not result["can_attempt_oi_walls"]


def test_rejects_unrecognized_schema():
    with pytest.raises(ValueError, match="Unknown optionsDX schema"):
        list(records(iter(["A,B,C"])))


def test_accepts_extra_side_specific_oi_fields():
    header = ",".join((*FIELDS, "C_OPEN_INTEREST", "P_OPEN_INTEREST"))
    row = ",".join(["0"] * len(FIELDS) + ["100", "120"])
    parsed = list(records(iter([header, row])))
    assert parsed[0]["C_OPEN_INTEREST"] == "100"


def test_accepts_vendor_bracketed_header_and_spaced_values():
    header = ", ".join(f"[{field}]" for field in FIELDS)
    row = ", ".join(["1546462800"] + ["0"] * (len(FIELDS) - 1))
    parsed = list(records(iter([header, row])))
    assert parsed[0]["QUOTE_UNIXTIME"] == "1546462800"
    assert parsed[0]["STRIKE"] == "0"


def test_empty_month_is_reported_without_hiding_other_months(tmp_path):
    empty = tmp_path / "tsla_eod_202107.txt"
    empty.touch()
    populated = tmp_path / "tsla_eod_202108.txt"
    at = datetime(2021, 8, 2, 16, 0, tzinfo=ZoneInfo("America/New_York"))
    row = dict.fromkeys(FIELDS, "0")
    row.update(QUOTE_UNIXTIME=str(int(at.timestamp())), QUOTE_READTIME="2021-08-02 16:00",
               QUOTE_DATE="2021-08-02", UNDERLYING_LAST="700", EXPIRE_DATE="2022-01-21",
               STRIKE="500", C_BID="200", C_ASK="201", P_BID="1", P_ASK="2")
    with populated.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerow(row)
    result = audit([empty, populated])
    assert result["counts"]["empty_sources"] == 1
    assert result["years"]["2021"]["days_with_rows"] == 1
