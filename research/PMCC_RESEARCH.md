# Long Call + PMCC + Conditional Long Put Research Contract

PMCC (Poor Man's Covered Call) is a **long call diagonal**: own a longer-dated,
lower-strike call and sell a shorter-dated, higher-strike call against it.
The requested long put is a separately purchased third leg, not a free hedge
or a textbook protective put on stock. It may have its own expiry and can
expire before the core call; that coverage gap must be visible in results.
The previous Bear Call Spread research is not a PMCC backtest and must not be
used to accept or reject this strategy.

## Required Position Definition

The long call is the core bullish exposure. Before calculating returns, supply
either (a) the actual long-call contract, acquisition timestamp, fill price,
quantity and costs, or (b) a prespecified rule for when/how the system buys a
long call. Without that, selecting a favorable LEAPS entry after seeing prices
would introduce lookahead bias. The short call can then be considered only
near a separately measured resistance level. Its entry and buyback quotes
must be contemporaneous with the long-call marks.

`pmcc_research.py` is a research-only multi-expiry ledger. It charges entry and
exit costs separately, keeps the long call alive through each short-call
cycle, checks exact contracts and chronological quote timestamps, and values
the portfolio at liquidation bid/ask. A same-underlying put can be bought after
the call, held while short calls are rolled, and separately closed or replaced.
It permits a new short call or put only after the previous contract of that
type is closed. It rejects held contracts at or after expiration rather than
pretending that assignment or exercise is a harmless cash settlement. The long
call must also be closed before its own expiry.

For the conditional-put candidate, `put_wall_breach_signal` uses a completed,
timestamped spot/Put Wall observation and requires execution later than that
observation. The frozen candidate is **spot below Put Wall at the prior signal
time, buy a put at the next executable quote**. This is a hypothesis, not a
validated support break or live instruction. Put strike, DTE, exit/roll,
maximum premium budget and data-quality filters still need prespecified rules
before a historical performance claim. The ledger allows an externally
specified put purchase; its execution API alone does not authorize a signal.

## Backtest Gates

1. Obtain the actual long-call trade or freeze a long-entry rule before replay.
   Test long calls and short calls on their **separate** expiries, including
   all holding-period daily quotes.
2. Define the resistance signal independently of the option sale; record the
   signal before the execution quote. Do not use a closing value to claim a
   fill earlier that day.
3. Specify short-call buyback/roll, breach and ex-dividend rules. Specify put
   strike, DTE, risk trigger, exit/roll and premium budget. The ledger
   does not model assignment, stock delivery, financing or broker margin;
   historical short-call returns cannot be approved without those checks.
4. Compare long call alone, PMCC, PMCC plus conditional put, and cash on equal
   starting capital and daily NAV. Report drawdown, fees, put premium drag,
   downside mitigation and uninvested cash. Freeze the rules before an
   untouched period or prospective paper-trading test.

The public SPY mirror has daily call and put quotes but unverifiable exact
quote timing and a known contaminated holiday. Its 2023-2025 three-leg
diagnostic is in `PMCC_SPY_REPLAY.md`; those results are not proof of
executable performance. The local Cboe sample has only one day. No validated
PMCC or three-leg historical return exists, and no live path is enabled.

Primary reference: [OIC long call calendar/diagonal spread](https://www.optionseducation.org/strategies/all-strategies/long-call-calendar-spread-call-horizontal).
The [OIC protective put](https://www.optionseducation.org/strategies/all-strategies/protective-put-married-put)
definition assumes long stock; our LEAPS-call combination is not that position.
