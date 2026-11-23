#!/usr/bin/env python3
# backtest.py — Gold Agent technical backtest with TP1/TP2/TP3 + partial-exit comparison
# Data: Yahoo Finance GC=F hourly, 2 years
import requests, datetime

UTC = datetime.timezone.utc
ENTRY_T, STRONG_T = 0.35, 0.60
STARTING_CAPITAL = 100.0
RISK_PCT_PER_TRADE = 0.01   # 1% of $100 = $1 risk per trade


# ─── DATA ────────────────────────────────────────────────
def fetch_yahoo(symbol="GC=F", days=730):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"interval": "1h", "range": f"{days}d"}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    r = requests.get(url, params=params, headers=headers, timeout=30)
    j = r.json()
    if "chart" not in j or not j["chart"].get("result"):
        print("yahoo err:", j)
        return []
    result = j["chart"]["result"][0]
    ts = result.get("timestamp", [])
    q  = result["indicators"]["quote"][0]
    opens, highs, lows, closes = q["open"], q["high"], q["low"], q["close"]
    rows = []
    for i in range(len(ts)):
        if None in (opens[i], highs[i], lows[i], closes[i]):
            continue
        rows.append({"t": int(ts[i]), "o": float(opens[i]),
                     "h": float(highs[i]), "l": float(lows[i]),
                     "c": float(closes[i])})
    print(f"fetched {len(rows)} candles ({symbol}, {days}d)")
    if rows:
        s = datetime.datetime.fromtimestamp(rows[0]["t"], UTC).strftime("%Y-%m-%d %H:%M")
        e = datetime.datetime.fromtimestamp(rows[-1]["t"], UTC).strftime("%Y-%m-%d %H:%M")
        print(f"range: {s} → {e}")
    return rows


# ─── INDICATORS ──────────────────────────────────────────
def ema_arr(closes, n):
    k = 2 / (n + 1)
    e = closes[0]
    out = [e]
    for x in closes[1:]:
        e = x * k + e * (1 - k)
        out.append(e)
    return out


def rsi_last(v, n=14):
    if len(v) < n + 1:
        return 50.0
    v = v[-(n + 1):]
    g = l = 0.0
    for i in range(1, n + 1):
        d = v[i] - v[i - 1]
        g += max(d, 0)
        l += max(-d, 0)
    if l == 0:
        return 100.0
    return 100 - 100 / (1 + g / l)


def atr_last(h, l, c, n=14):
    if len(c) < n + 1:
        return 0.0
    trs = [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
           for i in range(1, len(c))]
    trs = trs[-n:]
    return sum(trs) / len(trs)


def score_at(i, e20, e50, e200, closes, highs, lows):
    price = closes[i]
    r = rsi_last(closes[i - 14:i + 1])
    a = atr_last(highs[i - 14:i + 1], lows[i - 14:i + 1], closes[i - 14:i + 1])
    trend = (0.6 if price > e200[i] else -0.6) + (0.4 if e20[i] > e50[i] else -0.4)
    if r >= 72:
        mom = -0.8
    elif r <= 28:
        mom = 0.8
    else:
        mom = (r - 50) / 35.0
    total = 0.35 * trend + 0.15 * mom
    return {"price": price, "e20": e20[i], "atr": a, "total": total}


