#!/usr/bin/env python3
# signals.py — Gold multi-timeframe signal engine (ENHANCED SCALP EDITION)
# More setups · tighter SL · fixed 1:2 RR · sweep / mean-reversion / momentum triggers
import os
import datetime
import requests

UTC = datetime.timezone.utc
PKT = datetime.timezone(datetime.timedelta(hours=5))
SYMBOL = "GC=F"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}

# Tier SL = ATR multiple of that timeframe. TP always 1R & 2R.
# Hard cap: risk must be <= max_sl USD per oz. 100 pips = $10.
TIER_CFG = {
    "scalp":    {"sl": 0.55, "max_sl": 10.0, "hold": "30m–4h"},
    "fast":     {"sl": 0.70, "max_sl": 10.0, "hold": "1–6 hours"},
    "intraday": {"sl": 0.90, "max_sl": 12.0, "hold": "4–24 hours"},
}
PIP = 0.10  # $0.10 per oz on gold


def fetch(interval, rng):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{SYMBOL}"
    try:
        r = requests.get(url, params={"interval": interval, "range": rng},
                         headers=UA, timeout=30)
        j = r.json()
    except Exception as e:
        print(f"[data] {interval}: {e}")
        return []
    res = (j.get("chart") or {}).get("result")
    if not res:
        return []
    res = res[0]
    ts = res.get("timestamp") or []
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    o, h, l, c = q.get("open", []), q.get("high", []), q.get("low", []), q.get("close", [])
    rows = []
    for i in range(len(ts)):
        if i >= len(c):
            break
        if None in (o[i], h[i], l[i], c[i]):
            continue
        rows.append({"t": int(ts[i]), "o": float(o[i]), "h": float(h[i]),
                     "l": float(l[i]), "c": float(c[i])})
    return rows


def bucketed(rows, hours):
    sec = hours * 3600
    buckets = {}
    for r in rows:
        k = r["t"] // sec
        b = buckets.get(k)
        if b is None:
            buckets[k] = {"t": r["t"], "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"]}
        else:
            b["h"] = max(b["h"], r["h"])
            b["l"] = min(b["l"], r["l"])
            b["c"] = r["c"]
    return [buckets[k] for k in sorted(buckets)]


def ema(vals, n):
    n = max(2, min(n, len(vals)))
    k = 2.0 / (n + 1)
    e = vals[0]
    out = [e]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def rsi_last(vals, n=14):
    if len(vals) < n + 1:
        return 50.0
    seg = vals[-(n + 1):]
    g = l = 0.0
    for i in range(1, len(seg)):
        d = seg[i] - seg[i - 1]
        if d > 0:
            g += d
        else:
            l += -d
    if l == 0:
        return 100.0
    return 100 - 100 / (1 + g / l)


def atr_last(highs, lows, closes, n=14):
    if len(closes) < n + 1:
        return 0.0
    trs = []
    for i in range(len(closes) - n, len(closes)):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    return sum(trs) / len(trs)


def state(rows, label):
    if len(rows) < 60:
        return None
    closes = [r["c"] for r in rows]
    highs = [r["h"] for r in rows]
    lows = [r["l"] for r in rows]
    price = closes[-1]
    e20 = ema(closes, 20)[-1]
    e50 = ema(closes, 50)[-1]
    e200 = ema(closes, 200)[-1]
    rv = rsi_last(closes)
    a = atr_last(highs, lows, closes)
    if a <= 0:
        a = max(price * 0.0005, 0.01)
    score = 0
    score += 1 if price > e200 else -1
    score += 1 if e20 > e50 else -1
    score += 1 if price > e20 else -1
    return {
        "label": label, "price": price, "e20": e20, "e50": e50, "e200": e200,
        "rsi": rv, "atr": a, "bias": score / 3.0,
        "hi_prev": max(highs[-21:-1]) if len(highs) > 21 else max(highs),
        "lo_prev": min(lows[-21:-1]) if len(lows) > 21 else min(lows),
        "last_t": rows[-1]["t"], "_bars": rows,
    }


def build(kind, tf, side, why):
    cfg = TIER_CFG[kind]
    price, a = tf["price"], tf["atr"]
    if side == "LONG":
        sl_atr = price - cfg["sl"] * a
        sl_struct = tf["lo_prev"] - 0.10 * a
        sl = max(sl_atr, sl_struct) if (price - sl_struct) <= 2.0 * a else sl_atr
        risk = price - sl
        if risk <= 0 or risk > cfg["max_sl"]:
            return None
        tp1 = price + risk * 1.0
        tp2 = price + risk * 2.0
    else:
        sl_atr = price + cfg["sl"] * a
        sl_struct = tf["hi_prev"] + 0.10 * a
        sl = min(sl_atr, sl_struct) if (sl_struct - price) <= 2.0 * a else sl_atr
        risk = sl - price
        if risk <= 0 or risk > cfg["max_sl"]:
            return None
        tp1 = price - risk * 1.0
        tp2 = price - risk * 2.0
    return {"kind": kind, "side": side, "entry": price, "sl": sl,
            "tp1": tp1, "tp2": tp2, "risk": risk,
            "why": why, "hold": cfg["hold"], "tf": tf["label"]}


