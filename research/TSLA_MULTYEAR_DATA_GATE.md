# TSLA Multiyear Option-Chain Data Gate

Status update on 2026-10-13: the user supplied 2019-2023 optionsDX archives. The full audit and the strictly complete-year 2019/2023 replay are in [TSLA_OPTIONSDX_2019_2023_RESULTS.md](TSLA_OPTIONSDX_2019_2023_RESULTS.md). The 2020-2022 data have material gaps; this is not yet a reliable multiyear win-rate dataset. No paid order was placed by the assistant.

## Source Decision

| Source | Verified coverage / format | Critical gap | Action |
| --- | --- | --- | --- |
| [optionsDX TSLA EOD](https://www.optionsdx.com/product/tsla-option-chains/) | Product lists 2016-2023 full chains. Each of the eight EOD annual variants returned a $0 price and in-stock status when checked on 2026-10-13. [FAQ](https://www.optionsdx.com/faq/) says free files become available after checkout through an account, without billing information. | [Published US field definitions](https://www.optionsdx.com/option-chain-field-definitions/) and linked SPY sample have 33 fields but **no side-specific open interest**. TSLA downloaded-file completeness, times and split handling remain unverified. No 2024-2025 years are listed. | Obtain free annual files through the user's own account, then run the audit below. Do not call this an OI-wall backtest unless actual files contain usable OI. |
| [Cboe Option EOD Summary](https://datashop.cboe.com/option-eod-summary) | Official product lists history from January 2012, selectable symbols and dates, 15:45/EOD NBBO, sizes and open interest. | Paid; exact quote/adjusted-root coverage and selected order price need checkout confirmation. | Request only the required TSLA years/fields after seeing the free-file audit; get explicit purchase approval. |
| [HistoricalData.net free sample](https://historicaldata.net/options.html) | Already verified: TSLA 2022/7-12, 127 dates, includes OI. | Cannot test January-June or multiyear win rates; 2022 quote timestamps absent. | Keep as a cross-source 2022 overlap check. |

## Local Intake

Place the user's downloaded annual CSV/TXT/GZ/ZIP files in `research/raw_options/optionsdx/`. This directory is Git-ignored; do not commit or redistribute the licensed raw data. Do not place account credentials in the repository. Run:

```sh
.venv/bin/python optionsdx_tsla_audit.py \
  --input research/raw_options/optionsdx \
  --prices research/market_data/TSLA.csv \
  --output research/optionsdx_tsla_multiyear_audit.json
```

The audit streams the files and reports per-year trading-day coverage, valid Call/Put bid/ask counts, quote-time distribution, timestamp mismatches, expired rows, stock-price ratio by year, and whether **actual files** contain parseable side-specific OI. The published US schema has no ticker column, so the TSLA price-scale check is only a heuristic, not proof of file identity. No strategy P&L should be published before these checks pass. Compare 2022 overlapping dates with the separately audited sample to detect scale, split or pricing inconsistencies. The parser has also been smoke-tested against the vendor's public 150,598-row **SPY intraday** sample; this checks format handling only, not TSLA EOD data quality.

## Frozen Research Gates

1. **Data:** Require whole-chain daily coverage for January-March, April-June, June-August and August-December across multiple years; verify all needed long/short/put strikes and expiries on both entry and liquidation dates. Reject missing held-position marks instead of carrying them forward. Determine whether quoted times truly represent consistent EOD snapshots. Check 2020 and 2022 split-adjusted deliverables explicitly.
2. **Strategy identity:** Keep separate, predeclared tests for a literal next-January Call and a >=365-DTE January Call. Never select the better-performing maturity after seeing the same year's result. Fix monthly tranche capital, the observable dip entry condition, put trigger/expiry/premium budget, short-call resistance and exit/assignment handling before evaluating additional years.
3. **Resistance choice:** If actual optionsDX files lack OI, an OI wall is **not observable**. A prior-price-high resistance rule may be tested as a different strategy, clearly labeled and specified before evaluating options P&L. Do not silently substitute the price rule for the OI rule. A true OI-wall study needs another source such as Cboe.
4. **Evaluation:** Buy at ask, sell at bid, include commissions/slippage and capital tied up; compare cash, stock, long Call only, long Call+short Call, and long Call+short Call+conditional Put. Report trade count, participation, per-trade and per-year win rates, mean/median return, worst year and maximum drawdown. Treat calendar years, not daily marks, as the basic uncertainty units. Keep post-selection years or prospective paper trading for validation.

The existing [2022 limited replay](TSLA_OPTIONS_REPLAY_2022H2.md) remains exploratory and does not clear these gates.
