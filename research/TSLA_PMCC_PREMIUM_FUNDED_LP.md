# TSLA PMCC 權利金支付週 Long Put：資金流重播

## 更正與答案

使用者的原意是：**以 PMCC 賣出短 Call 收到的權利金，支付回調時的週 Long Put 防守**；不是從長 Call 槽位的剩餘現金任意支付 Put。此前的[週 Put 回測](TSLA_WEEKLY_PUT_DEFENSE.md)沒有這項專款限制，因此不是同一策略。這份研究新增每個長 Call 槽位獨立的「短 Call 權利金防守預算」，起始為零，Put 只在風險訊號出現、合格週 Put 有報價、且該槽位預算和實際現金都足夠時買入。不跨槽借款。其他長 Call、短 Call 與週 Put 規則維持原研究設定。

比較兩個資金口徑：

- **毛權利金口徑：**短 Call 賣出當天把收到的現金列入可用防守預算，買回短 Call 時再扣掉成本。這可以提早買 Put，但出售權利金不是已賺到的利潤；之後的買回可能把預算打成負數。
- **已實現淨額口徑：**短 Call 買回後，才把該次賣出收入減買回成本記入防守預算。Put 出售所得可回補預算。遇到短 Call 虧損，預算可變負，此後須先填補缺口才可買 Put。

兩種口徑在本樣本的**實際 Put 交易與帳戶結果相同**，但不代表它們一般等價。

| 年份 | Call 進場 | 不買 Put 的 PMCC | 毛權利金支付週 Put | 已實現淨額支付週 Put | 無專款限制的週 Put | 被權利金支付的 Put 次數 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 2019 | 月首 | +72.32% | +72.32% | +72.32% | +63.27% | 0 |
| 2019 | 回落 10% | +66.38% | +66.38% | +66.38% | +61.18% | 0 |
| 2023 | 月首 | +17.10% | +14.46% | +14.46% | +18.70% | 18 |
| 2023 | 回落 10% | +0.03% | -1.73% | -1.73% | +1.08% | 12 |

2019 月首雖然全年短 Call 賣出共收 **$4,844.10**、買回支出 **$4,204.90**，但在合格回調訊號出現的當下，對應槽位沒有足夠可用權利金，故週 Put **0 張**。這不等於全年從未收過權利金；關鍵是**時間與同槽預算是否碰得上**。2019 月首有 86 次「槽位 × 合格風險訊號」因預算不足而未買 Put；這不是 86 個獨立日期。該年有一個槽位在短 Call 買回後預算最低 **-$68.30**，表示賣 Call 也不保證淨收入。

2023 月首，權利金專款版本共買 18 張週 Put，支付 **$3,461.70**、賣回收 **$816.30**，Put 已實現淨虧 **$2,645.40**；帳戶因此從 PMCC +17.10% 降為 +14.46%，最大每日可清算 NAV 回撤從 **30.97%** 變為 **31.07%**。回落 10% 版本買 12 張，Put 淨虧 **$1,763.60**，帳戶由 +0.03% 變為 -1.73%。無專款限制版本在 2023 看起來較好，是因為它能在尚無短 Call 資金時先買 Put，**不應當作你的策略績效**。

## 判斷與限制

目前「阻力位才賣 Call」與「回調訊號才買 Put」的時序並不穩定匹配：先前有防守需求時，短 Call 權利金可能尚不存在；有權利金時，週 Put 又可能耗損。這是從本回測規則與交易事件得出的**推論**，不是對所有市場環境的結論。週 Put 3–9 DTE、約 95% 股價履約價，以及 20 日價格高低點訊號都仍是研究假設；資料沒有 OI，無法測原始 OI 阻力位。只有完整的 2019、2023 兩年，且年末長 Call 未平倉，無法宣稱勝率或跨年報酬。期權 EOD 報價未必成交，提前指派、保證金、稅和資金成本未模型化。

這個「Call 權利金幫忙支付 Put」的機制與股票持有者使用 collar 的資金概念相似，但**長 Call + 短 Call + 長 Put 不是股票 collar**；[OIC 對股票 collar 的說明](https://prd-web.optionseducation.org/strategies/all-strategies/collar-protective-collar)同時指出短 Call 收入可抵部分 Put 成本，也會限制上漲空間。

機器結果：[毛權利金版](tsla_optionsdx_premium_funded_gross.json)、[已實現淨額版](tsla_optionsdx_premium_funded_realized.json)。重跑：

```sh
.venv/bin/python tsla_optionsdx_continuous_replay.py --put-policy weekly --funding-policy gross --output research/tsla_optionsdx_premium_funded_gross.json
.venv/bin/python tsla_optionsdx_continuous_replay.py --put-policy weekly --funding-policy realized --output research/tsla_optionsdx_premium_funded_realized.json
```
