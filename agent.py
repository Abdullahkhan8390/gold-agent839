#!/usr/bin/env python3
"""
agent.py — XAUUSD (Gold) technical-only signal bot
═══════════════════════════════════════════════════════════════════
REMOVED: LLM scoring · news sentiment · Fed sentiment · flip/strong/near gates
KEPT:    Sweeps · EMA structure · RSI extremes · thrust · engulfing
RULES:   SL ≤ 100 pips ($10) · TP1 = 1R · TP2 = 2R · more setups
"""
import os, json, datetime, requests

UTC = datetime.timezone.utc
PKT = datetime.timezone(datetime.timedelta(hours=5))
SYMBOL = "GC=F"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
PIP = 0.10
COOLDOWN_HOURS = 2
STATE_FILE = "signal_state.json"

CFG = {
    "scalp":    {"sl": 0.55, "max_sl": 10.0, "hold": "30m–4h"},
    "fast":     {"sl": 0.70, "max_sl": 10.0, "hold": "1–6h"},
    "intraday": {"sl": 0.90, "max_sl": 12.0, "hold": "4–24h"},
}


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
        "_bars": rows,
    }


def build(kind, tf, side, why):
    cfg = CFG[kind]
    price, a = tf["price"], tf["atr"]
    if side == "LONG":
        sl_atr = price - cfg["sl"] * a
        sl_struct = tf["lo_prev"] - 0.10 * a
        sl = max(sl_atr, sl_struct) if (price - sl_struct) <= 2.0 * a else sl_atr
        risk = price - sl
        if risk <= 0 or risk > cfg["max_sl"]:
            return None
        tp1 = price + risk
        tp2 = price + risk * 2
    else:
        sl_atr = price + cfg["sl"] * a
        sl_struct = tf["hi_prev"] + 0.10 * a
        sl = min(sl_atr, sl_struct) if (sl_struct - price) <= 2.0 * a else sl_atr
        risk = sl - price
        if risk <= 0 or risk > cfg["max_sl"]:
            return None
        tp1 = price - risk
        tp2 = price - risk * 2
    return {"kind": kind, "side": side, "entry": price, "sl": sl,
            "tp1": tp1, "tp2": tp2, "risk": risk,
            "why": why, "hold": cfg["hold"], "tf": tf["label"]}


def find_scalp(m, hh):
    if not m or len(m["_bars"]) < 21:
        return []
    a15, price, rsi = m["atr"], m["price"], m["rsi"]
    bars = m["_bars"]; last = bars[-1]; out = []

    pull_up = 0 <= price - m["e20"] <= 0.7 * a15
    pull_dn = 0 <= m["e20"] - price <= 0.7 * a15
    brk_up = price >= m["hi_prev"]
    brk_dn = price <= m["lo_prev"]

    if hh["bias"] > 0 and rsi < 72 and pull_up:
        out.append(("LONG", "H1 up + M15 pullback to EMA20"))
    if hh["bias"] < 0 and rsi > 28 and pull_dn:
        out.append(("SHORT", "H1 down + M15 rejecting EMA20"))
    if hh["bias"] >= 0 and brk_up and rsi < 78:
        out.append(("LONG", f"M15 breakout above {m['hi_prev']:.2f}"))
    if hh["bias"] <= 0 and brk_dn and rsi > 22:
        out.append(("SHORT", f"M15 breakdown below {m['lo_prev']:.2f}"))

    recent_low = min(b["l"] for b in bars[-21:-1])
    recent_high = max(b["h"] for b in bars[-21:-1])
    if last["l"] < recent_low and last["c"] > recent_low and hh["bias"] >= 0:
        out.append(("LONG", f"M15 sweep {recent_low:.2f} + reclaim"))
    if last["h"] > recent_high and last["c"] < recent_high and hh["bias"] <= 0:
        out.append(("SHORT", f"M15 sweep {recent_high:.2f} + rejection"))

    if rsi < 25 and price > m["e200"] and hh["bias"] >= 0:
        out.append(("LONG", f"M15 oversold bounce (RSI {rsi:.0f})"))
    if rsi > 75 and price < m["e200"] and hh["bias"] <= 0:
        out.append(("SHORT", f"M15 overbought fade (RSI {rsi:.0f})"))
    return out


def find_fast(m):
    if not m or len(m["_bars"]) < 3:
        return []
    a15 = m["atr"]; bars = m["_bars"]
    last, prev = bars[-1], bars[-2]
    body = abs(last["c"] - last["o"]); out = []

    if last["c"] > last["o"] and body > 0.7 * a15:
        out.append(("LONG", f"M15 thrust up (body {body/a15:.1f}×ATR)"))
    if last["c"] < last["o"] and body > 0.7 * a15:
        out.append(("SHORT", f"M15 thrust down (body {body/a15:.1f}×ATR)"))
    if last["c"] > last["o"] and prev["c"] < prev["o"] \
       and last["c"] > prev["o"] and last["o"] < prev["c"]:
        out.append(("LONG", "M15 bullish engulfing"))
    if last["c"] < last["o"] and prev["c"] > prev["o"] \
       and last["c"] < prev["o"] and last["o"] > prev["c"]:
        out.append(("SHORT", "M15 bearish engulfing"))
    return out


