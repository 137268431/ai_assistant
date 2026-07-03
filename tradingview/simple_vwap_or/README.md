# Signal Strategy VWAP OR[Glory]

Core-only TradingView strategy for the simplified intraday model:

- Market/sector alignment filters the environment.
- Symbol metadata is stored directly in `SSVOR_Lib_SymbolMeta[Glory].pine`.
- VWAP and opening range are the map.
- Bare K + volume three-stage structure is the only entry trigger.
- Position sizing follows the prior `$5000` notional-cap style with risk quantity safety.
- Alerts keep the TV-primary `entry`, `entry_fill`, `risk_update`, `exit`, and `cancel` payload shape.
- No status table is used. The chart shows an RTH-only VWAP line, ORH/ORL lines, stage markers, blocked-candidate markers, real order labels, and TP/SL management labels.

## Trading Rules

Long structure:

1. Volume breakout through ORH or VWAP.
2. Lower-volume pullback holds ORH/VWAP.
3. Volume reconfirmation breaks the pullback high.
4. Strategy submits a market order for next 2m bar.

Short structure is the inverse through ORL/VWAP.

The script only trades on 2m RTH charts. 09:30-09:45 only builds ORH/ORL; 09:45-11:30 is the primary window; 14:00-15:30 is strict continuation only; 15:45 forces flat.

## Chart Markers

Use `Display profile` to switch label density:

- `PC`: full marker text plus detailed tooltip.
- `Mobile`: larger, shorter labels for phone review.
- `Minimal`: only key entry/block/management marks.

Marker legend:

- `RTH VWAP`: yellow RTH-only VWAP. It resets at 09:30 New York time and does not use extended-hours data; no horizontal track-price dotted line is drawn.
- `VWAP`, `ORH`, `ORL` price tags: latest map levels, shown directly on the chart.
- `GATE`: entry-window and hard-filter state marker. Tooltip shows long/short base gate, score, VWAP/OR, RVOL, RS, and QQQ/SPY market alignment details.
- `L1` / `S1`: volume breakout or breakdown through OR/VWAP.
- `L2` / `S2`: lower-volume pullback or retest holding OR/VWAP.
- `xL1` / `xS1`: breakout/reclaim attempt appeared, but environment, RVOL, wick, or timing blocked stage 1.
- `xL2` / `xS2`: pullback/retest appeared, but volume did not dry up or the map level failed.
- `xL3` / `xS3`: 2m reconfirm appeared, but volume, score, stop, or qty gate blocked the actual order.
- `NO`: compact plot marker for a blocked L3 candidate; use the nearby `xL3` / `xS3` tooltip for details.
- `BUY next` / `SELL next`: real strategy order submitted for the next 2m bar.
- `FILL L` / `FILL S`: strategy position opened, using TradingView fill price.
- `TP1/BE`: 1R touched and stop moved toward breakeven.
- `TP2`, `SL`, `BE`, `TIME`, `EOD`: final exit reason.

Blocked markers are diagnostics only. They do not call `strategy.entry()` and do not emit entry alerts.

`market_not_aligned` is intentional hard filtering. A long needs QQQ above VWAP with 15m trend not down and SPY not opposing; a short needs QQQ below VWAP with 15m trend not up and SPY not opposing. If the stock is strong/weak but QQQ/SPY do not agree, the script diagnoses the setup but does not trade.

On Regular-only TradingView charts the script resets VWAP and the RTH open by date as well as by session start. This keeps the VWAP visible and prevents the prior day from leaking into the current RTH calculation.

## Order Case Example

Example using the default `$5000` notional cap:

- Entry: `$100.00`
- Stop: `$99.20`
- Risk per share: `$0.80`
- Max planned risk: `$75`
- Risk-based qty: `floor(75 * 0.95 / 0.80) = 89` shares before ATR safety buffer
- Notional-cap qty: `floor(5000 / 100.00) = 50` shares
- Final order qty: `50` shares because the `$5000` cap is smaller
- TP1: `$100.80`
- TP2: `$101.60`

On a real signal the chart label shows the same fields: entry, stop, risk/share, qty, notional, TP1, TP2, and score. This is a calculation example only; the strategy does not create fake orders or relax filters to force examples.

## Publish Order

1. Publish `libs/SSVOR_Lib_Format[Glory].pine` as version `2`.
2. Publish `libs/SSVOR_Lib_SymbolMeta[Glory].pine` as version `3`; this adds storage/hardware names such as `WDC`, `SNDK`, `STX`, `NTAP`, and `PSTG` to `XLK`, and strips common exchange prefixes before matching.
3. Publish `libs/SSVOR_Core_Payload[Glory].pine` as version `2`; it imports `SSVOR_Lib_Format_Glory/2`.
4. Publish `core/Signal_Strategy_VWAP_OR_Core[Glory].pine`; it imports `SSVOR_Lib_Format_Glory/2`, `SSVOR_Lib_SymbolMeta_Glory/3`, and `SSVOR_Core_Payload_Glory/2`.

If a library version changes, update the `import o8431/.../<version>` lines in dependent scripts.

## Metadata Maintenance

Add or adjust symbols directly in `SSVOR_Lib_SymbolMeta[Glory].pine`. Unknown symbols default to `metadata_missing` and do not trade unless fallback metadata is explicitly enabled.
