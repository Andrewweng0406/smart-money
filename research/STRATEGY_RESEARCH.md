# Strategy Research and Validation

## Current Evidence

The local audit contains 7 daily snapshots, 6 recommendations, 1 resolved
recommendation, and no historical full option-chain snapshots. Recommendation
19 does not match the entry rule: SOXL spot 121.82, long call 120, long put 112,
debit 30.05 per share. Its expiry breakevens are 81.95 and 150.05. Historical
returns or quotes must not be invented to fill these gaps.

The code changes are experimental safeguards, not a profitable strategy claim.
Production now abstains without an ex-ante volatility forecast. The analysis
caller deliberately does not supply one: no forecast has yet been validated.
It continues saving received full chains for prospective research.

## Candidate Changes

- Numeric net GEX replaces alert text as the regime input. Call-positive and
  put-negative OI-weighted GEX remains a proxy, not actual dealer inventory.
- Valid walls must bracket spot. Wall proximity alone remains a hypothesis;
  model expectation and spread checks can veto its template recommendation.
- Guarded credit spreads use a 15-30 absolute delta band, targeting 20 delta,
  rather than a universal 8-15% distance. Short strikes must be beyond walls.
- Guarded quotes require positive bids, uncrossed markets, and relative spreads
  at most 25%. These thresholds are research defaults, not optimized findings.
- Credit must be below spread width; model profit probability uses breakevens,
  with one common near-ATM volatility for the condor distribution.
- A zero-drift lognormal physical forecast screens expected expiry payoff after
  costs. It ignores skew and jumps and requires forecast provenance. Positive
  modeled expectation alone does not establish positive real-world expectation.
- Missing historical settlement prices stay unresolved. Settlement queries use
  unadjusted closes; corporate actions and adjusted contracts still require
  separate treatment. Default resolver runs wait for the session close.

## Commands

Run the local data audit:

```sh
.venv/bin/python options_research.py --output research/current_data_audit.json
```

After production has collected full chains, export them:

```sh
.venv/bin/python options_research.py --export-frames research/chains.jsonl
```

Compare the legacy template and guarded candidate on authentic historical data:

```sh
.venv/bin/python options_research.py --frames research/chains.jsonl \
  --prices research/closes.csv --marks research/daily_option_marks.csv \
  --split-date 2025-01-01 \
  --capital 100000 --risk-fraction 0.01 --output research/comparison.json
```

Expanding training with separate holdouts:

```sh
.venv/bin/python options_research.py --frames research/chains.jsonl \
  --prices research/closes.csv --marks research/daily_option_marks.csv \
  --fold-boundaries 2024-01-01 2025-01-01 2026-01-01 \
  --capital 100000 --output research/walk_forward.json
```

Historical dates above describe an input contract, not available local data.
An exploratory 2023-2025 SPY comparison using an unverified public Parquet
mirror has since been executed; see `PUBLIC_SPY_BACKTEST.md`. A research-only
`resistance_defense` candidate checks both signal and entry near Call Wall and
sells a defined-risk call spread there; its 2025 fixed-rule holdout was negative.
This is not the user's clarified PMCC strategy. PMCC needs a long call with a
different expiry and separate position accounting; see `PMCC_RESEARCH.md`.
The SPY study does not validate TSLA/SOXL or replace a licensed, timestamped
chain backtest.

## Input Contract

Each JSONL frame contains `symbol`, timezone-aware `observed_at`, `expiry`,
`spot`, `put_wall`, `call_wall`, `alert`, `total_net_gex`, `data_usable`, and
`legs`. Legs contain every `StrikeLegRaw` field: expiry, strike, call/put OI,
IV, volume, bid, and ask. Use standard 100-share contracts, contemporaneous
quotes and consistent underlying prices. Raw yfinance captures are indicative;
receipt time does not prove quote freshness or execution availability.

For guarded replay, signal frames also require annualized `forecast_vol` and
timezone-aware `forecast_asof` no later than signal observation. Forecasts must
be produced using only data available then. The engine does not verify their
training data or silently manufacture forecasts. Settlement CSV columns are
`symbol,date,close,price_basis,contract_multiplier,source,corporate_action_checked`.
The last three contract fields require a documented, checked standard 100-share
contract; `price_basis` must be `as_traded_equity_close`, and the action check
must be `yes`. Split-adjusted Yahoo history is not an accepted settlement source.
Expiry closes must use the contract's then-current share basis.

