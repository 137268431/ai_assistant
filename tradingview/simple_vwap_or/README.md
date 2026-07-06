# Signal Strategy VWAP OR[Glory]

Core-only TradingView strategy for the simplified intraday model:

- Auto primary benchmark + broad SPY risk veto filters the environment. The primary benchmark comes from `SSVOR_Lib_SymbolMeta[Glory].pine` (`QQQ` / `SPY` / `IWM`) unless an input override is used.
- Higher-timeframe trend filters the environment; 2m bars are used only for execution confirmation.
- Symbol metadata is stored directly in `SSVOR_Lib_SymbolMeta[Glory].pine`.
- VWAP and opening range are the map.
- Bare K + volume three-stage structure is the normal VWAP/OR entry trigger; a separate trend-day continuation trigger handles clean days when price is already too far from VWAP/OR.
- Position sizing follows the prior `$5000` notional-cap style with risk quantity safety.
- Stops and trailing use multi-timeframe ATR (`2m`, `5m`, `15m`) so tiny 2m ATR does not make risk or runner exits too tight.
- Alerts keep the TV-primary `entry`, `entry_fill`, `risk_update`, `exit`, and `cancel` payload shape.
- No status table is used. By default the chart shows the RTH VWAP line, ORH/ORL lines, session markers, key L1/L2 stage labels, real order labels, and TP/SL/runner management labels. Blocked-candidate markers remain opt-in diagnostics.
- `Enable strategy orders` is the actual TradingView strategy order switch. When it is off, the script keeps diagnostics but will not call `strategy.entry()`.

## Trading Rules

VWAP/OR long structure:

1. Morning volume breakout through ORH or VWAP. Afternoon entries only use ORH continuation.
2. Lower-volume pullback holds ORH/VWAP.
3. A 2m execution bar breaks the pullback high with a strong close and recovered volume.
4. Strategy submits a market order for next 2m bar.

Short structure is the inverse through ORL/VWAP, with afternoon entries only using ORL continuation. The intended model is: 15m/30m decides direction, VWAP/OR decides location, and 2m only decides timing.

Trend-day continuation structure:

1. Primary benchmark, sector, stock relative strength, and VWAP side agree.
2. Price is already outside ORH/ORL and far enough from the VWAP/OR map that the normal L3 setup would be a chase.
3. Price forms a short 2m flag/consolidation with controlled depth.
4. A 2m bar breaks that flag in trend direction with minimum RVOL and submits the next-bar order.

The script only trades on 2m RTH charts. 09:30-09:45 only builds ORH/ORL; 09:45-11:30 is the primary VWAP/OR window; 11:30-14:00 remains closed for normal VWAP/OR entries but can allow trend-day continuation when `Allow trend-day midday entries` is enabled; 14:00-15:30 is continuation only; 15:45 forces flat.

Default filter notes:

- `Market benchmark mode` defaults to `Auto`. The library decides whether the symbol should follow `QQQ`, `SPY`, or `IWM`; `Manual` keeps a direct symbol override. WDC/tech-style names use `QQQ`. SPY is no longer a strict same-direction gate; it is only a broad risk veto when it is clearly opposite by VWAP and 15m trend.
- `Max distance to VWAP/OR (ATR)` defaults to `1.20`, now measured with the risk ATR blend instead of raw 2m ATR, so tiny 2m candles do not falsely mark every move as extended.
- `Max L3 confirm distance to VWAP/OR (ATR)` defaults to `2.20`. L1/L2 must still form near the map, but the final 2m confirmation is allowed to move slightly farther before entry; beyond this cap it remains blocked as a chase.
- `Enable trend-day continuation` defaults to `true`. It does not simply widen the VWAP/OR rule; it requires outside-OR price context, trend alignment, a compact flag, minimum RVOL, and a 2m flag breakout.
- `Require sector beating primary as hard filter` defaults to `false`. The hard sector gate checks whether the sector ETF is on the correct VWAP side; the sector-vs-primary leadership value is still logged and can be made strict by enabling this option.
- `Trend filter mode` defaults to `Balanced`. Balanced keeps the primary benchmark as the hard trend gate: longs need primary 30m up with 15m not down, shorts need primary 30m down with 15m not up. Sector ETF and stock 15m/30m trends contribute the trend score point instead of blocking every setup. `Hard` restores the older six-layer alignment rule; `Off` disables this trend gate for diagnostics.
- L1 keeps the strict `RVOL breakout min` filter. L2 still requires lower-volume pullback/retest. L3 no longer needs another `RVOL >= 1.30`; it needs volume recovery: default `RVOL >= 0.75` plus either `RVOL >= 1.00`, volume at least `1.20x` the L2 pullback/retest bar, or volume at least `1.10x` the prior 2m bar.
- L3 also requires the 2m confirmation candle to close in the correct half of its range by default. Blocks appear as `l3_volume_dead`, `l3_volume_not_recovered`, or `l3_weak_close`.
- The 7-point score uses market, sector, relative strength, VWAP side, VWAP/OR map distance, volume, and trend context. RR/stop quality is checked separately during order validation instead of being a permanent score point.
- Profit management is runner-first: TP1 defaults to 33% at 1R, the stop still moves toward breakeven at 1R, and the rest defaults to `Runner exit mode = Trailing`. The runner starts trailing after `Start runner trail after R`, switches to a chandelier-style trail after `Use chandelier after R`, and only uses a hard target if `Hard final target for trailing runner` is enabled. `Runner exit mode = Fixed target` restores the old fixed final target behavior.
- The 14:00-15:30 window is OR continuation only. Longs must be above ORH and VWAP; shorts must be below ORL and VWAP. VWAP-only afternoon reclaim/reject signals are blocked as `pm_not_continuation`.
- `Pattern TTL bars` defaults to `10`, matching the 20-minute time-stop window.
- The script waits for ATR, VWAP, RVOL MA, primary benchmark/SPY, and sector data before L1/L2/L3 can trigger. 5m/15m ATR use fallbacks while warming up, so the strategy no longer blocks for hours just because the 15m ATR series is new.
- L2 can only be evaluated on a bar after L1, and L3 can only be evaluated on a bar after L2. This prevents same-bar `L1_PASS` plus `L2_BLOCK` contradictions.
- Stop-structure validation uses the actual pullback low / retest high plus ATR buffer. The stop no longer has to be below ORH/VWAP for every long, or above ORL/VWAP for every short, because valid shallow retests can hold above/below the map.

