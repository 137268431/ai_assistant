# Signal Strategy VWAP OR[Glory]

Core-only TradingView strategy for the simplified intraday model:

- Market/sector alignment filters the environment.
- Symbol metadata is stored directly in `SSVOR_Lib_SymbolMeta[Glory].pine`.
- VWAP and opening range are the map.
- Bare K + volume three-stage structure is the only entry trigger.
- Position sizing follows the prior `$5000` notional-cap style with risk quantity safety.
- Alerts keep the TV-primary `entry`, `entry_fill`, `risk_update`, `exit`, and `cancel` payload shape.

## Trading Rules

Long structure:

1. Volume breakout through ORH or VWAP.
2. Lower-volume pullback holds ORH/VWAP.
3. Volume reconfirmation breaks the pullback high.
4. Strategy submits a market order for next 2m bar.

Short structure is the inverse through ORL/VWAP.

The script only trades on 2m RTH charts. 09:30-09:45 only builds ORH/ORL; 09:45-11:30 is the primary window; 14:00-15:30 is strict continuation only; 15:45 forces flat.

## Publish Order

1. Publish `libs/SSVOR_Lib_Format[Glory].pine`.
2. Publish `libs/SSVOR_Lib_SymbolMeta[Glory].pine`.
3. Publish `libs/SSVOR_Core_Payload[Glory].pine`.
4. Publish `core/Signal_Strategy_VWAP_OR_Core[Glory].pine`.

If a library version changes, update the `import o8431/.../<version>` lines in dependent scripts.

## Metadata Maintenance

Add or adjust symbols directly in `SSVOR_Lib_SymbolMeta[Glory].pine`. Unknown symbols default to `metadata_missing` and do not trade unless fallback metadata is explicitly enabled.
