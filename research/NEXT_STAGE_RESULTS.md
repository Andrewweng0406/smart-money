# Executed Next-Stage Research

This study was executed with real downloaded prices and an official Cboe
option-chain sample. It establishes data and forecast behavior, not profitable
option trading. No live forecast or trading path has been enabled.
An additional 2023-2025 exploratory SPY replay from a public, unverified
Parquet mirror is documented in `PUBLIC_SPY_BACKTEST.md`; its resistance-defense
holdout was negative and no result is live evidence. That same-expiry spread
was not PMCC; the two-expiry PMCC research contract is in `PMCC_RESEARCH.md`.

## Frozen Price Archive

Research cutoff: 2026-10-13 06:00 UTC, after the 2026-10-12 U.S. close.
Requested history begins 2015-01-01. TSLA, SOXL and SPY each contain 2,961
completed sessions, 2015-01-02 through 2026-10-12. SPCX contains only 84
sessions, beginning 2026-06-12. The source returned no internal missing
NYSE sessions in these observed intervals. CSV files and SHA-256 manifests
are saved in `market_data/` and can be replayed without refetching.

This archive uses Yahoo's adjusted prices for return/volatility research.
Historical `Close` is split-adjusted even with `auto_adjust=False`; it is
not a general-purpose settlement archive for historical option strikes.
Revised data, adjusted contracts and corporate actions remain limitations.

## Forecast Validation

Fixed candidates: trailing 20-session variance, trailing 60-session variance,
and EWMA with decay 0.94. Each predicts the following 21 sessions' average
squared log return, annualized by 252. Training targets must finish before the
fold boundary. Training-only variance calibration and QLIKE select the model;
holdout targets do not overlap. Folds begin in 2020, 2022, 2024 and 2026.

Positive QLIKE improvement means lower forecast error than uncalibrated RV20.
These values are losses and loss differences, NOT returns or percentages.

| Symbol | Non-overlapping holdout targets | Mean QLIKE improvement | Exploratory 95% paired block-bootstrap interval | Finding |
| --- | ---: | ---: | --- | --- |
| TSLA | 79 | 0.0411 | [-0.0077, 0.0953] | Improvement not established |
| SOXL | 79 | 0.0299 | [-0.0070, 0.0688] | Improvement not established |
| SPY | 79 | 0.0761 | [0.0236, 0.1396] | Preliminary aggregate forecast improvement |
| SPCX | 0 evaluable | N/A | N/A | Insufficient training history |

In the 2026 fold, TSLA and SOXL loss improvements are negative (-0.0301 and
-0.0180). A positive aggregate average does not establish stability in the
current regime. SPY's interval is exploratory, without multiple-comparison
correction, and does not establish an options trading edge. No symbol is live
approved. Full fold metrics are in `volatility_validation.json`.

## Authentic Option Sample

The [Cboe EOD Calcs sample](https://datashop.cboe.com/download/sample/217)
contains one date, 2023-08-25. The selected historical/CGI CSV includes 11,868
TSLA/SPY rows, all accepted by the importer, producing six 25-45 DTE frames.
Indices are outside this engine's supported stock/ETF contract assumptions.

Quotes, underlying price and IV all use the 15:45 observation. OI is
start-of-day OI. Full-day volume is excluded because it was not yet available
at 15:45. Nonstandard roots, duplicate conflicts, missing required fields,
crossed quotes and inconsistent underlying prices are rejected. Early-close
timestamps follow the exchange calendar rather than a fixed 15:45 clock.

Training-only forecasts were attached using prices through the previous close:
SPY approximately 11.74%, TSLA approximately 48.79% annualized volatility.
All six snapshot candidates abstained. SPY strangles lacked modeled net edge;
TSLA credit-spread short legs did not satisfy the wall filter. These are
same-snapshot diagnostics, NOT historical trade outcomes. See
`cboe_sample_diagnostics.json` and the frame manifests.

The importer now also exports all standard contract quotes to
`cboe_sample_marks.csv`, including near-expiry contracts outside the 25-45 DTE
entry window. This one-day sample has no complete holding-period mark sequence.
The backtester can calculate daily liquidation NAV from full daily marks and
rejects missing legs. It requires verified as-traded, corporate-action-checked
expiry prices before reporting option P&L.

A one-day sample has no previous-session quote history for delayed entries,
no full holding-period marks and no performance-validation capability.

## Data Source Findings

- No market-data provider credentials were configured in the local environment.
- Cboe's official Calcs product supplies quotes, sizes, IV and OI. Its real
  sample validates the importer; complete licensed files remain necessary.
- The public DoltHub `post-no-preference/options` schema supplies bid/ask and
  Greeks but no open interest, underlying price or quote-time field. It cannot
  reconstruct this GEX strategy; missing OI is never filled with zeros.
- No data subscription was purchased and no external messages were sent.

## Reproduction

```sh
.venv/bin/python volatility_research.py --offline \
  --as-of 2026-10-13T06:00:00+00:00

curl --fail --location --max-time 30 \
  --output /tmp/smart-money-cboe-eod-sample.zip \
  https://datashop.cboe.com/download/sample/217

.venv/bin/python historical_options.py \
  --input /tmp/smart-money-cboe-eod-sample.zip \
  --member UnderlyingOptionsEODCalcs_2023-08-25_cgi_or_historical.csv \
  --output research/cboe_sample_forecast_frames.jsonl \
  --price-directory research/market_data \
  --forecast-validation research/volatility_validation.json \
  --preview-output research/cboe_sample_diagnostics.json \
  --marks-output research/cboe_sample_marks.csv
```

## Remaining Gate

Full contemporaneous TSLA/SOXL option quotes, OI, IV, contract identifiers and
daily marks across multiple years are required before evaluating real option
P&L, early exits, NAV drawdown and capital efficiency. Forecasts must be judged
on untouched new data, not retuned until the already-observed holdout looks good.
The new importer accepts complete Cboe Calcs CSV/ZIP files through the same
validated interface; synthetic test data is never included in these findings.

Primary references: [Cboe product](https://datashop.cboe.com/option-eod-summary),
[Cboe field specification](https://datashop.cboe.com/documents/Option_EOD_Summary_Layout.pdf),
[Cboe OI and timing FAQ](https://datashop.cboe.com/faqs),
[DoltHub read SQL API](https://www.dolthub.com/docs/products/dolthub/api/v2/database/),
[yfinance adjustment implementation discussion](https://github.com/ranaroussi/yfinance/issues/687),
[Federal Reserve EWMA research](https://www.federalreserve.gov/pubs/ifdp/1997/600/ifdp600.pdf).