## Chart Markers

Use `Display profile` to switch label density:

- `PC`: compact marker text plus detailed tooltip.
- `Mobile`: larger, shorter labels for phone review.
- `Minimal`: only key entry/block/management marks.

Use `Marker label size` and `Order label size` to override the automatic sizing. `Auto` follows the selected display profile; choose `Tiny`, `Small`, `Normal`, `Large`, or `Huge` when phone tap targets need to be manually adjusted.

Marker legend:

- `RTH VWAP`: yellow RTH-only VWAP. It resets at 09:30 New York time and does not use extended-hours data; no horizontal track-price dotted line is drawn.
- `VWAP`, `ORH`, `ORL` price tags: latest map levels, shown directly on the chart.
- `OR 09:30 ORDER ON/OFF`: RTH open marker. 09:30-09:45 builds ORH/ORL only; no new entries. The same marker also shows whether the actual `strategy.entry()` switch is enabled, so the chart does not stack duplicate 09:30 labels.
- `AM ON 09:45`: primary entry window is open.
- `AM OFF 11:30` or `TC MID`: morning VWAP/OR entry window is closed; if trend-day midday is enabled, continuation entries can still trigger.
- `PM ON 14:00`: afternoon strict-continuation entry window is open when enabled.
- `MANAGE 15:30`: no new entries; manage existing positions only.
- `FLAT 15:45`: force-flat window starts; no overnight hold.
- `GATE`: entry-window and hard-filter state marker. It also shows `MKT L`, `MKT S`, `MKT MIX`, or `MKT DATA?` so the current market gate is visible without a table.
- `L1` / `S1`: volume breakout or breakdown through OR/VWAP. Afternoon L1/S1 only uses ORH/ORL continuation.
- `L2` / `S2`: lower-volume pullback or retest holding OR/VWAP.
- `xL1` / `xS1`: breakout/reclaim attempt appeared, but environment, RVOL, wick, or timing blocked stage 1.
- `xL2` / `xS2`: pullback/retest appeared, but volume did not dry up or the map level failed.
- `xL3` / `xS3`: 2m reconfirm appeared, but volume recovery, close quality, score, stop, or qty gate blocked the actual order.
- `xTC`: trend-day continuation context appeared, but the flag, distance, extension, RVOL, score, stop, or qty gate blocked the actual order.
- `NO`: compact plot marker for a blocked L3/TC candidate; use the nearby `xL3` / `xS3` / `xTC` tooltip for details.
- `BUY next` / `SELL next`: real strategy order submitted for the next 2m bar.
- `FILL L` / `FILL S`: strategy position opened, using TradingView fill price.
- `TP1/BE`: 1R touched and stop moved toward breakeven.
- `TP2`, `TRAIL`, `SL`, `BE`, `TIME`, `EOD`: final exit reason.

Stage markers are visual execution breadcrumbs and default to visible. Blocked markers are diagnostics and default to hidden. Neither calls `strategy.entry()` or emits entry alerts.

`market_not_aligned` is intentional primary-benchmark filtering. A long needs the primary benchmark above VWAP with 15m trend not down; a short needs the primary benchmark below VWAP with 15m trend not up. SPY conflicts are separated into `broad_market_veto` and only block when SPY is clearly opposite by VWAP and 15m trend. If the stock is strong/weak but the primary benchmark does not agree, the script diagnoses the setup but does not trade. Chart labels add the exact short code:

