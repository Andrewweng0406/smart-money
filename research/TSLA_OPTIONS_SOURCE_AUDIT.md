# TSLA Historical Options Data: Source Audit

## Acquired Sample

- Source: [HistoricalData.net free options sample](https://historicaldata.net/options.html), direct [ZIP download](https://historicaldata.net/file/options_sample_2022H2.zip).
- Local raw archive: `/tmp/options_sample_2022H2.zip`; extracted data: `/tmp/options_sample_2022H2`. These are temporary and not committed or redistributed.
- Coverage: 2022-07-01 through 2022-12-30, 127 trading days. The vendor's `verify.py --complete day_by_date` passed all 127 files, including manifest hashes, row counts and structure; ZIP CRC also passed.
- Our [machine-readable audit](tsla_options_sample_audit.json), reproducible with `python tsla_chain_sample_audit.py --output research/tsla_options_sample_audit.json`: ZIP SHA-256 `8517b069f4d41e44f950dc79a3cdab694359b0fae91664190011e39dd8e48b76`.
- TSLA: 625,260 rows, 312,630 calls and 312,630 puts. There are 507,824 rows with finite, noncrossed, nonzero-ask bid/ask quotes; 117,436 lack such quotes. Both sides appear on all 127 dates. No nonstandard TSLA contract roots or duplicate contract rows were found by our parser.
- Jan 2023 calls: 20,521 valid-quote rows; Jan 2024 calls: 20,260. The latter is a longer-dated proxy for the user's long-call core, not the literal next January from August 2022.
- After the 2022-08-25 3-for-1 split, all 89 dates have at least one quoted Jan 2024 deep-ITM call (70-85% of spot; spread <=10% of ask), 45-90 DTE put (90-100% of spot; spread <=25%), and 21-45 DTE OTM short call (spread <=25%). These filters **do not** establish executable fills or profitability. A representative Jan 2024 call, `TSLA240119C00220000`, has valid quotes on 89/89 post-split dates.
- Vendor TSLA underlying closes agree with the existing split-adjusted stock file within $0.005 on all sample dates after adjusting pre-split levels by three. This is a price-level reconciliation, not independent validation of option bid/ask.

## Limits For This Strategy

The sample begins in July, so it cannot test January-March entries or the April-June rise. It offers only one June-August and August-December episode, far too few to estimate a seasonal win rate. The 2022 `quote_time` field is blank; end-of-day standing quotes may be asynchronous across legs and cannot prove spread fills at those prices. The source notes that adjusted option contracts before August 2026 are omitted, so positions carried across Tesla's August 2022 split require special treatment. Use post-split entries for a first limited replay. Account for bid/ask, lot sizing, short-call assignment, ex-dividend risk if relevant, put hedge cost, and long-call remaining value. Do not treat 89 daily observations as 89 independent trades.

## Longer-History Sources

- [Cboe Option EOD Summary](https://datashop.cboe.com/option-eod-summary): official paid historical EOD quotes with selectable symbols/dates. Best candidate for a controlled multiyear TSLA study; confirm selected fields and quote timing before purchase.
- [optionsDX TSLA option chains](https://www.optionsdx.com/product/tsla-option-chains/): advertises TSLA full-chain history including 2016-2023, with bid/ask, IV and Greeks. Check exact package, account requirements and contract-adjustment policy before relying on it.

No paid acquisition or multiyear options-performance claim has been made. The stock-only research remains a stock timing diagnostic, not an options backtest.

A limited post-split cash-flow replay using this sample is documented in [TSLA_OPTIONS_REPLAY_2022H2.md](TSLA_OPTIONS_REPLAY_2022H2.md).
