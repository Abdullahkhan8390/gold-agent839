#!/usr/bin/env python3
"""
agent.py — XAUUSD (Gold) technical-only signal bot
═══════════════════════════════════════════════════════════════════
REMOVED: LLM · news · Fed · flip/strong/near gates
RULES:   SL hard cap $10 · TP1 = 1R · TP2 = 2R · more setups
"""
import os, json, datetime, requests

UTC = datetime.timezone.utc
PKT = datetime.timezone(datetime.timedelta(hours=5))
SYMBOL = "GC=F"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
PIP = 0.10
COOLDOWN_HOURS = 1
STATE_FILE = "signal_state.json"

CFG = {
    "scalp":    {"sl": 0.35, "max_sl": 10.0, "hold": "30m–4h"},
    "fast":     {"sl": 0.50, "max_sl": 10.0, "hold": "1–6h"},
    "intraday": {"sl": 0.65, "max_sl": 12.0, "hold": "4–24h"},
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
        if d > 0: g += d
        else: l += -d
    if l == 0: return 100.0
    return 100 - 100 / (1 + g / l)


def atr_last(highs, lows, closes, n=14):
    if len(closes) < n + 1: return 0.0
    trs = []
    for i in range(len(closes) - n, len(closes)):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    return sum(trs) / len(trs)


def state(rows, label):
    if len(rows) < 60: return None
    closes = [r["c"] for r in rows]
    highs = [r["h"] for r in rows]
    lows = [r["l"] for r in rows]
    price = closes[-1]
    e20 = ema(closes, 20)[-1]
    e50 = ema(closes, 50)[-1]
    e200 = ema(closes, 200)[-1]
    rv = rsi_last(closes)
    a = atr_last(highs, lows, closes)
    if a <= 0: a = max(price * 0.0005, 0.01)
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
    """Build signal with SL hard-capped at max_sl. Never returns None unless SL degenerate."""
    cfg = CFG[kind]
    price, a = tf["price"], tf["atr"]
    max_sl = cfg["max_sl"]

    if side == "LONG":
        sl_atr = price - cfg["sl"] * a
        sl_struct = tf["lo_prev"] - 0.10 * a
        # Tighter of the two (structure or ATR), but never tighter than 0.20 ATR
        sl = max(sl_atr, sl_struct) if sl_struct < price else sl_atr
        if price - sl < 0.20 * a:
            sl = price - 0.20 * a
        # Hard cap
        if price - sl > max_sl:
            sl = price - max_sl
        risk = price - sl
        if risk <= 0.05: return None
        tp1 = price + risk
        tp2 = price + risk * 2
    else:
        sl_atr = price + cfg["sl"] * a
        sl_struct = tf["hi_prev"] + 0.10 * a
        sl = min(sl_atr, sl_struct) if sl_struct > price else sl_atr
        if sl - price < 0.20 * a:
            sl = price + 0.20 * a
        if sl - price > max_sl:
            sl = price + max_sl
        risk = sl - price
        if risk <= 0.05: return None
        tp1 = price - risk
        tp2 = price - risk * 2

    return {"kind": kind, "side": side, "entry": price, "sl": sl,
            "tp1": tp1, "tp2": tp2, "risk": risk,
            "why": why, "hold": cfg["hold"], "tf": tf["label"]}


# ═══════════════════════════════════════════════════════════════════
# SETUP DETECTORS — 8 types per tier, fires more often
# ═══════════════════════════════════════════════════════════════════

def find_scalp(m, hh, h4s):
    if not m or len(m["_bars"]) < 25: return []
    a = m["atr"]; price = m["price"]; rsi = m["rsi"]
    bars = m["_bars"]; last = bars[-1]; prev = bars[-2]
    out = []

    last_bull = last["c"] > last["o"]
    last_bear = last["c"] < last["o"]
    prev_bull = prev["c"] > prev["o"]
    prev_bear = prev["c"] < prev["o"]
    body = abs(last["c"] - last["o"])
    near_e20 = abs(price - m["e20"]) <= 1.0 * a
    above_hi = price >= m["hi_prev"]
    below_lo = price <= m["lo_prev"]

    # ── LONG ──
    if hh["bias"] >= 0:
        if last_bull and body > 0.35 * a and rsi < 78:
            out.append(("LONG", f"M15 momentum bar (H1 up, RSI {rsi:.0f})"))
        if near_e20 and last_bull and rsi < 72:
            out.append(("LONG", "M15 pullback to EMA20, holding"))
        if above_hi:
            out.append(("LONG", f"M15 breakout > {m['hi_prev']:.2f}"))
        if last_bull and prev_bull:
            out.append(("LONG", "M15 2-bar bullish follow"))
        if rsi < 32:
            out.append(("LONG", f"M15 oversold bounce (RSI {rsi:.0f})"))
        if hh["bias"] >= 0.3 and h4s["bias"] >= 0.3 and last_bull:
            out.append(("LONG", "H1+H4 bull confluence"))

    # ── SHORT ──
    if hh["bias"] <= 0:
        if last_bear and body > 0.35 * a and rsi > 22:
            out.append(("SHORT", f"M15 momentum bar (H1 down, RSI {rsi:.0f})"))
        if near_e20 and last_bear and rsi > 28:
            out.append(("SHORT", "M15 pullback to EMA20, rejecting"))
        if below_lo:
            out.append(("SHORT", f"M15 breakdown < {m['lo_prev']:.2f}"))
        if last_bear and prev_bear:
            out.append(("SHORT", "M15 2-bar bearish follow"))
        if rsi > 68:
            out.append(("SHORT", f"M15 overbought fade (RSI {rsi:.0f})"))
        if hh["bias"] <= -0.3 and h4s["bias"] <= -0.3 and last_bear:
            out.append(("SHORT", "H1+H4 bear confluence"))

    # ── SWEEP (last 3 bars, not just last) ──
    if len(bars) >= 24:
        recent_lo = min(b["l"] for b in bars[-21:-3])
        recent_hi = max(b["h"] for b in bars[-21:-3])
        for b in bars[-3:]:
            if b["l"] < recent_lo and b["c"] > recent_lo and hh["bias"] >= 0:
                out.append(("LONG", f"M15 swept {recent_lo:.2f}, reclaimed")); break
        for b in bars[-3:]:
            if b["h"] > recent_hi and b["c"] < recent_hi and hh["bias"] <= 0:
                out.append(("SHORT", f"M15 swept {recent_hi:.2f}, rejected")); break

    return out


def find_fast(m, hh):
    if not m or len(m["_bars"]) < 3: return []
    a = m["atr"]; bars = m["_bars"]
    last, prev = bars[-1], bars[-2]
    body = abs(last["c"] - last["o"]); out = []

    if last["c"] > last["o"] and body > 0.55 * a and hh["bias"] >= 0:
        out.append(("LONG", f"M15 thrust up ({body/a:.1f}×ATR)"))
    if last["c"] < last["o"] and body > 0.55 * a and hh["bias"] <= 0:
        out.append(("SHORT", f"M15 thrust down ({body/a:.1f}×ATR)"))
    if last["c"] > last["o"] and prev["c"] < prev["o"] \
       and last["c"] > prev["o"] and last["o"] < prev["c"]:
        out.append(("LONG", "M15 bullish engulfing"))
    if last["c"] < last["o"] and prev["c"] > prev["o"] \
       and last["c"] < prev["o"] and last["o"] > prev["c"]:
        out.append(("SHORT", "M15 bearish engulfing"))
    return out


def find_intraday(hh, h4s):
    if not hh or len(hh["_bars"]) < 21: return []
    a = hh["atr"]; price = hh["price"]; rsi = hh["rsi"]
    bars = hh["_bars"]; last = bars[-1]; prev = bars[-2]
    out = []

    last_bull = last["c"] > last["o"]
    last_bear = last["c"] < last["o"]
    prev_bull = prev["c"] > prev["o"]
    prev_bear = prev["c"] < prev["o"]
    body = abs(last["c"] - last["o"])
    near_e20 = abs(price - hh["e20"]) <= 1.0 * a
    above_hi = price >= hh["hi_prev"]
    below_lo = price <= hh["lo_prev"]

    if hh["bias"] >= 0 or h4s["bias"] >= 0:
        if last_bull and body > 0.35 * a and rsi < 76:
            out.append(("LONG", f"H1 momentum bar (RSI {rsi:.0f})"))
        if near_e20 and last_bull and rsi < 72:
            out.append(("LONG", "H1 pullback to EMA20, holding"))
        if above_hi:
            out.append(("LONG", f"H1 breakout > {hh['hi_prev']:.2f}"))
        if last_bull and prev_bull:
            out.append(("LONG", "H1 2-bar bullish follow"))
        if rsi < 32:
            out.append(("LONG", f"H1 oversold bounce (RSI {rsi:.0f})"))
        if hh["bias"] >= 0.3 and h4s["bias"] >= 0.3:
            out.append(("LONG", "H1+H4 bull confluence"))

    if hh["bias"] <= 0 or h4s["bias"] <= 0:
        if last_bear and body > 0.35 * a and rsi > 24:
            out.append(("SHORT", f"H1 momentum bar (RSI {rsi:.0f})"))
        if near_e20 and last_bear and rsi > 28:
            out.append(("SHORT", "H1 pullback to EMA20, rejecting"))
        if below_lo:
            out.append(("SHORT", f"H1 breakdown < {hh['lo_prev']:.2f}"))
        if last_bear and prev_bear:
            out.append(("SHORT", "H1 2-bar bearish follow"))
        if rsi > 68:
            out.append(("SHORT", f"H1 overbought fade (RSI {rsi:.0f})"))
        if hh["bias"] <= -0.3 and h4s["bias"] <= -0.3:
            out.append(("SHORT", "H1+H4 bear confluence"))

    if len(bars) >= 24:
        recent_lo = min(b["l"] for b in bars[-21:-3])
        recent_hi = max(b["h"] for b in bars[-21:-3])
        for b in bars[-3:]:
            if b["l"] < recent_lo and b["c"] > recent_lo:
                out.append(("LONG", f"H1 swept {recent_lo:.2f}, reclaimed")); break
        for b in bars[-3:]:
            if b["h"] > recent_hi and b["c"] < recent_hi:
                out.append(("SHORT", f"H1 swept {recent_hi:.2f}, rejected")); break

    return out


# ═══════════════════════════════════════════════════════════════════
# STATE / COOLDOWN / TELEGRAM
# ═══════════════════════════════════════════════════════════════════

def load_state():
    if os.path.exists(STATE_FILE):
        try: return json.load(open(STATE_FILE))
        except Exception: pass
    return {"recent": []}


def save_state(st):
    try: json.dump(st, open(STATE_FILE, "w"), indent=2, default=str)
    except Exception as e: print("[state] save failed:", e)


def filter_cooldown(signals, st):
    now = datetime.datetime.now(UTC)
    out = []
    seen_keys = set()
    for name, emoji, sig in signals:
        key = (sig["side"], round(sig["entry"], 1))
        if key in seen_keys:
            continue
        same_dir = [r for r in st["recent"] if r["side"] == sig["side"]]
        if same_dir:
            try:
                last_t = datetime.datetime.fromisoformat(same_dir[-1]["time"])
                if last_t.tzinfo is None: last_t = last_t.replace(tzinfo=UTC)
                if (now - last_t).total_seconds() / 3600 < COOLDOWN_HOURS:
                    continue
            except Exception:
                pass
        seen_keys.add(key)
        out.append((name, emoji, sig))
    return out


def fmt_signal(sig):
    pips = sig["risk"] / PIP
    arrow = "🟢 BUY" if sig["side"] == "LONG" else "🔴 SELL"
    grade = "🔥 HIGH" if pips <= 50 else ("✅ GOOD" if pips <= 80 else "🟡 OK")
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
        print("[tg] no TG_TOKEN/TG_CHAT — skipping"); return
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
    last_bar_pkt = datetime.datetime.fromtimestamp(hh["_bars"][-1]["t"], PKT).strftime("%Y-%m-%d %H:%M")

    print(f"spot={spot:.2f} · H1 bias={hh['bias']:+.2f} · H4 bias={h4s['bias']:+.2f} · D1 bias={dd['bias']:+.2f}")
    print(f"last H1 bar: {last_bar_pkt} PKT")

    # ─── Collect candidates ───
    candidates = []
    if m:
        for side, why in find_scalp(m, hh, h4s):
            b = build("scalp", m, side, why)
            if b: candidates.append(("⚡ SCALP", "⚡", b))
        for side, why in find_fast(m, hh):
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
        # Debug WHY
        print("\n--- DEBUG: no setup fired ---")
        if m:
            lb = m["_bars"][-1]
            bd = abs(lb["c"] - lb["o"]) / m["atr"]
            dirn = "bull" if lb["c"] > lb["o"] else "bear"
            print(f"M15: rsi={m['rsi']:.0f} last={dirn} body={bd:.2f}×ATR "
                  f"px-e20={m['price']-m['e20']:+.2f} ATR={m['atr']:.2f}")
        print(f"H1 bias={hh['bias']:+.2f} rsi={hh['rsi']:.0f}")
        print(f"H4 bias={h4s['bias']:+.2f}")
        print("If this repeats all day → cron too slow, or market genuinely ranging.")
        return

    # ─── Print ───
    for name, emoji, sig in found:
        print(f"\n{emoji} {name} — {fmt_signal(sig)}")

    # ─── Save state ───
    now_iso = datetime.datetime.now(UTC).isoformat()
    for name, emoji, sig in found:
        st["recent"].append({
            "time": now_iso, "side": sig["side"], "kind": sig["kind"],
            "entry": sig["entry"], "sl": sig["sl"],
            "tp1": sig["tp1"], "tp2": sig["tp2"],
        })
    st["recent"] = st["recent"][-100:]
    save_state(st)

    # ─── Telegram ───
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
    
