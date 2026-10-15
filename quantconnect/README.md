# TSLA PMCC cloud data gate and quote replay

## LEAN order-based backtest

Use `tsla_pmcc_orders.py` as the Cloud project's `main.py` for a genuine order
backtest. It submits next-session market-on-open option orders and records fills
in the backtest **Orders**, **Holdings**, and **Strategy Equity** views. Its
default parameters are also `year=2021`, `mode=funded`, `resistance=oi`.
Run `long`, `pmcc`, and `funded` separately, with the same year and resistance
setting, and compare their actual portfolio results. Read the backtest **Logs**
for `DATA_PROGRESS`, `FILL`, `ORDER_FAILED`, and `TSLA_PMCC_ORDERS_RESULT`.
The bottom Cloud Terminal shows job deployment, not necessarily these results.
An already-running job does not change when `main.py` is edited.

The order script seeks six monthly long Call entries near 20-session lows, or
late in the month. It sells a short Call only within 1% below a 21-45 DTE
open-interest wall (or 20-close high in `resistance=price` mode). A weekly Put
is opened on a close below the previous 20 closes only if the **same slot** has
enough realized, net short-Call premium. Long Calls roll when under 120 DTE;
the year-end portfolio keeps any remaining long exposure. Each slot owns at
most one of each leg, and orders are serialized per slot. Rejected orders and
position mismatches are logged; an assignment-induced mismatch aborts the run.
These rules are a testable interpretation of the proposed strategy, not a
validated optimal schedule or a live execution recommendation. Market-on-open
option fills, spread, liquidity, assignment, corporate actions, margin, and
taxes still require review in the completed QC backtest. This workspace cannot
launch the user's private Cloud run, so no performance result is claimed here.

## Quote replay (diagnostic only)

Copy `tsla_pmcc_research.py` into a QuantConnect Cloud Python project as `main.py`.
The default project parameters are `year=2021`, `mode=funded`, `resistance=oi`.
Run three comparable backtests with `mode=long`, `mode=pmcc`, and `mode=funded`.
Use `resistance=price` only as a separate price-high proxy experiment, not as an
OI-wall result. Repeat on 2020, 2022, 2023, 2024 and 2025 after 2021 passes.

The `TSLA_PMCC_RESEARCH_RESULT` log line and `Research NAV` chart are the outputs.
Custom runtime statistics show `TSLA sessions`, `Quote NAV`, and open long Call
count during execution; these are distinct from LEAN's built-in portfolio
statistics. `TSLA_DATA_PROGRESS` appears in the backtest **Logs** tab, not the
Cloud Terminal. Only replace the cloud script after reading the completed
backtest's Logs, because code changes do not update an already-running job.
QuantConnect's built-in portfolio statistics are **not** this strategy's returns:
the script makes no LEAN orders. It replays long Call, short Call and weekly Put
cash flows at observed daily bid/ask with $0.01/share adverse slippage and
$0.65/contract per side. Contract decisions use day T's completed chain and
stock close; hypothetical execution uses day T+1's chain. A missing held quote
invalidates the run rather than being forward-filled or model-priced. Six equal
$16,666.67 slots attempt long Call entry in Jan/Feb/Mar/Jun/Jul/Aug, roll near
120 DTE, and retain open positions in year-end quote NAV. Short Calls are opened
only within 1% below a resistance level; `oi` sums Call open interest across
21-45 DTE expirations at each strike. Risk is a close below the previous 20
closes; weekly Puts target 95% of spot, 3-9 DTE at execution, and are bought
only with **realized net short-Call premium from the same slot**. No cross-slot
funding, put-profit reinvestment or discretionary buying is assumed.

This is a first data and rule check, not a broker fill/assignment simulation.
EOD quotes may be stale or unfillable; corporate-action-adjusted contracts,
early assignment, margin, financing and taxes need explicit follow-up tests.
Compare only runs with valid daily coverage and inspect `counts` for missing
execution quotes, missing OI walls, missed rolls, and forced long exits. A
cloud run has not yet been performed from this workspace; account access is
required. Do not report these synthetic quote returns as realized performance.

Sources: [QuantConnect AlgoSeek US equity options](https://www.quantconnect.com/docs/v2/writing-algorithms/datasets/algoseek/us-equity-options),
[option universes](https://www.quantconnect.com/docs/v2/writing-algorithms/universes/equity-options),
and [time modeling](https://www.quantconnect.com/docs/v2/writing-algorithms/key-concepts/time-modeling/timeslices).
