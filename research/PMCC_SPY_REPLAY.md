# Exploratory SPY Long Call + PMCC + Conditional Put Replay

This is an exploratory **SPY-only** replay, not a tradable performance claim.
The public option mirror has no independently verified original vendor or
exact quote timestamp. The rules were designed after inspecting this archive;
none of 2023-2025 is an untouched holdout. The 2025 file ends December 12.
Machine-readable results and daily NAV are in
`pmcc_spy_exploratory_2023_2025.json`.

## Fixed Diagnostic Rules

- Each calendar year starts independently with $100,000 cash. On its first
  trading day, buy one SPY call 365-730 DTE, strike 70-85% of spot, preferring
  540 DTE and 80% of spot. Hold through the last available session of that
  year, then sell at bid. This is not a continuously rolled LEAPS portfolio.
- Call/Put Walls are the largest open-interest strikes in the 21-45 DTE chain,
  within 5% above/below spot. They are **OI proxies**, not measured dealer
  inventory or proven resistance/support. A short call is sold only when the
  prior signal and entry-day spot are both within 1% below the prior Call Wall.
  Select 21-45 DTE, strike at or above that wall and above the long call strike;
  buy it back after five trading sessions. No new short while one is open.
- A put is considered when today's spot is below the **previous** session's
  Put Wall. Buy at the following available option quote, choosing 45-90 DTE
  and a strike nearest 95% of signal spot; sell after 20 trading sessions.
  This is conditional protection, not permanent insurance.
- All three strategies buy the same long call. PMCC and PMCC+Put sell the same
  short calls. Cash is a fourth baseline. Long buys pay ask; short sales
  receive bid; closures use the opposite side. Each leg/side incurs $0.65
  commission and $0.01/share adverse slippage. Daily NAV uses liquidation
  bid/ask, with no carried-forward missing marks. Historical EOD fills are
  assumed, but the archive does not prove they were executable.

## Result

Account return and peak-to-trough daily NAV drawdown are percentages of the
same $100,000 starting account, **not** returns on the option debit. Only one
long call is held; its initial debit was $10,388 in 2023, $12,217 in 2024,
and $15,263 in 2025. The rest of the account sits as zero-yield cash.

| Year | Long Call return / DD | PMCC return / DD | PMCC + Put return / DD | Short Call net P&L | Put net P&L |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2023 | 7.16% / 4.69% | 5.70% / 4.18% | 4.87% / 3.95% | -$1,463 | -$822 |
| 2024 | 9.14% / 4.18% | 7.79% / 4.26% | 4.00% / 4.76% | -$1,352 | -$3,793 |
| 2025 to Dec 12 | 6.72% / 9.53% | 6.01% / 9.53% | 5.83% / 5.90% | -$711 | -$180 |

Cash returns 0% in this zero-interest model. Short-call entries numbered
14, 15, and 11; put entries numbered 3, 5, and 3. The short call was held
in the money at the close on 28, 27, and 22 session observations respectively.
That is **not** an assignment count. American exercise, dividends and broker
margin are not modeled, so actual short-call outcomes can be worse or
operationally different. OIC explains that early assignment can occur,
particularly around ex-dividend dates.

The diagnostic finding is narrow: **neither overlay improved year-end return
over the long call alone in any of these three calendar-year cohorts**. The
conditional put reduced observed drawdown in 2025 but raised it in 2024.
No inference of positive expected return, statistical significance or
superior capital efficiency is warranted. The put rule was not tuned here;
changing its trigger, strike or holding period on the same years and calling
the new result out of sample would be data mining.

## Reproduce

Use the public files and source checks documented in `PUBLIC_SPY_BACKTEST.md`,
then run with system `python3` (the local `.venv` lacks `pyarrow`):

```sh
python3 pmcc_public_replay.py \
  --options /tmp/smart-money-spy-options-2023.parquet \
    /tmp/smart-money-spy-options-2024.parquet \
    /tmp/smart-money-spy-options-2025.parquet \
  --underlying /tmp/smart-money-spy-underlying-prices.parquet \
  --output research/pmcc_spy_exploratory_2023_2025.json
```

Next gate: timestamped, licensed option chains for the intended symbol,
verified ex-dividend/assignment treatment, fixed put premium budget and
long-call roll rules, then a genuinely untouched period and paper execution.
Nothing here is enabled in the live strategy path.

References: [OIC long call calendar/diagonal spread](https://www.optionseducation.org/strategies/all-strategies/long-call-calendar-spread-call-horizontal),
[OIC long put](https://www.optionseducation.org/strategies/all-strategies/long-put).
