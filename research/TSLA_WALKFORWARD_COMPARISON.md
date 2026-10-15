# TSLA Seasonal Rule Comparison, 2016-2025

**Bottom line:** no tested stock-price timing rule simultaneously maximized
win rate, account return and downside protection. These are stock-proxy
diagnostics, **not** returns on next-January calls or long puts. The full
candidate-by-year record and chronological selection folds are in
`tsla_walkforward_stock_comparison.json`.

## Method

Seven candidate entry rules were compared on the same TSLA split-adjusted
daily closes: first session of each month; last session; first ex-ante 5%,
10% or 15% 20-session drawdown, falling back to month-end; first session only
if previous close was above its 200-session average; and the 10% dip rule
subject to that trend gate. The trend candidates may hold cash. Entries are
in January-March and June-August; spring positions close at June-end, summer
positions at December-end. Six fixed $16,667 allocation slots are used even
when a trade is skipped, with five basis points per side in **stock-proxy**
costs and zero yield on idle cash. The 2015 price history warms up the
lookbacks; reported candidate years are 2016-2025.

"Annual win" means a positive return on the entire $100,000 account for that
calendar-year cohort. "Trade win" means a positive cost-adjusted return on
an executed monthly stock tranche. Neither is an option win rate.

| Entry rule | Positive account years | Winning tranches | Mean account return | Worst year | Worst daily NAV DD | Participation |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Month first | 9/10 | 33/60 = 55.0% | +29.5% | -41.4% | 48.5% | 100% |
| Month last | 9/10 | **37/60 = 61.7%** | +22.4% | -41.3% | 49.0% | 100% |
| 5% dip | 9/10 | 34/60 = 56.7% | +26.4% | -40.1% | 48.3% | 100% |
| 10% dip | 9/10 | 36/60 = 60.0% | +24.8% | -38.3% | 47.8% | 100% |
| 15% dip | 9/10 | 35/60 = 58.3% | +25.3% | -40.1% | 49.4% | 100% |
| 200-day gate + month first | 7/10 | 17/38 = 44.7% | +17.6% | -15.6% | 34.5% | 63% |
| 200-day gate + 10% dip | 5/10 | 19/39 = 48.7% | +15.8% | -16.6% | 31.8% | 65% |

Month-last has the **highest observed per-tranche win rate**, not the best
return or risk profile. All full-participation rules were positive in the
same nine years and negative in 2022. In that year **all six tranches lost**
under each full-participation rule; waiting for a deeper dip did not solve the
regime problem. Trend gates skipped June-August in 2022, reducing the account
loss but not protecting January-March positions already purchased. A trend
gate is cash abstention, **not** a long put.

## Chronological Selection

For each 2021-2025 test year, only 2016 through the preceding year were used
to choose among the same seven candidates. Three selection objectives were
tested. The trade-win objective required at least 80% training participation;
the drawdown objective required at least 50% participation and positive
training mean return. These are simulations of a rule-selection process, not
pristine holdouts: these years had already been inspected in this project.

| Training objective | Rules chosen for 2021-2025 | Positive test years | Mean test-year account return | Worst test year |
| --- | --- | ---: | ---: | ---: |
| Annual win rate, then median return | Month first all five | 4/5 | +16.9% | -41.4% |
| Per-tranche win rate | 10% dip all five | 4/5 | +15.7% | -38.3% |
| Lowest historical drawdown | Month last twice, trend dip three times | 3/5 | +2.9% | -41.3% |

The drawdown selector chose month-last before the 2022 loss, then moved to
the trend gate and missed much of the 2023 rebound. Selection based on past
win rate did not raise the number of winning future years.

## Robustness and Limits

2020 is an unusually strong year. Without it, month-first mean account return
falls from +29.5% to +16.5%; month-last falls from +22.4% to +12.9%.
The paired difference (candidate minus month-first) was -7.1 percentage
points for month-last and -4.7 points for the 10% dip. Descriptive year-block
bootstrap intervals were [-15.8, -0.5] and [-12.6, -0.0] points respectively.
These intervals are not adjusted for testing seven rules, are based on only
ten potentially dependent years, and **do not** establish future superiority.
Searching additional thresholds for a higher historical win rate would
increase overfitting risk.

The result says nothing about whether long puts would have offset the 2022
loss or funded extra calls. TSLA option premiums depend on strike, term,
implied volatility and executable bid/ask. The archive has no multi-year
TSLA option chain. Nothing here has been enabled in the live strategy path.

To reproduce:

```sh
.venv/bin/python tsla_walkforward_research.py \
  --prices research/market_data/TSLA.csv \
  --output research/tsla_walkforward_stock_comparison.json
```

Related baseline: `TSLA_SEASONAL_STOCK_TEST.md`.
References: [OIC LEAPS pricing](https://www.optionseducation.org/optionsoverview/leaps-pricing),
[Bailey et al. on backtest overfitting](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2308659).