def find_intraday(hh, h4s):
    if not hh or len(hh["_bars"]) < 21:
        return []
    a1, price, rsi = hh["atr"], hh["price"], hh["rsi"]
    bars = hh["_bars"]; last = bars[-1]; out = []

    pull_up = 0 <= price - hh["e20"] <= 0.8 * a1
    pull_dn = 0 <= hh["e20"] - price <= 0.8 * a1
    brk_up = price >= hh["hi_prev"]
    brk_dn = price <= hh["lo_prev"]

    if (h4s["bias"] > 0 or hh["bias"] > 0) and rsi < 74 and pull_up:
        out.append(("LONG", "H1 pullback to EMA20 (bull structure)"))
    if (h4s["bias"] < 0 or hh["bias"] < 0) and rsi > 26 and pull_dn:
        out.append(("SHORT", "H1 rejecting EMA20 (bear structure)"))
    if brk_up and rsi < 76:
        out.append(("LONG", f"H1 breakout above {hh['hi_prev']:.2f}"))
    if brk_dn and rsi > 24:
        out.append(("SHORT", f"H1 breakdown below {hh['lo_prev']:.2f}"))

    recent_low = min(b["l"] for b in bars[-21:-1])
    recent_high = max(b["h"] for b in bars[-21:-1])
    if last["l"] < recent_low and last["c"] > recent_low:
        out.append(("LONG", f"H1 sweep {recent_low:.2f} + reclaim"))
    if last["h"] > recent_high and last["c"] < recent_high:
        out.append(("SHORT", f"H1 sweep {recent_high:.2f} + rejection"))
    return out


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE))
        except Exception:
            pass
    return {"recent": []}


def save_state(st):
    try:
        json.dump(st, open(STATE_FILE, "w"), indent=2, default=str)
    except Exception as e:
        print("[state] save failed:", e)


def filter_cooldown(signals, st):
    now = datetime.datetime.now(UTC)
    out = []
    for name, emoji, sig in signals:
        same = [r for r in st["recent"] if r["side"] == sig["side"]]
        if same:
            try:
                last = datetime.datetime.fromisoformat(same[-1]["time"])
                if last.tzinfo is None:
                    last = last.replace(tzinfo=UTC)
                if (now - last).total_seconds() / 3600 < COOLDOWN_HOURS:
                    print(f"  [skip] {sig['side']} in cooldown ({sig['kind']})")
                    continue
            except Exception:
                pass
        out.append((name, emoji, sig))
    return out


def fmt_signal(sig):
    pips = sig["risk"] / PIP
    arrow = "🟢 BUY" if sig["side"] == "LONG" else "🔴 SELL"
    grade = "🔥 HIGH" if pips <= 40 else ("✅ GOOD" if pips <= 70 else "🟡 OK")
    return (
        f"{arrow} @ {sig['entry']:.2f}   [{grade}]\n"
        f"  SL:  {sig['sl']:.2f}   ({pips:.0f} pips / ${sig['risk']:.2f})\n"
        f"  TP1: {sig['tp1']:.2f}   (1R · close 50%, SL→BE)\n"
        f"  TP2: {sig['tp2']:.2f}   (2R · close rest)\n"
        f"  Why: {sig['why']}\n"
        f"  Hold: {sig['hold']} · {sig['tf']}"
    )


def send_tg(text):
    token = os.environ.get("TG_TOKEN")
    chat = os.environ.get("TG_CHAT")
    if not token or not chat:
        print("[tg] no TG_TOKEN/TG_CHAT — skipping")
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": text}, timeout=15)
    except Exception as e:
        print("[tg] error:", e)


def main():
    print("=== agent.py · technical only ===")
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
        print("❌ HTF data missing"); return

    m, hh, h4s, dd = S["m15"], S["h1"], S["h4"], S["d1"]
    spot = hh["price"]
    now_pkt = datetime.datetime.now(PKT).strftime("%Y-%m-%d %H:%M PKT")

    print(f"spot={spot:.2f} · H1 bias={hh['bias']:+.2f} · H4 bias={h4s['bias']:+.2f} · D1 bias={dd['bias']:+.2f}")

    # ── Collect all valid setups ──
    candidates = []
    if m:
        for side, why in find_scalp(m, hh):
            b = build("scalp", m, side, why)
            if b: candidates.append(("⚡ SCALP", "⚡", b))
        for side, why in find_fast(m):
            b = build("fast", m, side, why)
            if b: candidates.append(("💨 FAST", "💨", b))
    for side, why in find_intraday(hh, h4s):
        b = build("intraday", hh, side, why)
        if b: candidates.append(("🎯 INTRADAY", "🎯", b))

    print(f"candidates={len(candidates)}")

    st = load_state()
    found = filter_cooldown(candidates, st)
    print(f"after cooldown={len(found)}")

    if not found:
        print("no signal — quiet")
        return

    # ── Print ──
    for name, emoji, sig in found:
        print(f"\n{emoji} {name} — {fmt_signal(sig)}")

    # ── Save state ──
    now_iso = datetime.datetime.now(UTC).isoformat()
    for name, emoji, sig in found:
        st["recent"].append({
            "time": now_iso, "side": sig["side"], "kind": sig["kind"],
            "entry": sig["entry"], "sl": sig["sl"],
            "tp1": sig["tp1"], "tp2": sig["tp2"],
        })
    st["recent"] = st["recent"][-50:]
    save_state(st)

    # ── Telegram: only send when signals exist ──
    if len(found) == 1:
        name, emoji, sig = found[0]
        text = f"🥇 GOLD {emoji} {name}\n{now_pkt}\n\n{fmt_signal(sig)}\n\nSpot: {spot:.2f}"
    else:
        head = f"🥇 GOLD — {len(found)} setups\n{now_pkt}\nSpot: {spot:.2f}"
        body = "\n\n".join(f"{e} {n}\n{fmt_signal(s)}" for n, e, s in found[:5])
        text = f"{head}\n\n{body}"
    send_tg(text)
    print("→ sent to Telegram")


if __name__ == "__main__":
    main()
    
