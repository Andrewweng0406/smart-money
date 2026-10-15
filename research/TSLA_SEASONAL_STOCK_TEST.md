# TSLA Seasonal Timing Diagnostic, 2015-2025

This tests the **stock-price timing hypothesis only**. It does not backtest
next-January calls, long puts, PMCC short calls, or funding call purchases from
put profits. A newly acquired July-December 2022 TSLA chain sample is too short
for the multiyear thesis; the single Cboe sample day also cannot establish those
option cash flows. See `TSLA_OPTIONS_SOURCE_AUDIT.md`. Full
machine-readable rules, monthly entries and annual outcomes are in
`tsla_seasonal_stock_diagnostic.json`.
Seven additional fixed entry-rule comparisons and chronological selection
checks are in `TSLA_WALKFORWARD_COMPARISON.md`.

## Prespecified Price Proxy

- Source: local Yahoo/yfinance TSLA `Close`, 2015-2025 complete years,
  split-adjusted and audited against the market-session calendar. The price
  file has 2020 and 2022 split events. Split-adjusted historical closes are
  suitable for stock-return ratios, **not** option strike or settlement values.
- Buy one equal-sized stock-proxy tranche in each of January, February,
  March, June, July and August. A "dip" means the previous close was at least
  10% below the high of the preceding 20 sessions. Execute at the **next**
  session's close. If there is no qualifying signal in the month, buy on the
  last session of that month. This is an ex-ante substitute for "buy the low";
  it does not know the month's eventual low.
- Sell January-March tranches at the last June close and June-August tranches
  at the last December close. Allocate one-sixth of $100,000 to each tranche,
  with zero return on waiting cash and a five-basis-point stock-proxy cost per
  side. Compare with buying the same tranches on each month's first session
  and selling on the same dates. The fixed-date strategy spends more time in
  the market, so this comparison includes the opportunity cost of waiting.
- Independently measure first-April-close to last-June-close and last-August-
  close to last-December-close stock returns. These windows are **not** option
  returns. The incomplete 2026 year is excluded.

## Findings

| Year | April-June stock | Late Aug-Dec stock | Dip-schedule account | First-session account |
| --- | ---: | ---: | ---: | ---: |
| 2015 | +43.0% | -3.6% | +12.8% | +10.4% |
| 2016 | -10.7% | +0.8% | +0.1% | +0.8% |
| 2017 | +21.1% | -12.5% | +19.4% | +22.1% |
| 2018 | +35.8% | +10.3% | +5.7% | +5.4% |
| 2019 | -22.7% | +85.4% | +36.6% | +35.9% |
| 2020 | +124.2% | +41.6% | +107.9% | +146.5% |
| 2021 | +2.7% | +43.6% | +21.2% | +23.7% |
| 2022 | -37.9% | -55.3% | -38.3% | -41.4% |
| 2023 | +34.4% | -3.7% | +32.3% | +36.4% |
| 2024 | +12.9% | +88.6% | +45.7% | +48.2% |
| 2025 | +18.3% | +34.7% | +17.5% | +17.8% |

The April-June window rose in 8/11 years; late August-December rose in 7/11.
Their mean returns were +20.1% and +20.9%, but 2020 and other large moves
skew the averages. Excluding 2020, the means were +9.7% and +18.8%, with
positive counts 7/10 and 6/10. A rising window is not proof of a repeatable
seasonal edge over TSLA's general drift or of a profitable call purchase.

The dip schedule had a +23.7% mean and +19.4% median annual account return,
versus +27.8% and +22.1% for the fixed first-session schedule. It beat the
fixed schedule in only 4/11 years. The mean difference of -4.08 percentage
points is dominated by 2020; without 2020 it is -0.64 points. Waiting for a
10% dip sometimes avoids damage, but also misses strong early rallies.

Among 49 triggered purchases, the following 20-session **stock** return was
positive 22 times, negative or zero 27 times; the mean was +1.95% and the
median -2.34%. This neither proves nor disproves that a purchased put would
profit: put premium, strike, expiry, implied-volatility changes and bid/ask
could overturn the stock-direction result. OIC notes that LEAPS pricing also
depends materially on volatility, time and interest rates.

## Decision

This stock-only check does **not** validate a January-March or June-August
TSLA option strategy. The seasonal windows are directionally interesting but
unstable, and the simple dip trigger shows no robust advantage against earlier
fixed purchases. In particular, 2022 is a direct counterexample to "we can
eat both rallies". Do not tune a more favorable trigger on these same 11 years
and present the result as independent evidence.

For a real options replay, obtain daily timestamped TSLA chains covering each
next-January LEAPS call, long put and any PMCC short call, with standard versus
split-adjusted contract identities, fills, commissions, exits, early assignment
and margin treatment. Cboe's [Option Quote Intervals](https://datashop.cboe.com/option-quote-intervals)
is one primary-source route; no data purchase has been made. Nothing here is
enabled in the live strategy path.

Reproduce:

```sh
.venv/bin/python tsla_seasonal_research.py \
  --prices research/market_data/TSLA.csv \
  --output research/tsla_seasonal_stock_diagnostic.json
```

Sources: [Tesla 2020 split](https://ir.tesla.com/press-release/tesla-announces-five-one-stock-split),
[Tesla 2022 split](https://ir.tesla.com/press-release/tesla-announces-three-one-stock-split),
[OIC LEAPS pricing](https://www.optionseducation.org/optionsoverview/leaps-pricing).
