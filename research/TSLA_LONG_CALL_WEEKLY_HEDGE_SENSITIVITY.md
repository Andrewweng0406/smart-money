# TSLA 長 Call 核心：PMCC 權利金週 Put 防守敏感度

## 研究問題

核心長天期 Call **不因週期回調訊號而賣出**；進場後持續持有，接近到期才按固定規則換倉。短 Call 只在 20 日價格阻力附近賣出，收取的權利金作為週 Long Put 防守預算。這輪改變的是 Put 的履約價目標、風險啟動訊號與權利金能否跨槽共用，不是改變長期看多核心。所有變體只用訊號日前已觀察到的股價；買 Put 用次一正常交易日的 optionsDX EOD ask，最多持有 4 個交易日並在到期前平倉。固定成本為每邊 $0.65/張及每股 $0.01 不利滑價。年份僅有來源完整的 2019、2023。

## 主要比較：月首分批買長 Call

表內是 $100,000 帳戶的年末**可清算估值**報酬和每日可清算 NAV 最大回撤，長 Call 年末仍開倉。`下跌日增益`是相對無 Put PMCC，在 **TSLA 現貨較前一交易日下跌日**的每日 NAV 變動差額加總；`上漲日代價`同理。這是樣本內路徑歸因，不是未來保護承諾，也不是 Put 合約單獨的淨利。

| 防守版本 | 2019 報酬 / 回撤 | 2023 報酬 / 回撤 | 2023 已買週 Put | 2023 下跌日增益 | 2023 上漲日代價 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 不買 Put 的 PMCC | +72.32% / 28.99% | +17.10% / 30.97% | 0 | 基準 | 基準 |
| 95% Put，20 日低點跌破，單槽權利金 | +72.32% / 28.99% | +14.46% / 31.07% | 18 | +$1,878 | -$4,523 |
| 95% Put，20 日低點跌破，帳戶共用權利金 | +72.32% / 28.99% | +14.77% / 30.84% | 18 | +$3,216.50 | -$5,551.90 |
| 90% Put，20 日低點跌破，帳戶共用權利金 | +72.32% / 28.99% | +15.29% / 31.43% | 18 | +$832.20 | -$2,643.60 |
| 95% Put，較 20 日高點回落 5%，單槽權利金 | +71.68% / 28.99% | +13.18% / 33.71% | 24 | +$802.20 | -$4,727.40 |
| 98% Put，20 日低點跌破，單槽權利金 | **無效報價** | +14.03% / 32.50% | 12 | +$210 | -$3,279.60 |

2019 的原始 20 日低點訊號下，90% 和 95% 的**單槽或共用權利金池都買到 0 張 Put**。不是全年沒有賣短 Call，而是合格風險訊號與可用權利金的日期錯開；共用池也沒有解決。較早的 5% 回落訊號讓 2019 買到 12 張 Put，卻把帳戶報酬從 +72.32% 降到 +71.68%，最大回撤沒有改善；2023 也從 +17.10% 降到 +13.18%，回撤變深。

95% 共用池另以「短 Call 平倉後已實現淨收入才能買 Put」重跑；四個年份／進場情境的 Put 次數與年末帳戶 NAV，均與毛權利金共用池相同。這只說明**樣本中**未提前動用毛權利金買到額外 Put，不代表毛權利金一般可安全視為已實現利潤。

2023 的 95% 共用池，下跌日增益比單槽高，但上漲日代價也更高，全年淨差為 **-$2,335.40**。最差 5 個交易日的帳戶變動：不買 Put **-16.61%**、90% 共用池 **-17.05%**、95% 共用池 **-17.27%**。這些週 Put 在部分下跌日有緩衝，卻**未改善樣本內最差五日**。90% 更便宜但保護較遠；[OIC 對保護性 Put 履約價的說明](https://www.optionseducation.org/videolibrary/considerations-for-choosing-a-strike-price-for-a-protective-put)也強調成本和覆蓋範圍需要權衡。這裡長 Call + 短 Call + Put 不是股票 collar，Put 也不會精確對沖長 Call 的時間價值或隱含波動率變化。

## 回落 10% 才買長 Call 的敏感度

這組有未進場的槽位，不能把帳戶報酬當成滿倉策略報酬。2019 無 Put 的 PMCC 為 **+66.38%**；20 日低點版本 90%、95% 共用池同為 **+66.38%**，因沒有被資助的 Put；較早 5% 回落、95% 單槽版本 **+65.74%**。2023 無 Put 的 PMCC 為 **+0.03%**；90% 共用池 **-1.17%**，95% 共用池 **-1.54%**，較早訊號 95% 單槽 **-2.58%**。這些結果沒有支持「更早買週 Put 就改善整體收益／回撤」。

## 資料門檻與判斷

98% 保護層級在 2019/08/20 的持倉 Put（2019/08/23 到期、$215 履約價）遇到原始報價 **bid $0.75 > ask $0.74**。報價不合格，不能標記持倉 NAV，所以該年整組判無效；不能以插值或忽略持倉日補出年報酬。2023 單年結果只作資料可用性觀察，**不與跨年完整策略排名**。

權利金「帳戶共用」是**共用防守預算**；實際 Put 買入仍受各長 Call 槽位現金約束，但這些重播沒有因槽位現金不足而擋下 Put。當天先處理各槽的短 Call 現金流，再依長 Call 建立順序配置 Put 防守預算。毛權利金在短 Call 賣出當天可用，之後買回成本才扣除，因此短 Call 可能讓專款預算出現赤字；這不是已實現利潤保證。

研究限制仍大：只有兩個完整期權年份；2019、2023 都不是可靠的熊市驗證；參數組在已有研究之後提出，屬樣本內敏感度，不能挑最高報酬宣稱最佳勝率。[SEC 對回測績效的提醒](https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-bulletins-47)指出回測是假設績效。供應商 EOD 掛單未必可成交，缺 OI，短 Call 阻力是價格代理，不是原始 OI wall；提早指派、保證金、融資、稅與跨年完整生命週期未模型化。

## 機器結果與重跑

- [90% 單槽](tsla_optionsdx_premium_funded_90.json)、[90% 共用池](tsla_optionsdx_pooled_premium_90.json)
- [95% 單槽](tsla_optionsdx_premium_funded_gross.json)、[95% 共用池](tsla_optionsdx_pooled_premium_95.json)
- [95% 共用池、已實現淨收入](tsla_optionsdx_pooled_realized_95.json)
- [95% 提早訊號](tsla_optionsdx_premium_funded_drawdown5.json)
- [98% 2023 單年](tsla_optionsdx_premium_funded_98_2023_only.json)

```sh
.venv/bin/python tsla_optionsdx_continuous_replay.py --put-policy weekly --funding-policy gross --funding-scope account --put-target-ratio 0.95 --output research/tsla_optionsdx_pooled_premium_95.json
.venv/bin/python tsla_optionsdx_continuous_replay.py --put-policy weekly --funding-policy gross --put-target-ratio 0.95 --risk-signal-policy drawdown5 --output research/tsla_optionsdx_premium_funded_drawdown5.json
```
