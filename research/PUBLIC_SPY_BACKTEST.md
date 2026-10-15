# Exploratory SPY Option Replay (2023-2025)

The machine-readable results are in `public_spy_exploratory_2023_2024.json`
and `public_spy_exploratory_2023_2025.json`.
This is a data-source experiment, not evidence that any candidate is tradable.
It does not test TSLA or SOXL.
It also does **not** test PMCC: all candidates below are same-expiry spreads.
See `PMCC_RESEARCH.md` for the separate diagonal-position data contract.

## Data Checks

The [public Parquet mirror](https://github.com/anahatsingh-ui/options-dataset-hist)
supplies 2023 and 2024 SPY option chains. Its original market-data vendor and
exact quote timestamp are not independently documented. The mirror claims a
market-close snapshot; the replay assumes those quotes could be acted on at
the close. This is an unverified, potentially optimistic execution assumption.

- 4,236,746 source rows across 503 dates; two example-like contracts on the
  2024-01-15 market holiday were quarantined. The remaining 4,236,744 rows
  cover 502 trading sessions and produced 1,536 entry-window frames.
- All 7,630 SPY contracts on 2023-08-25 matched the independent
  [Cboe sample](https://datashop.cboe.com/download/sample/217) by expiry,
  strike and side; open interest matched exactly. Quotes differed because the
  mirror's claimed market-close observation is not Cboe's 15:45 snapshot.
- Every one of the 502 underlying closes agreed with the independently
  downloaded SPY `Close` series to within $0.01. No split was recorded in
  these two years. The mirror's `in_the_money` field is wrong for at least
  the sampled day and was not used.
- The 1,536 frames have forecasts selected on earlier training folds. Full
  period quote timing, IV and adjusted-contract accuracy remain unverified.

## Replay

Previous-session signals use next-session option bid/ask for entry. The
portfolio reserves defined maximum risk, charges $0.65 per leg per side plus
$0.01/share adverse entry slippage, and holds to expiry. Daily NAV liquidates
open longs at bid and shorts at ask. Starting capital is $100,000 in each
partition; partitions are independent, not compounded.

| Candidate | 2023 training trades | 2023 return | 2023 max daily NAV drawdown | 2024 holdout trades | 2024 return | 2024 max daily NAV drawdown |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Legacy template | 46 | -5.62% | 8.60% | 51 | +2.50% | 3.54% |
| Guarded candidate | 15 | -2.83% | 4.83% | 28 | -8.65% | 8.68% |
| Resistance defense | 14 | -0.51% | 10.59% | 18 | -5.87% | 17.27% |

The resistance-defense hypothesis was fixed **after** observing the original
2024 legacy/guarded comparison. It requires both signal-day and entry-day SPY
to be less than 1% below the signal-day Call Wall, positive Net GEX, and a
short call at or just above that wall with the long call at the next higher
listed strike. The 1% band was not tuned on this replay. Nevertheless, 2024
is not an untouched holdout for this newly designed candidate.

The rule was then held fixed and the previously unexamined 2025 file was used
for a new holdout. The 2025 mirror ends on 2025-12-12; frames expiring after
that date were excluded before the replay. The three-year input has 6,556,942
rows, 740 trading sessions after quarantining the same two holiday rows, and
2,298 entry-window frames; 2,216 have expiries inside the archive.

| Candidate | 2023-2024 training trades | Training return | 2025 holdout trades | 2025 return | 2025 max daily NAV drawdown |
| --- | ---: | ---: | ---: | ---: | ---: |
| Legacy template | 95 | -5.65% | 42 | -4.03% | 10.28% |
| Guarded candidate | 43 | -10.63% | 21 | -1.86% | 13.68% |
| Resistance defense | 36 | -9.04% | 13 | -7.82% | 10.41% |

Resistance defense won 1 of 13 completed 2025 trades (profit factor 0.09).
The result argues against the present hypothesis; 13 trades alone cannot
estimate its long-run expected return precisely. The Call Wall is an OI/GEX
proxy, not a verified rejection level. Entering near it does not establish a
reversal, and a 25-45 DTE spread held to expiry is not a short-term hedge.

The legacy template lost money in training and had fewer than the prespecified
100 training trades. The guarded and resistance candidates lost money in both
the 2023-2024 training window and the 2025 holdout. Under the existing
training-only selection gate, **none qualifies; cash remains the selection**.
A positive single-year legacy return
is not a validated edge, particularly when the source and execution timestamp
are unverified. No live recommendation path was enabled.

SPY dividends, American-style early assignment/exercise, pin risk, financing,
and broker-specific margin are not simulated. These matter for short option
positions. Mark coverage being complete only means this mirror has quotes for
held contracts, not that those quotes were executable. Annualized return and
Sharpe are intentionally null for these shorter-than-252-return partitions.

## Reproduce

Requires `pyarrow` in the Python environment. Files were downloaded without
credentials or purchases; SHA-256 hashes are recorded in the JSON result.

```sh
curl -fL -o /tmp/smart-money-spy-options-2023.parquet \
  https://raw.githubusercontent.com/anahatsingh-ui/options-dataset-hist/main/spy/options_2023.parquet
curl -fL -o /tmp/smart-money-spy-options-2024.parquet \
  https://raw.githubusercontent.com/anahatsingh-ui/options-dataset-hist/main/spy/options_2024.parquet
curl -fL -o /tmp/smart-money-spy-options-2025.parquet \
  https://raw.githubusercontent.com/anahatsingh-ui/options-dataset-hist/main/spy/options_2025.parquet
curl -fL -o /tmp/smart-money-spy-underlying-prices.parquet \
  https://raw.githubusercontent.com/anahatsingh-ui/options-dataset-hist/main/spy/underlying_prices.parquet
python3 public_options_research.py \
  --options /tmp/smart-money-spy-options-2023.parquet /tmp/smart-money-spy-options-2024.parquet \
    /tmp/smart-money-spy-options-2025.parquet \
  --underlying /tmp/smart-money-spy-underlying-prices.parquet \
  --split-date 2025-01-01 \
  --output research/public_spy_exploratory_2023_2025.json
```

Next validation requires licensed, timestamped historical chains for the
actual traded symbols and independent reconciliation of entry and exit quotes.
Do not retune the strategy on these observed results and then call the same
years out of sample.
