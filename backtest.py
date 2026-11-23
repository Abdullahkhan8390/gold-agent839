#!/usr/bin/env python3
# backtest.py — technical-only backtest of Gold Agent entry logic
# Data: Yahoo Finance GC=F hourly, 2 years
import requests, datetime

UTC = datetime.timezone.utc
ENTRY_T, STRONG_T = 0.35, 0.60


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
    q = result["indicators"]["quote"][0]
    opens, highs, lows, closes = q["open"], q["high"], q["low"], q["close"]
    rows = []
    for i in range(len(ts)):
        if None in (opens[i], highs[i], lows[i], closes[i]):
            continue
        rows.append({"t": int(ts[i]), "o": float(opens[i]),
                     "h": float(highs[i]), "l": float(lows[i]),
                     "c": float(closes[i])})
    print(f"fetched {len(rows)} candles ({symbol}, {days}d, 1 request)")
    if rows:
        start = datetime.datetime.fromtimestamp(rows[0]["t"], UTC).strftime("%Y-%m-%d %H:%M")
        end   = datetime.datetime.fromtimestamp(rows[-1]["t"], UTC).strftime("%Y-%m-%d %H:%M")
        print(f"range: {start} → {end}")
    return rows


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
    return {"price": price, "e20": e20[i], "atr": a,
            "trend": trend, "mom": mom, "total": total}


def backtest(rows, hold_max=72):
    closes = [x["c"] for x in rows]
    highs  = [x["h"] for x in rows]
    lows   = [x["l"] for x in rows]
    e20, e50, e200 = ema_arr(closes, 20), ema_arr(closes, 50), ema_arr(closes, 200)
    trades, open_trade = [], None
    i = 210
    while i < len(rows):
        if open_trade:
            row = rows[i]
            side = open_trade["side"]
            exit_px, result = None, None
            if side == "BUY":
                if row["l"] <= open_trade["sl"]:
                    exit_px, result = open_trade["sl"], "SL"
                elif row["h"] >= open_trade["tps"][0]:
                    exit_px, result = open_trade["tps"][0], "TP1"
            else:
                if row["h"] >= open_trade["sl"]:
                    exit_px, result = open_trade["sl"], "SL"
                elif row["l"] <= open_trade["tps"][0]:
                    exit_px, result = open_trade["tps"][0], "TP1"
            if exit_px is None and i - open_trade["entry_i"] >= hold_max:
                exit_px, result = row["c"], "TIMEOUT"
            if exit_px is not None:
                trades.append({**open_trade, "exit_px": exit_px, "result": result})
                open_trade = None
            i += 1
            continue

        cur  = score_at(i,     e20, e50, e200, closes, highs, lows)
        prev = score_at(i - 1, e20, e50, e200, closes, highs, lows)
        d_now = 1 if cur["total"] >= ENTRY_T else (-1 if cur["total"] <= -ENTRY_T else 0)
        d_old = 1 if prev["total"] >= ENTRY_T else (-1 if prev["total"] <= -ENTRY_T else 0)
        flip     = d_now != 0 and d_now != d_old
        strong   = abs(cur["total"]) >= STRONG_T
        near_ema = abs(cur["price"] - cur["e20"]) <= 0.35 * cur["atr"]

        if flip or (strong and near_ema):
            price, a = cur["price"], cur["atr"]
            if a <= 0:
                i += 1
                continue
            if cur["total"] > 0:
                sl   = min(min(lows[i - 12:i + 1]), price - 1.2 * a)
                risk = price - sl
                tps  = [price + risk * k for k in (1, 2, 3)]
                side = "BUY"
            else:
                sl   = max(max(highs[i - 12:i + 1]), price + 1.2 * a)
                risk = sl - price
                tps  = [price - risk * k for k in (1, 2, 3)]
                side = "SELL"
            if risk > 0:
                open_trade = {"entry_i": i, "side": side, "entry": price,
                              "sl": sl, "tps": tps, "risk": risk,
                              "score": cur["total"]}
        i += 1
    return trades


def report(trades):
    if not trades:
        print("\n❌ Koi trade nahi — signal conditions kabhi poori nahi hui")
        return
    n = len(trades)
    wins  = [t for t in trades if t["result"] == "TP1"]
    sls   = [t for t in trades if t["result"] == "SL"]
    touts = [t for t in trades if t["result"] == "TIMEOUT"]

    print(f"\n══ BACKTEST RESULTS ══")
    print(f"Total trades:  {n}")
    print(f"Wins (TP1):    {len(wins)}  ({100*len(wins)/n:.1f}%)")
    print(f"Stop loss:     {len(sls)}  ({100*len(sls)/n:.1f}%)")
    print(f"Timeout:       {len(touts)}  ({100*len(touts)/n:.1f}%)")

    r_seq = []
    for t in trades:
        if t["result"] == "TP1":
            r = 1.0
        elif t["result"] == "SL":
            r = -1.0
        else:
            diff = ((t["exit_px"] - t["entry"]) if t["side"] == "BUY"
                    else (t["entry"] - t["exit_px"]))
            r = diff / t["risk"]
        r_seq.append(r)

    total_r = sum(r_seq)
    avg_r   = total_r / n
    eq = peak = mdd = 0.0
    for r in r_seq:
        eq += r
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)

    print(f"\nTotal R:       {total_r:+.2f}")
    print(f"Expectancy:    {avg_r:+.3f}R per trade")
    print(f"Max drawdown:  {mdd:.2f}R")
    print(f"Best single:   {max(r_seq):+.2f}R")
    print(f"Worst single:  {min(r_seq):+.2f}R")

    if avg_r > 0.05:
        print("\n✅ Positive expectancy — technical engine mein edge hai")
    elif avg_r > -0.05:
        print("\n🟡 Neutral — edge nahi, breakeven ke qareeb")
    else:
        print("\n🔴 Negative expectancy — technical engine ko tune karna hoga")


if __name__ == "__main__":
    rows = fetch_yahoo(days=730)
    if len(rows) < 300:
        print(f"Not enough data ({len(rows)} candles, need 300+)")
        raise SystemExit(1)
    trades = backtest(rows)
    report(trades)
                   
