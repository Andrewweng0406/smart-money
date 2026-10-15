import pytest

from historical_options import import_cboe


def rows(day="2023-08-25"):
    return [dict(underlying_symbol="TSLA", quote_date=day, root="TSLA", expiration="2023-09-29",
                 strike=str(strike), option_type=side, bid_1545="1", ask_1545="1.1",
                 active_underlying_price_1545="100", implied_volatility_1545=".4", open_interest="100",
                 bid_size_1545="10", ask_size_1545="10", trade_volume="999999")
            for strike in (85, 90, 95, 105, 110, 115) for side in ("C", "P")]


def test_cboe_uses_same_timestamp_quotes_iv_and_oi_without_future_volume():
    frames, report = import_cboe(rows())
    assert report["frames"] == 1
    frame = frames[0]
    assert frame["observed_at"] == "2023-08-25T15:45:00-04:00"
    assert frame["data_usable"] is True
    assert all(leg["call_volume"] == leg["put_volume"] == 0 for leg in frame["legs"])
    assert frame["volume_available"] is False


def test_cboe_half_day_timestamp_uses_exchange_calendar():
    data = rows("2023-11-24")
    for row in data:
        row["expiration"] = "2023-12-29"
    frames, _ = import_cboe(data)
    assert frames[0]["observed_at"] == "2023-11-24T12:45:00-05:00"


def test_alternate_quote_time_and_provenance_are_explicit():
    marks = []
    frames, report = import_cboe(rows(), marks=marks, quote_minutes_before_close=0,
                                 quote_source="research_close_time_unverified")
    assert frames[0]["observed_at"] == "2023-08-25T16:00:00-04:00"
    assert frames[0]["quote_source"] == "research_close_time_unverified"
    assert marks[0]["observed_at"] == frames[0]["observed_at"]
    assert marks[0]["quote_source"] == frames[0]["quote_source"]
    assert report["mark_rows"] == len(rows())


def test_missing_oi_is_not_fabricated():
    data = rows()
    del data[0]["open_interest"]
    with pytest.raises(ValueError, match="schema missing"):
        import_cboe(data)


def test_conflicting_duplicate_contract_aborts():
    data = rows()
    data.append(dict(data[0], bid_1545=".9"))
    with pytest.raises(ValueError, match="Conflicting"):
        import_cboe(data)


def test_identical_duplicate_does_not_double_count_oi():
    data = rows()
    base, _ = import_cboe(data)
    data.append(dict(data[0]))
    duplicated, report = import_cboe(data)
    assert base[0]["total_net_gex"] == duplicated[0]["total_net_gex"]
    assert report["counts"]["identical_duplicate_rows"] == 1


def test_nonstandard_roots_and_indices_excluded():
    data = rows()
    for row in data:
        row["root"] = "TSLA1"
    frames, report = import_cboe(data)
    assert frames == []
    assert report["rejections"]["index_or_nonstandard_contract"] == len(data)


def test_crossed_and_unpaired_quotes_recorded():
    data = rows()
    data[0]["bid_1545"] = "2"
    frames, report = import_cboe(data)
    assert report["rejections"]["Crossed or sizeless quote"] == 1
    assert report["counts"]["unpaired_strikes"] == 1
    assert len(frames[0]["legs"]) == 5


def test_inconsistent_spot_prices_abort():
    data = rows()
    data[0]["active_underlying_price_1545"] = "110"
    with pytest.raises(ValueError, match="Inconsistent"):
        import_cboe(data)


def test_forecast_attachment_uses_training_selection_not_holdout_winner(tmp_path, monkeypatch):
    import historical_options as module
    frames, _ = import_cboe(rows())
    validation = {"symbols": {"TSLA": {"validation": {"folds": [{
        "start": "2022-01-01", "end_exclusive": "2024-01-01", "status": "evaluated",
        "selected_on_training": "rv60", "training_scales": {"rv60": 1.2},
        "candidate_holdout": {"ewma94": {"qlike": 0}}
    }]}}}}
    calls = []
    monkeypatch.setattr(module, "load_prices", lambda *args: (None, {}))
    def fake_forecast(history, observed, model, scale):
        calls.append((observed.isoformat(), model, scale))
        return {"forecast_vol": .3, "forecast_asof": "2023-08-24T20:00:00+00:00", "live_approved": False}
    monkeypatch.setattr(module, "forecast_at", fake_forecast)
    enriched = module.attach_forecasts(frames, tmp_path, validation)
    assert calls == [("2023-08-25T15:45:00-04:00", "rv60", 1.2)]
    assert enriched[0]["forecast_training_cutoff"] == "2022-01-01"
    assert "forecast_vol" not in frames[0]


def test_preview_labels_diagnostics_not_returns():
    import historical_options as module
    frames, _ = import_cboe(rows())
    report = module.preview_candidates(frames)
    assert report["study"] == "same_snapshot_diagnostics_not_backtest"
    assert report["performance_validation_ready"] is False
    assert report["candidates"][0]["screen_passed"] is False


def test_marks_include_quotes_outside_entry_dte_window():
    data = rows()
    for row in data:
        row["expiration"] = "2023-09-01"
    marks = []
    frames, report = import_cboe(data, marks=marks)
    assert frames == []
    assert report["mark_rows"] == len(data)
    assert marks[0]["observed_at"] == "2023-08-25T15:45:00-04:00"


def test_marks_do_not_include_rejected_crossed_quote():
    data = rows()
    data[0]["bid_1545"] = "2"
    marks = []
    import_cboe(data, marks=marks)
    assert len(marks) == len(data) - 1


def test_batch_imports_two_days_and_preserves_all_marks(tmp_path, monkeypatch):
    import csv
    import json
    import sys
    import historical_options as module
    sources = []
    for day in ("2023-08-25", "2023-08-28"):
        source = tmp_path / f"{day}.csv"
        data = rows(day)
        with source.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=data[0])
            writer.writeheader()
            writer.writerows(data)
        sources.append(source)
    output = tmp_path / "frames.jsonl"
    marks = tmp_path / "marks.csv"
    monkeypatch.setattr(sys, "argv", ["historical_options.py", "--input", *map(str, sources),
                                      "--output", str(output), "--marks-output", str(marks)])
    module.main()
    frames = [json.loads(line) for line in output.read_text().splitlines()]
    with marks.open(newline="") as handle:
        mark_rows = list(csv.DictReader(handle))
    assert len(frames) == 2
    assert len(mark_rows) == 24
    manifest = json.loads(output.with_suffix(".manifest.json").read_text())
    assert manifest["source_sessions"] == [["TSLA", "2023-08-25"], ["TSLA", "2023-08-28"]]
    assert manifest["mark_rows"] == 24


def test_batch_rejects_duplicate_day_without_overwriting_output(tmp_path, monkeypatch):
    import csv
    import sys
    import historical_options as module
    source = tmp_path / "quotes.csv"
    data = rows()
    with source.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=data[0])
        writer.writeheader()
        writer.writerows(data)
    output = tmp_path / "frames.jsonl"
    output.write_text("existing result\n")
    monkeypatch.setattr(sys, "argv", ["historical_options.py", "--input", str(source), str(source),
                                      "--output", str(output)])
    with pytest.raises(ValueError, match="Duplicate symbol/trading day"):
        module.main()
    assert output.read_text() == "existing result\n"