- `P<VWAP`: long is blocked because the primary benchmark is not above VWAP.
- `P>VWAP`: short is blocked because the primary benchmark is not below VWAP.
- `P15Dn`: long is blocked because primary 15m trend is down.
- `P15Up`: short is blocked because primary 15m trend is up.
- `SPYVETO`: SPY broad risk veto is active.
- `DATA?`: primary/SPY/VWAP/trend data is missing.
- `MIX`: more than one market condition is unresolved or mixed.

`trend_not_aligned` depends on `Trend filter mode`. In the default Balanced mode, primary 30m is the hard direction filter and primary 15m must not oppose. Sector ETF and stock 15m/30m are logged as context scores and affect the 7-point setup score. In `Hard` mode, a long needs primary benchmark, sector ETF, and stock 15m/30m trends all up; a short needs all six trends down.

On Regular-only TradingView charts the script resets VWAP and the RTH open by date as well as by session start. This keeps the VWAP visible and prevents the prior day from leaking into the current RTH calculation.

## Pine Debug Logs

Use `Enable Pine debug logs` when chart labels are inconvenient. Logs are written to TradingView's Pine Logs panel and are off by default to avoid noise.

- `Log session/gate events`: RTH open, AM/PM windows, manage-only, force-flat, and `GATE` market state.
- `GATE` logs include `active_side` / `active_base` plus the opposite side, so a short-market day does not get misread from the long-side `market_not_aligned` reason.
- `GATE` and stage logs include `benchmark mode`, `primary`, `broad`, `broad_veto L/S`, and `model=primary_plus_spy_risk_veto`.
- `Log extended skip events`: `EXTENDED_SKIP LONG/SHORT` when direction, sector, relative strength, and VWAP side agree, but price is already too far from VWAP/OR for the normal L3 setup.
- `Log stage pass/block events`: L1/L2/L3/TC pass and block events with reason, market code, VWAP/OR, RVOL, score, relative strength, sector state, and ATR blend.
- Afternoon logs include `pm_cont L/S`; if price is not outside ORH/ORL in the market direction, the block reason is `pm_not_continuation`.
- Trend logs include `trend_mode`, `trend_gate L/S`, context trend scores, `trend_hard L/S`, and primary/sector/stock 15m/30m direction.
- L3 logs include `l3_rvol`, `l3_vs_l2`, `l3_vs_prev`, `l3_volume_ok`, `l3_volume_reason`, `l3_close_loc`, and `l3_close_ok`.
- TC logs include `tc_base`, map distance, VWAP extension, flag range/depth, breakout confirmation, and continuation RVOL.
- `Log order lifecycle events`: order blocked/submitted, fill, cancel, TP1/breakeven, runner trail updates, close request, and final position close.

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
- TP1 exit pct: `33%`
- Runner reference: default trailing runner starts after 2R; if `Runner exit mode = Fixed target`, the fixed target is `$102.40` at the default `3R`

On a real signal the chart label shows the compact order case, while the tooltip shows entry, stop, risk/share, qty, notional, TP1, runner mode/final reference, and score. This is a calculation example only; the strategy does not create fake orders or relax filters to force examples.

Order lifecycle logs also show `actual_risk_dollars`, `expected_1R_dollars`, `expected_final_dollars`, `final_target_r`, and `notional_cap_binds`. With the default `$5000` cap, high-priced stocks can use far less than the `$75` risk ceiling because share count is capped by notional first.

## Publish Order

1. Publish `libs/SSVOR_Lib_Format[Glory].pine` as version `2`.
2. Publish `libs/SSVOR_Lib_SymbolMeta[Glory].pine` as version `3`; the core imports `SSVOR_Lib_SymbolMeta_Glory/3`.
3. Publish `libs/SSVOR_Core_Payload[Glory].pine` as version `2`; it imports `SSVOR_Lib_Format_Glory/2`.
4. Publish `core/Signal_Strategy_VWAP_OR_Core[Glory].pine`; it imports `SSVOR_Lib_Format_Glory/2`, `SSVOR_Lib_SymbolMeta_Glory/3`, and `SSVOR_Core_Payload_Glory/2`.

If a library version changes, update the `import o8431/.../<version>` lines in dependent scripts. Only point the core to versions that TradingView already shows as published.

## Metadata Maintenance

Add or adjust symbols directly in `SSVOR_Lib_SymbolMeta[Glory].pine`. Unknown symbols default to `metadata_missing` and do not trade unless fallback metadata is explicitly enabled.

The metadata lib maps `relativeBenchmark()` to `QQQ`, `SPY`, or `IWM`. Core now uses SymbolMeta `/3`, which includes the expanded daily-watchlist mapping.