def find_scalp_signals(m, hh):
    if not m or not m.get("_bars") or len(m["_bars"]) < 21:
        return []
    a15, price, rsi = m["atr"], m["price"], m["rsi"]
    bars = m["_bars"]; last = bars[-1]; sigs = []

    pull_up = 0 <= price - m["e20"] <= 0.7 * a15
    pull_dn = 0 <= m["e20"] - price <= 0.7 * a15
    brk_up = price >= m["hi_prev"]
    brk_dn = price <= m["lo_prev"]

    if hh["bias"] > 0 and rsi < 72 and pull_up:
        sigs.append(("LONG", "H1 up + M15 pullback to EMA20"))
    if hh["bias"] < 0 and rsi > 28 and pull_dn:
        sigs.append(("SHORT", "H1 down + M15 rejecting EMA20"))
    if hh["bias"] >= 0 and brk_up and rsi < 78:
        sigs.append(("LONG", "M15 breakout of 20-bar high"))
    if hh["bias"] <= 0 and brk_dn and rsi > 22:
        sigs.append(("SHORT", "M15 breakdown of 20-bar low"))

    recent_low = min(b["l"] for b in bars[-21:-1])
    recent_high = max(b["h"] for b in bars[-21:-1])
    if last["l"] < recent_low and last["c"] > recent_low and hh["bias"] >= 0:
        sigs.append(("LONG", f"M15 sweep {recent_low:.2f} + reclaim"))
    if last["h"] > recent_high and last["c"] < recent_high and hh["bias"] <= 0:
        sigs.append(("SHORT", f"M15 sweep {recent_high:.2f} + rejection"))

    if rsi < 25 and price > m["e200"] and hh["bias"] >= 0:
        sigs.append(("LONG", f"M15 oversold bounce (RSI {rsi:.0f})"))
    if rsi > 75 and price < m["e200"] and hh["bias"] <= 0:
        sigs.append(("SHORT", f"M15 overbought fade (RSI {rsi:.0f})"))
    return sigs


def find_fast_signals(m):
    if not m or not m.get("_bars") or len(m["_bars"]) < 3:
        return []
    a15 = m["atr"]; bars = m["_bars"]
    last, prev = bars[-1], bars[-2]
    body = abs(last["c"] - last["o"]); sigs = []

    if last["c"] > last["o"] and body > 0.7 * a15:
        sigs.append(("LONG", f"M15 thrust candle (body {body/a15:.1f}× ATR)"))
    if last["c"] < last["o"] and body > 0.7 * a15:
        sigs.append(("SHORT", f"M15 thrust candle (body {body/a15:.1f}× ATR)"))
    if last["c"] > last["o"] and prev["c"] < prev["o"]:
        if last["c"] > prev["o"] and last["o"] < prev["c"]:
            sigs.append(("LONG", "M15 bullish engulfing"))
    if last["c"] < last["o"] and prev["c"] > prev["o"]:
        if last["c"] < prev["o"] and last["o"] > prev["c"]:
            sigs.append(("SHORT", "M15 bearish engulfing"))
    return sigs


def find_intraday_signals(hh, h4s):
    if not hh or not hh.get("_bars") or len(hh["_bars"]) < 21:
        return []
    a1, price, rsi = hh["atr"], hh["price"], hh["rsi"]
    bars = hh["_bars"]; last = bars[-1]; sigs = []

    pull_up = 0 <= price - hh["e20"] <= 0.8 * a1
    pull_dn = 0 <= hh["e20"] - price <= 0.8 * a1
    brk_up = price >= hh["hi_prev"]
    brk_dn = price <= hh["lo_prev"]

    if (h4s["bias"] > 0 or hh["bias"] > 0) and rsi < 74 and pull_up:
        sigs.append(("LONG", "H1 pullback to EMA20 in bull structure"))
    if (h4s["bias"] < 0 or hh["bias"] < 0) and rsi > 26 and pull_dn:
        sigs.append(("SHORT", "H1 rejecting EMA20 in bear structure"))
    if brk_up and rsi < 76:
        sigs.append(("LONG", "H1 breakout of 20-bar high"))
    if brk_dn and rsi > 24:
        sigs.append(("SHORT", "H1 breakdown of 20-bar low"))

    recent_low = min(b["l"] for b in bars[-21:-1])
    recent_high = max(b["h"] for b in bars[-21:-1])
    if last["l"] < recent_low and last["c"] > recent_low:
        sigs.append(("LONG", f"H1 sweep {recent_low:.2f} + reclaim"))
    if last["h"] > recent_high and last["c"] < recent_high:
        sigs.append(("SHORT", f"H1 sweep {recent_high:.2f} + rejection"))
    return sigs