# ─── BACKTEST ────────────────────────────────────────────
def backtest(rows, hold_max=72):
    closes = [x["c"] for x in rows]
    highs  = [x["h"] for x in rows]
    lows   = [x["l"] for x in rows]
    e20, e50, e200 = ema_arr(closes, 20), ema_arr(closes, 50), ema_arr(closes, 200)

    trades = []
    i = 210
    while i < len(rows) - 1:
        cur  = score_at(i,     e20, e50, e200, closes, highs, lows)
        prev = score_at(i - 1, e20, e50, e200, closes, highs, lows)
        d_now = 1 if cur["total"] >= ENTRY_T else (-1 if cur["total"] <= -ENTRY_T else 0)
        d_old = 1 if prev["total"] >= ENTRY_T else (-1 if prev["total"] <= -ENTRY_T else 0)
        flip     = d_now != 0 and d_now != d_old
        strong   = abs(cur["total"]) >= STRONG_T
        near_ema = abs(cur["price"] - cur["e20"]) <= 0.35 * cur["atr"]

        if not (flip or (strong and near_ema)):
            i += 1
            continue

        price, a = cur["price"], cur["atr"]
        if a <= 0:
            i += 1
            continue

        if cur["total"] > 0:
            sl     = min(min(lows[i - 12:i + 1]), price - 1.2 * a)
            risk   = price - sl
            tps    = [price + risk * k for k in (1, 2, 3)]
            side   = "BUY"
        else:
            sl     = max(max(highs[i - 12:i + 1]), price + 1.2 * a)
            risk   = sl - price
            tps    = [price - risk * k for k in (1, 2, 3)]
            side   = "SELL"

        if risk <= 0:
            i += 1
            continue

        # Walk forward, track MFE (max favorable excursion) before SL/timeout
        mfe_r  = 0.0
        exit_i = None
        exit_px = None
        result = "TIMEOUT"
        timeout_r = None

        for j in range(i + 1, min(i + hold_max + 1, len(rows))):
            row = rows[j]
            if side == "BUY":
                if row["l"] <= sl:       # SL hit first (conservative)
                    exit_i, exit_px, result = j, sl, "SL"
                    break
                fav = (row["h"] - price) / risk
            else:
                if row["h"] >= sl:
                    exit_i, exit_px, result = j, sl, "SL"
                    break
                fav = (price - row["l"]) / risk
            if fav > mfe_r:
                mfe_r = fav

        if exit_i is None:
            exit_i  = min(i + hold_max, len(rows) - 1)
            exit_px = rows[exit_i]["c"]
            result  = "TIMEOUT"
            timeout_r = ((exit_px - price) if side == "BUY"
                         else (price - exit_px)) / risk

        trades.append({
            "entry_i": i, "exit_i": exit_i, "side": side,
            "entry": price, "sl": sl, "tps": tps, "risk": risk,
            "mfe_r": mfe_r, "exit_px": exit_px, "result": result,
            "timeout_r": timeout_r, "score": cur["total"],
        })
        i = exit_i + 1

    return trades


# ─── PnL helpers ─────────────────────────────────────────
def pnl_tp(trade, tp_level):
    """Return PnL in R if we had targeted TP{tp_level}"""
    if trade["mfe_r"] >= tp_level:
        return float(tp_level)          # TP hit before SL
    if trade["result"] == "SL":
        return -1.0                     # SL hit before reaching TP
    return trade["timeout_r"]           # timeout, mark-to-market


def pnl_partial(trade):
    """1/3 at TP1, 1/3 at TP2, 1/3 at TP3; SL on remaining portions"""
    if trade["result"] == "TIMEOUT":
        return trade["timeout_r"]
    m = trade["mfe_r"]
    if m >= 3:
        return (1 + 2 + 3) / 3.0        # +2.00R
    if m >= 2:
        return (1 + 2 - 1) / 3.0        # +0.67R
    if m >= 1:
        return (1 - 1 - 1) / 3.0        # -0.33R
    return -1.0                          # SL before TP1


def max_dd(pnls):
    eq = peak = mdd = 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
    return mdd


def compound(pnls, start=STARTING_CAPITAL, risk_pct=RISK_PCT_PER_TRADE):
    eq = start
    for p in pnls:
        eq *= (1 + risk_pct * p)
        if eq <= 0:
            return 0.0
    return eq


