# TSLA Options Replay: Post-Split 2022 H2

## Bottom Line

This is a **single-episode, exploratory cash-flow replay**, not a seasonal win-rate estimate or executable-trade record. It begins on 2022-08-31, after Tesla's 3-for-1 split, and liquidates on 2022-12-30 (85 sessions). This period was a severe TSLA decline. The [audited free historical chain](TSLA_OPTIONS_SOURCE_AUDIT.md) is the option source; it has no 2022 quote timestamps. We use each date's standing bid/ask as a hypothetical date-bucket execution and never fill a missing held-contract mark.

The fixed resistance rule generated **zero short-call entries**. Four conditional puts cushioned the decline in this particular episode. Neither observation proves future profitability or a high win rate.

## Prespecified Replay Rules

- Each variant starts with $100,000 cash and buys **one** TSLA call on 2022-08-31, strike 70-85% of spot and nearest 80% among quotes with <=10% relative spread. Three expiry policies are compared to expose the ambiguity in "next January": Jan 2023 (142 DTE), Jan 2024 (506 DTE), and a roughly 540-DTE long call (562 DTE, Mar 2024). Jan 2023 is not a long-duration PMCC core from late August 2022.
- Resistance is the maximum-open-interest call strike between spot and 5% above spot, among 21-45 DTE options. Only when the prior close is within 1% below that wall **and** the next session's spot remains within 1% below it can a 21-45 DTE short call be written at/above the wall. Buy it back after five sessions. This is an OI proxy, not proven resistance.
- A put-risk signal occurs when a session's spot closes below the **previous** session's 21-45 DTE maximum-OI put wall within 5% below spot. Buy a 45-90 DTE put on the following session, strike near 95% of signal spot and <=25% relative spread. Close after 20 sessions. This is conditional insurance, not permanent protection.
- Long buys and short covers pay ask; long sales and short writes receive bid. Every leg/side also incurs $0.65 commission and $0.01/share adverse slippage. One contract per leg, $100 multiplier, zero cash yield. Year-end liquidation uses the opposite side of the quoted market.

## Results

Returns and drawdowns below use the whole $100,000 account; a large cash balance is idle. These are **not option-debit returns**. PMCC has the same result as the bare long call because no short call was written.

| Long-call expiry | Entry debit | Bare long call: account return / max DD | PMCC + conditional put: account return / max DD | Put overlay net P&L |
| --- | ---: | ---: | ---: | ---: |
| Jan 2023 | $7,551.65 | -7.544% / 9.827% | +0.198% / 4.102% | +$7,741.80 |
| Jan 2024 | $10,871.65 | -9.773% / 12.269% | -2.031% / 5.075% | +$7,741.80 |
| Mar 2024 (~540 DTE) | $11,431.65 | -10.073% / 12.635% | -2.331% / 5.271% | +$7,741.80 |

The five prior-session resistance signals produced zero entry-day signals still within the 1% band. Thus the short-call overlay P&L is $0 in all variants. Four put purchases occurred on 2022-09-14, 10-12, 11-10 and 12-13. Across those four cycles, debits totaled $6,506.60 and exit credits totaled $14,248.40; the $7,741.80 difference reconciles exactly to the account-level Put overlay. This outcome benefited from the observed 2022 downtrend; it is not an expected-return estimate. Idle cash returning 0% would have beaten the Jan 2024 long-call variants in this period.

## What This Does And Does Not Test

This replay does not test January-March purchases, April-June gains, repeated summer dip buying, or a multiyear seasonal win rate. The literal Jan 2023 call and Jan 2024 long-duration call are different exposures; choosing whichever looks best after seeing 2022 would be overfitting. The 85 daily NAV marks are not 85 independent trades. 2022 vendor quotes are not synchronized snapshots, and neither quote timestamps nor actual fillability are known. American short-call early assignment, financing, broker margin and tax are not simulated. The short-call leg had no entries, so this is **not** evidence that a working PMCC systematically earns premium near resistance.

No live strategy path was changed. No paid data was purchased.

## Next Major Action

Obtain a *multiyear, TSLA-specific* chain with a consistent 15:45 snapshot or timestamped bid/ask (ideally 2016-2025, including corporate-action handling). [Cboe Option EOD Summary](https://datashop.cboe.com/option-eod-summary) supports symbol/date selection and reports 15:45 and EOD quote fields; verify exact history, adjusted roots and license before ordering. Independently check [optionsDX TSLA yearly packages](https://www.optionsdx.com/product/tsla-option-chains/) for completeness, price and split handling. A paid order needs explicit user approval.

Then freeze one strategy specification **before** seeing further years: seasonal entry dates and tranches, long-call maturity/delta, resistance definition and short-call holding/assignment rules, put risk trigger and premium budget, roll/exit rules, fill/fee assumptions, and capital accounting. Replay each year, compare long-call-only/PMCC/PMCC+Put/cash/stock baselines, report trade count and participation alongside return/drawdown, and reserve untouched years or forward paper trading for validation. Do not optimize thresholds to maximize the same sample's historical win rate.

## Reproduce

The raw licensed sample is local under `/tmp/options_sample_2022H2` and is not in Git. See the source-audit report for its ZIP hash and verification. Run:

```sh
.venv/bin/python tsla_options_sample_replay.py --long-expiry jan2023 --output research/tsla_options_replay_jan2023.json
.venv/bin/python tsla_options_sample_replay.py --long-expiry jan2024 --output research/tsla_options_replay_jan2024.json
.venv/bin/python tsla_options_sample_replay.py --long-expiry nearest_540d --output research/tsla_options_replay_nearest_540d.json
```