Replay uses the previous NYSE session's signal and the following session's
quotes, with 25-45 calendar days to expiry. A repeated same-session observation
cannot serve as its own entry signal. Missing sessions or settlements are
excluded and counted. Only one position per symbol/expiry is admitted.

## Capital and Metrics

Baseline limits: 1% maximum loss per trade, 3% per symbol, 10% portfolio risk.
Positions reserve full defined loss plus costs until expiry; sizing uses whole
contracts and settled equity. Entry uses sell bid/buy ask, plus adverse slippage
of $0.01 per leg/share and $0.65 per leg/contract for both entry and terminal
processing. The second fee is a conservative cost allowance, not a modeled
early exit. No naked short positions are generated.

Reports include net P&L, account return, profit factor, win rate, reserved risk,
and P&L per risk-dollar-day. Settled-equity drawdown is explicitly not daily
NAV drawdown. No Sharpe ratio or annualized portfolio return is reported without
daily option marks. Profit factor is null when there are no losses.

With `--marks` the replay checks each open leg's quote on every trading session.
It values a long leg at bid and a short leg at ask, subtracting round-trip costs.
Missing/late quotes invalidate NAV drawdown, annualized return and Sharpe; no
prices are carried forward. Mark CSV fields are `symbol,date,observed_at,expiry,
strike,option_type,bid,ask,quote_source`. This still assumes holding to expiry;
assignment, exercise, financing and stock exposure are not simulated.

Holding intervals crossing a split are purged. Walk-forward folds choose between
fixed candidates using training net P&L per risk-dollar-day, requiring at least
100 completed training trades and positive training P&L; otherwise they select
cash. This count is a screening minimum, not statistical proof. Holdout results
cannot choose the candidate. Each fold resets capital; returns are not compounded.

## Required Next Evidence

The next-stage research is now implemented in `volatility_research.py` and
`historical_options.py`. See `NEXT_STAGE_RESULTS.md` for executed results and
`volatility_validation.json` for the frozen-data expanding-window study.

`historical_options.py` imports Cboe EOD Calcs CSV or ZIP files, taking quotes
and IV from the same 15:45 observation (12:45 on early-close days). It requires
OI, rejects nonstandard roots and indices, and excludes end-of-day volume from
earlier observations. A multi-CSV ZIP requires an explicit member selection.
Pass multiple daily files after `--input` to import a batch; duplicate
symbol/trading-day files are rejected before output files are replaced. Use
`--marks-output` to retain every contract quote for daily valuation, including
expiries outside the entry window.
Forecast enrichment uses the model and calibration fixed before each fold,
with prices available before the snapshot; it does not enable live forecasts.

The downloaded Yahoo `Close` column is dividend-unadjusted but remains
split-adjusted. It must not be used blindly against historic contract strikes.
The new daily-price archive is for volatility research, not an official expiry
settlement archive. Contract adjustments remain an unresolved validation gate.

1. Acquire authentic point-in-time chains, OI/IV and daily option marks across
   multiple regimes; reconcile contract changes, splits, dividends and expiry.
2. Train and calibrate volatility forecasts within each training window. Compare
   trailing realized volatility and implied volatility baselines; account for
   earnings, skew, jumps and forecast uncertainty.
3. Compare fixed strike selection, delta selection, gamma gating, and wall
   gating through prespecified ablations; include cash and a consistently
   capitalized underlying benchmark once daily NAV is available.
4. Evaluate event and block-bootstrap confidence intervals, non-overlapping
   exposures, cost stress, correlated underlyings, and worst regime losses.
5. Test profit-taking and stop rules only with historical daily/intraday option
   marks. Expiry payoff cannot validate an early-exit rule or intratrade drawdown.
6. Require stable untouched holdout and paper execution before enabling a live
   forecast-driven recommendation path. No rule has passed that gate yet.

Sources: [OIC long strangle](https://www.optionseducation.org/strategies/all-strategies/long-strangle-long-combination),
[Cboe historical option quotes](https://datashop.cboe.com/option-quote-intervals),
[Bailey et al., The Probability of Backtest Overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf).