# ─── REPORT ──────────────────────────────────────────────
def report(trades):
    n = len(trades)
    if n == 0:
        print("\n❌ Koi trade nahi")
        return

    reached1 = sum(1 for t in trades if t["mfe_r"] >= 1)
    reached2 = sum(1 for t in trades if t["mfe_r"] >= 2)
    reached3 = sum(1 for t in trades if t["mfe_r"] >= 3)
    exact1   = reached1 - reached2
    exact2   = reached2 - reached3
    exact3   = reached3
    stop_only = sum(1 for t in trades if t["result"] == "SL" and t["mfe_r"] < 1)
    timeout_only = sum(1 for t in trades if t["result"] == "TIMEOUT" and t["mfe_r"] < 1)

    print(f"\n═══ TRADE DISTRIBUTION (n={n}) ═══")
    print(f"  Reached TP1 (ever):       {reached1:5d}  ({100*reached1/n:5.1f}%)")
    print(f"  Reached TP2 (ever):       {reached2:5d}  ({100*reached2/n:5.1f}%)")
    print(f"  Reached TP3 (ever):       {reached3:5d}  ({100*reached3/n:5.1f}%)")
    print(f"  ─────────────────────────────────────────")
    print(f"  Exactly TP1 (reversed):   {exact1:5d}  ({100*exact1/n:5.1f}%)")
    print(f"  Exactly TP2 (reversed):   {exact2:5d}  ({100*exact2/n:5.1f}%)")
    print(f"  Exactly TP3 (full run):   {exact3:5d}  ({100*exact3/n:5.1f}%)")
    print(f"  SL only (no TP ever):     {stop_only:5d}  ({100*stop_only/n:5.1f}%)")
    print(f"  Timeout (no TP):          {timeout_only:5d}  ({100*timeout_only/n:5.1f}%)")

    variants = []
    for k in (1, 2, 3):
        pnls = [pnl_tp(t, k) for t in trades]
        wins = sum(1 for p in pnls if p > 0)
        exp  = sum(pnls) / n
        tot  = sum(pnls)
        dollar = tot * STARTING_CAPITAL * RISK_PCT_PER_TRADE
        final  = compound(pnls)
        variants.append((f"TP{k} exit", wins, exp, tot, dollar, final, pnls))

    pnls_p = [pnl_partial(t) for t in trades]
    wins_p = sum(1 for p in pnls_p if p > 0)
    exp_p  = sum(pnls_p) / n
    tot_p  = sum(pnls_p)
    dollar_p = tot_p * STARTING_CAPITAL * RISK_PCT_PER_TRADE
    final_p  = compound(pnls_p)
    variants.append(("Partial 1/3s", wins_p, exp_p, tot_p, dollar_p, final_p, pnls_p))

    print(f"\n═══ STRATEGY COMPARISON ═══")
    print(f"{'Strategy':<14} {'Wins':>5} {'Win%':>6} {'Exp(R)':>8} "
          f"{'TotalR':>8} {'$ Profit':>10} {'$100→':>8}")
    print("  " + "─" * 66)
    for name, wins, exp, tot, dollar, final, _ in variants:
        print(f"{name:<14} {wins:>5} {100*wins/n:>5.1f}% {exp:>+8.3f} "
              f"{tot:>+8.2f} {dollar:>+10.2f} {final:>8.2f}")

    print(f"\n  '$ Profit'= flat $1 risk/trade on $100 account (1%)")
    print(f"  '$100→'   = compounded 1% risk/trade, starting at $100")

    print(f"\n═══ DRAWDOWNS ═══")
    for name, _, _, _, _, _, pnls in variants:
        dd = max_dd(pnls)
        print(f"  {name:<14} {dd:6.2f}R   (${dd:.2f} on $1 risk per trade)")


if __name__ == "__main__":
    rows = fetch_yahoo(days=730)
    if len(rows) < 300:
        print(f"Not enough data ({len(rows)} candles)")
        raise SystemExit(1)
    trades = backtest(rows)
    report(trades)
    