def tag(s):
    pips = s["risk"] / PIP
    return "🟢 HIGH" if pips <= 50 else ("🟡 MED" if pips <= 100 else "🟠 LOW")


def print_setup(name, emoji, s):
    arrow = "LONG 🟢" if s["side"] == "LONG" else "SHORT 🔴"
    pips = s["risk"] / PIP
    print(f"\n{emoji} {name} — {arrow}   [{tag(s)}]")
    print(f"   Setup     : {s['why']}  [{s['tf']}]")
    print(f"   Entry     : {s['entry']:.2f}")
    print(f"   SL        : {s['sl']:.2f}   (${s['risk']:.2f} = {pips:.0f} pips)")
    print(f"   TP1 (1R)  : {s['tp1']:.2f}   [close 50% · SL→BE]")
    print(f"   TP2 (2R)  : {s['tp2']:.2f}   [close rest]")
    print(f"   Hold      : {s['hold']}")


def main():
    m15 = fetch("15m", "30d")
    h1 = fetch("1h", "730d")
    d1r = fetch("1d", "5y")
    h4 = bucketed(h1, 4) if h1 else []

    if not h1 or len(h1) < 300:
        print("❌ H1 data unavailable"); return

    S = {"m15": state(m15, "M15") if m15 else None,
         "h1": state(h1, "H1"),
         "h4": state(h4, "H4") if h4 else None,
         "d1": state(d1r, "D1") if d1r else None}

    if not (S["h1"] and S["h4"] and S["d1"]):
        print("❌ Not enough HTF data"); return

    m, hh, h4s, dd = S["m15"], S["h1"], S["h4"], S["d1"]
    spot = hh["price"]
    now_pkt = datetime.datetime.now(PKT).strftime("%Y-%m-%d %H:%M PKT")

    print("\n" + "═" * 66)
    print("  🥇 GOLD SIGNALS — ENHANCED SCALP EDITION")
    print(f"  {now_pkt}   |   GC=F ≈ {spot:.2f}")
    print(f"  Rules: SL ≤ 100 pips · TP1=1R · TP2=2R")
    print("═" * 66)

    b = dd["bias"]
    bias_txt = "🟢 BULL" if b >= 0.33 else ("🔴 BEAR" if b <= -0.33 else "🟡 MIXED")
    print(f"\n📅 D1 BRIEF — {bias_txt} ({b:+.2f})  ·  RSI {dd['rsi']:.0f}  ·  ATR ${dd['atr']:.2f}")
    print(f"   H4 bias {h4s['bias']:+.2f}  ·  H1 bias {hh['bias']:+.2f}  ·  H1 EMA20 {hh['e20']:.2f}")

    found = []
    if m:
        for side, why in find_scalp_signals(m, hh):
            s = build("scalp", m, side, why)
            if s: found.append(("⚡ SCALP", "⚡", s))
        for side, why in find_fast_signals(m):
            s = build("fast", m, side, why)
            if s: found.append(("💨 FAST", "💨", s))
    for side, why in find_intraday_signals(hh, h4s):
        s = build("intraday", hh, side, why)
        if s: found.append(("🎯 INTRADAY", "🎯", s))

    if found:
        print(f"\n✅ {len(found)} setup(s):")
        for name, emoji, s in found:
            print_setup(name, emoji, s)
    else:
        print("\n⏳ No trigger at scan — market between zones")
        if m:
            print(f"   Watch LONG > {m['hi_prev']:.2f} or bounce {m['lo_prev']:.2f}")
            print(f"   Watch SHORT < {m['lo_prev']:.2f} or reject {m['hi_prev']:.2f}")

    print("\n" + "─" * 66 + "\n")

    token = os.environ.get("TG_TOKEN")
    chat = os.environ.get("TG_CHAT")
    if token and chat:
        try:
            if not found:
                text = f"🥇 Gold scan {now_pkt}\nNo setup · {spot:.2f}\nNext scan on schedule."
            else:
                lines = [f"🥇 GOLD SIGNALS — {now_pkt}", f"Spot ≈ {spot:.2f}"]
                for name, emoji, s in found[:5]:
                    arrow = "🟢 LONG" if s["side"] == "LONG" else "🔴 SHORT"
                    pips = s["risk"] / PIP
                    lines.append(f"\n{emoji} {name} — {arrow}  [{tag(s)}]")
                    lines.append(f"Entry: {s['entry']:.2f}")
                    lines.append(f"SL:    {s['sl']:.2f} ({pips:.0f} pips)")
                    lines.append(f"TP1:   {s['tp1']:.2f} (1R)")
                    lines.append(f"TP2:   {s['tp2']:.2f} (2R)")
                    lines.append(f"Setup: {s['why']}")
                text = "\n".join(lines)
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat, "text": text}, timeout=15)
        except Exception as e:
            print("telegram:", e)


if __name__ == "__main__":
    main()
    
