#!/usr/bin/env python3
"""Washout dip-buyer for cbBTC (agent-fun/bars-replay/v1).

Strategy: long-only, one position at a time, full-size entries.
- Enter when the close sits at least 1.10% below the trailing 60-bar high
  (a sharp washout), with a 5-bar cooldown after each exit.
- Exit at +1.2% take-profit, -1.0% stop-loss, or after 480 bars held.
- Session guard: no new entries once equity falls below 95% of start.
- Late fallback: if no trade has filled by bar 1080, enter on a smaller
  0.70% dip or a close within 0.20% of the trailing 240-bar high, with
  +0.8% / -0.8% exits and a 240-bar hold cap, so the bot still trades
  (and meets the 1-trade bar) on a quiet or steadily rising day.

All arithmetic is integer (basis points against integer prices), no clock,
no network, no randomness, no files: two runs on the same bars produce
byte-identical replies.
"""

import sys
import json

SYMBOL = "CBBTC"

# --- strategy parameters (basis points) ---
LOOKBACK_BARS = 60
ENTRY_DD_BPS = 110          # buy when close is 1.10% below 60-bar high
TAKE_PROFIT_BPS = 120       # sell at +1.2% vs entry fill price
STOP_LOSS_BPS = 100         # sell at -1.0% vs entry fill price
MAX_HOLD_BARS = 480
COOLDOWN_BARS = 5
SESSION_STOP_BPS = 500      # stop entering below 95% of starting equity

# --- late fallback (only if nothing has filled by FALLBACK_BAR) ---
FALLBACK_BAR = 1080
FALLBACK_LOOKBACK_BARS = 240
FALLBACK_DD_BPS = 70        # smaller dip
FALLBACK_NEAR_HIGH_BPS = 20  # or within 0.20% of the 240-bar high
FALLBACK_TP_BPS = 80
FALLBACK_SL_BPS = 80
FALLBACK_HOLD_BARS = 240

MAX_SLIPPAGE_BPS = 150      # 1-min moves on this pool are well under this


def _reply(out, seq, orders):
    out.write(json.dumps({"type": "reply", "seq": seq, "orders": orders},
                         separators=(",", ":")) + "\n")
    out.flush()


def main():
    inp = sys.stdin
    out = sys.stdout

    hello_line = inp.readline()
    if not hello_line:
        return
    hello = json.loads(hello_line)
    if hello.get("type") != "hello":
        return
    start_equity = int(hello["capital"]["quote_units"])

    closes = []            # integer closes, price scale from the bars
    bar_index = -1         # index of the latest bar seen
    cash = 0
    qty = 0
    entry_price = 0
    entry_bar = -1
    cur_tp_bps = TAKE_PROFIT_BPS
    cur_sl_bps = STOP_LOSS_BPS
    cur_hold_bars = MAX_HOLD_BARS
    trades_filled = 0
    cooldown_until = -1
    client_counter = 0

    for line in inp:
        if not line.strip():
            continue
        msg = json.loads(line)
        mtype = msg.get("type")
        if mtype == "done":
            break
        if mtype != "batch":
            continue
        seq = msg["seq"]

        # --- ingest bars (usually exactly one per batch) ---
        for bar in msg.get("bars", []):
            if bar.get("symbol") != SYMBOL:
                continue
            closes.append(int(bar["c"]))
            bar_index += 1

        # --- sync account (authoritative) ---
        account = msg.get("account", {})
        cash = int(account.get("cash_quote_units", cash))
        equity = int(account.get("equity_quote_units", 0))
        qty = 0
        for pos in account.get("positions", []):
            if pos.get("symbol") == SYMBOL:
                qty = int(pos["qty"])

        # --- ingest fills: record entry price and filled-trade count ---
        for fill in msg.get("fills", []):
            if fill.get("status") != "filled":
                continue
            if str(fill.get("client_id", "")).startswith("_flatten_"):
                continue
            trades_filled += 1
            if fill.get("side") == "buy":
                entry_price = int(fill["price"])
                entry_bar = bar_index

        orders = []
        if closes:
            close = closes[-1]
            if qty == 0:
                # --- flat: look for an entry ---
                can_enter = (
                    bar_index >= LOOKBACK_BARS
                    and bar_index > cooldown_until
                    and equity * 10000 >= start_equity * (10000 - SESSION_STOP_BPS)
                    and cash > 0
                )
                if can_enter:
                    use_fallback = trades_filled == 0 and bar_index >= FALLBACK_BAR
                    if use_fallback:
                        hi60 = max(closes[-LOOKBACK_BARS:])
                        hi240 = max(closes[-FALLBACK_LOOKBACK_BARS:])
                        dip = (hi60 - close) * 10000 >= FALLBACK_DD_BPS * hi60
                        near_high = ((hi240 - close) * 10000
                                     <= FALLBACK_NEAR_HIGH_BPS * hi240)
                        if dip or near_high:
                            client_counter += 1
                            orders.append({
                                "side": "buy",
                                "client_id": "buy-%d" % client_counter,
                                "symbol": SYMBOL,
                                "notional_quote_units": str(cash),
                                "max_slippage_bps": MAX_SLIPPAGE_BPS,
                            })
                            cur_tp_bps = FALLBACK_TP_BPS
                            cur_sl_bps = FALLBACK_SL_BPS
                            cur_hold_bars = FALLBACK_HOLD_BARS
                    else:
                        hi = max(closes[-LOOKBACK_BARS:])
                        if (hi - close) * 10000 >= ENTRY_DD_BPS * hi:
                            client_counter += 1
                            orders.append({
                                "side": "buy",
                                "client_id": "buy-%d" % client_counter,
                                "symbol": SYMBOL,
                                "notional_quote_units": str(cash),
                                "max_slippage_bps": MAX_SLIPPAGE_BPS,
                            })
                            cur_tp_bps = TAKE_PROFIT_BPS
                            cur_sl_bps = STOP_LOSS_BPS
                            cur_hold_bars = MAX_HOLD_BARS
            elif entry_price > 0 and entry_bar >= 0:
                # --- holding: check exits against the entry fill price ---
                exit_now = False
                if (close - entry_price) * 10000 >= cur_tp_bps * entry_price:
                    exit_now = True
                elif (entry_price - close) * 10000 >= cur_sl_bps * entry_price:
                    exit_now = True
                elif bar_index - entry_bar >= cur_hold_bars:
                    exit_now = True
                if exit_now:
                    client_counter += 1
                    orders.append({
                        "side": "sell",
                        "client_id": "sell-%d" % client_counter,
                        "symbol": SYMBOL,
                        "qty": str(qty),
                        "max_slippage_bps": MAX_SLIPPAGE_BPS,
                    })
                    cooldown_until = bar_index + COOLDOWN_BARS

        _reply(out, seq, orders)


if __name__ == "__main__":
    main()
