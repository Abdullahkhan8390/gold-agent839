#!/usr/bin/env python3
"""
agent.py — XAUUSD technical signals + trade tracking
═════════════════════════════════════════════════════════════════
FEATURES:
  · 8 setup types across M15 / H1 (sweep, momentum, pullback, breakout...)
  · SL hard cap $10 (100 pips) · TP1 = 1R · TP2 = 2R
  · Tracks every fired signal → WIN / LOSS / BREAKEVEN / EXPIRED
  · Sends previous result + cumulative stats with each new signal
"""
import os, json, datetime, requests

UTC = datetime.timezone.utc
PKT = datetime.timezone(datetime.timedelta(hours=5))
SYMBOL = "GC=F"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
PIP = 0.10
COOLDOWN_HOURS = 1
MAX_HOURS_OPEN = 24
STATE_FILE = "signal_state.json"

CFG = {
    "scalp":    {"sl": 0.35, "max_sl": 10.0, "hold": "30m–4h"},
    "fast":     {"sl": 0.50, "max_sl": 10.0, "hold": "1–6h"},
    "intraday": {"sl": 0.65, "max_sl": 12.0, "hold": "4–24h"},
}


# ═══════════════════════ DATA ═══════════════════════
def fetch(interval, rng):
def fetch(interval, rng, symbol=None):
    sym = symbol or SYMBOL
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
    try:
        r = requests.get(url, params={"interval": interval, "range": rng},
                         headers=UA, timeout=30)
        j = r.json()
    except Exception as e:
        print(f"[data] {sym} {interval}: {e}"); return []
    res = (j.get("chart") or {}).get("result")
    if not res: return []
    res = res[0]; ts = res.get("timestamp") or []
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    o, h, l, c = q.get("open", []), q.get("high", []), q.get("low", []), q.get("close", [])
    rows = []
    for i in range(len(ts)):
        if i >= len(c): break
        if None in (o[i], h[i], l[i], c[i]): continue
        rows.append({"t": int(ts[i]), "o": float(o[i]), "h": float(h[i]),
                     "l": float(l[i]), "c": float(c[i])})
    return rows


def fetch_fresh(interval, rng, max_age_h):
    """Try GC=F then XAUUSD=X, return first fresh series."""
    for sym in ["GC=F", "XAUUSD=X"]:
        rows = fetch(interval, rng, sym)
        if not rows: continue
        age = (datetime.datetime.now(UTC).timestamp() - rows[-1]["t"]) / 3600
        if age <= max_age_h:
            print(f"[data] {sym} {interval}: OK · last bar {age:.1f}h old")
            return rows, sym
        print(f"[data] {sym} {interval}: STALE ({age:.1f}h) → next")
    return [], None
def get_live_quote():
    """Live gold price from Yahoo meta.spot → futures."""
    urls = [
        ("XAUUSD=X", "https://query1.finance.yahoo.com/v8/finance/chart/XAUUSD=X?interval=1m&range=1d"),
        ("GC=F",     "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?interval=1m&range=1d"),
    ]
    for name, u in urls:
        try:
            r = requests.get(u, headers=UA, timeout=15)
            j = r.json()
            res = (j.get("chart") or {}).get("result")
            if not res: continue
            meta = res[0].get("meta") or {}
            p = meta.get("regularMarketPrice")
            t = meta.get("regularMarketTime")
            if p:
                age_min = (datetime.datetime.now(UTC).timestamp() - t) / 60 if t else 9999
                return float(p), age_min, name
        except Exception as e:
            print(f"[live] {name}: {e}")
    return None, None, None


def validate_data(h1, h4, d1r, m15=None):
    """Return list of problems (empty = clean)."""
    now_ts = datetime.datetime.now(UTC).timestamp()
    problems = []
    if not h1:
        problems.append("H1 data missing"); return problems

    h1_age = (now_ts - h1[-1]["t"]) / 3600
    if h1_age > 3:
        problems.append(f"H1 last bar {h1_age:.1f}h old (max 3h)")

    if h4:
        h4_age = (now_ts - h4[-1]["t"]) / 3600
        if h4_age > 5:
            problems.append(f"H4 last bar {h4_age:.1f}h old (max 5h)")

    if d1r:
        d1_age = (now_ts - d1r[-1]["t"]) / 3600
        if d1_age > 48:
            problems.append(f"D1 last bar {d1_age:.0f}h old (max 48h)")

    if m15:
        m15_age = (now_ts - m15[-1]["t"]) / 3600
        if m15_age > 1:
            problems.append(f"M15 last bar {m15_age:.1f}h old (max 1h)")

    return problems


def bucketed(rows, hours):
    sec = hours * 3600; buckets = {}
    for r in rows:
        k = r["t"] // sec; b = buckets.get(k)
        if b is None:
            buckets[k] = {"t": r["t"], "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"]}
        else:
            b["h"] = max(b["h"], r["h"]); b["l"] = min(b["l"], r["l"]); b["c"] = r["c"]
    return [buckets[k] for k in sorted(buckets)]


def ema(vals, n):
    n = max(2, min(n, len(vals))); k = 2.0/(n+1); e = vals[0]; out = [e]
    for v in vals[1:]:
        e = v*k + e*(1-k); out.append(e)
    return out


def rsi_last(vals, n=14):
    if len(vals) < n+1: return 50.0
    seg = vals[-(n+1):]; g = l = 0.0
    for i in range(1, len(seg)):
        d = seg[i] - seg[i-1]
        if d > 0: g += d
        else: l += -d
    if l == 0: return 100.0
    return 100 - 100/(1 + g/l)


def atr_last(hs, ls, cs, n=14):
    if len(cs) < n+1: return 0.0
    trs = []
    for i in range(len(cs)-n, len(cs)):
        trs.append(max(hs[i]-ls[i], abs(hs[i]-cs[i-1]), abs(ls[i]-cs[i-1])))
    return sum(trs)/len(trs)


def state(rows, label):
    if len(rows) < 60: return None
    closes = [r["c"] for r in rows]; highs = [r["h"] for r in rows]; lows = [r["l"] for r in rows]
    price = closes[-1]
    e20 = ema(closes, 20)[-1]; e50 = ema(closes, 50)[-1]; e200 = ema(closes, 200)[-1]
    rv = rsi_last(closes); a = atr_last(highs, lows, closes)
    if a <= 0: a = max(price*0.0005, 0.01)
    score = 0
    score += 1 if price > e200 else -1
    score += 1 if e20 > e50 else -1
    score += 1 if price > e20 else -1
    return {"label": label, "price": price, "e20": e20, "e50": e50, "e200": e200,
            "rsi": rv, "atr": a, "bias": score/3.0,
            "hi_prev": max(highs[-21:-1]) if len(highs) > 21 else max(highs),
            "lo_prev": min(lows[-21:-1]) if len(lows) > 21 else min(lows),
            "_bars": rows}


# ═══════════════════════ BUILD ═══════════════════════
def build(kind, tf, side, why):
    cfg = CFG[kind]; price = tf["price"]; a = tf["atr"]; max_sl = cfg["max_sl"]
    if side == "LONG":
        sl_atr = price - cfg["sl"]*a
        sl_struct = tf["lo_prev"] - 0.10*a
        sl = max(sl_atr, sl_struct) if sl_struct < price else sl_atr
        if price - sl < 0.20*a: sl = price - 0.20*a
        if price - sl > max_sl: sl = price - max_sl
        risk = price - sl
        if risk <= 0.05: return None
        tp1 = price + risk; tp2 = price + risk*2
    else:
        sl_atr = price + cfg["sl"]*a
        sl_struct = tf["hi_prev"] + 0.10*a
        sl = min(sl_atr, sl_struct) if sl_struct > price else sl_atr
        if sl - price < 0.20*a: sl = price + 0.20*a
        if sl - price > max_sl: sl = price + max_sl
        risk = sl - price
        if risk <= 0.05: return None
        tp1 = price - risk; tp2 = price - risk*2
    return {"kind": kind, "side": side, "entry": price, "sl": sl,
            "tp1": tp1, "tp2": tp2, "risk": risk,
            "why": why, "hold": cfg["hold"], "tf": tf["label"]}


# ═══════════════════════ SETUPS ═══════════════════════
def find_scalp(m, hh, h4s):
    if not m or len(m["_bars"]) < 25: return []
    a = m["atr"]; price = m["price"]; rsi = m["rsi"]
    bars = m["_bars"]; last = bars[-1]; prev = bars[-2]; out = []
    lb = last["c"] > last["o"]; lbe = last["c"] < last["o"]
    pb = prev["c"] > prev["o"]
    body = abs(last["c"] - last["o"])
    near = abs(price - m["e20"]) <= 1.0*a
    above = price >= m["hi_prev"]; below = price <= m["lo_prev"]

    if hh["bias"] >= 0:
        if lb and body > 0.35*a and rsi < 78: out.append(("LONG", f"M15 momentum bar (H1 up, RSI {rsi:.0f})"))
        if near and lb and rsi < 72: out.append(("LONG", "M15 pullback to EMA20, holding"))
        if above: out.append(("LONG", f"M15 breakout > {m['hi_prev']:.2f}"))
        if lb and pb: out.append(("LONG", "M15 2-bar bullish follow"))
        if rsi < 32: out.append(("LONG", f"M15 oversold bounce (RSI {rsi:.0f})"))
        if hh["bias"] >= 0.3 and h4s["bias"] >= 0.3 and lb: out.append(("LONG", "H1+H4 bull confluence"))
    if hh["bias"] <= 0:
        if lbe and body > 0.35*a and rsi > 22: out.append(("SHORT", f"M15 momentum bar (H1 down, RSI {rsi:.0f})"))
        if near and lbe and rsi > 28: out.append(("SHORT", "M15 pullback to EMA20, rejecting"))
        if below: out.append(("SHORT", f"M15 breakdown < {m['lo_prev']:.2f}"))
        if lbe and not pb: out.append(("SHORT", "M15 2-bar bearish follow"))
        if rsi > 68: out.append(("SHORT", f"M15 overbought fade (RSI {rsi:.0f})"))
        if hh["bias"] <= -0.3 and h4s["bias"] <= -0.3 and lbe: out.append(("SHORT", "H1+H4 bear confluence"))

    if len(bars) >= 24:
        rl = min(b["l"] for b in bars[-21:-3]); rh = max(b["h"] for b in bars[-21:-3])
        for b in bars[-3:]:
            if b["l"] < rl and b["c"] > rl and hh["bias"] >= 0:
                out.append(("LONG", f"M15 swept {rl:.2f}, reclaimed")); break
        for b in bars[-3:]:
            if b["h"] > rh and b["c"] < rh and hh["bias"] <= 0:
                out.append(("SHORT", f"M15 swept {rh:.2f}, rejected")); break
    return out


def find_fast(m, hh):
    if not m or len(m["_bars"]) < 3: return []
    a = m["atr"]; bars = m["_bars"]; last = bars[-1]; prev = bars[-2]
    body = abs(last["c"] - last["o"]); out = []
    if last["c"] > last["o"] and body > 0.55*a and hh["bias"] >= 0:
        out.append(("LONG", f"M15 thrust up ({body/a:.1f}×ATR)"))
    if last["c"] < last["o"] and body > 0.55*a and hh["bias"] <= 0:
        out.append(("SHORT", f"M15 thrust down ({body/a:.1f}×ATR)"))
    if last["c"] > last["o"] and prev["c"] < prev["o"] and last["c"] > prev["o"] and last["o"] < prev["c"]:
        out.append(("LONG", "M15 bullish engulfing"))
    if last["c"] < last["o"] and prev["c"] > prev["o"] and last["c"] < prev["o"] and last["o"] > prev["c"]:
        out.append(("SHORT", "M15 bearish engulfing"))
    return out


def find_intraday(hh, h4s):
    if not hh or len(hh["_bars"]) < 21: return []
    a = hh["atr"]; price = hh["price"]; rsi = hh["rsi"]
    bars = hh["_bars"]; last = bars[-1]; prev = bars[-2]; out = []
    lb = last["c"] > last["o"]; lbe = last["c"] < last["o"]
    pb = prev["c"] > prev["o"]
    body = abs(last["c"] - last["o"])
    near = abs(price - hh["e20"]) <= 1.0*a
    above = price >= hh["hi_prev"]; below = price <= hh["lo_prev"]

    if hh["bias"] >= 0 or h4s["bias"] >= 0:
        if lb and body > 0.35*a and rsi < 76: out.append(("LONG", f"H1 momentum bar (RSI {rsi:.0f})"))
        if near and lb and rsi < 72: out.append(("LONG", "H1 pullback to EMA20, holding"))
        if above: out.append(("LONG", f"H1 breakout > {hh['hi_prev']:.2f}"))
        if lb and pb: out.append(("LONG", "H1 2-bar bullish follow"))
        if rsi < 32: out.append(("LONG", f"H1 oversold bounce (RSI {rsi:.0f})"))
        if hh["bias"] >= 0.3 and h4s["bias"] >= 0.3: out.append(("LONG", "H1+H4 bull confluence"))
    if hh["bias"] <= 0 or h4s["bias"] <= 0:
        if lbe and body > 0.35*a and rsi > 24: out.append(("SHORT", f"H1 momentum bar (RSI {rsi:.0f})"))
        if near and lbe and rsi > 28: out.append(("SHORT", "H1 pullback to EMA20, rejecting"))
        if below: out.append(("SHORT", f"H1 breakdown < {hh['lo_prev']:.2f}"))
        if lbe and not pb: out.append(("SHORT", "H1 2-bar bearish follow"))
        if rsi > 68: out.append(("SHORT", f"H1 overbought fade (RSI {rsi:.0f})"))
        if hh["bias"] <= -0.3 and h4s["bias"] <= -0.3: out.append(("SHORT", "H1+H4 bear confluence"))

    if len(bars) >= 24:
        rl = min(b["l"] for b in bars[-21:-3]); rh = max(b["h"] for b in bars[-21:-3])
        for b in bars[-3:]:
            if b["l"] < rl and b["c"] > rl:
                out.append(("LONG", f"H1 swept {rl:.2f}, reclaimed")); break
        for b in bars[-3:]:
            if b["h"] > rh and b["c"] < rh:
                out.append(("SHORT", f"H1 swept {rh:.2f}, rejected")); break
    return out


# ═══════════════════════ TRACKER ═══════════════════════
def load_state():
    st = {"open_signals": [], "history": [],
          "stats": {"total": 0, "wins": 0, "losses": 0, "expired": 0,
                    "breakeven": 0, "total_pips": 0}}
    if os.path.exists(STATE_FILE):
        try:
            loaded = json.load(open(STATE_FILE))
            st["open_signals"] = loaded.get("open_signals", [])
            st["history"] = loaded.get("history", [])
            for k in st["stats"]:
                st["stats"][k] = loaded.get("stats", {}).get(k, 0)
        except Exception as e:
            print(f"[state] load failed: {e}")
    return st


def save_state(st):
    try:
        json.dump(st, open(STATE_FILE, "w"), indent=2, default=str)
    except Exception as e:
        print(f"[state] save failed: {e}")


def check_open_signals(st, current_price, now):
    """Check every open signal vs current price. Returns (closed_list, remaining_open)."""
    resolved = []; still_open = []
    for sig in st["open_signals"]:
        entry, sl, tp1, tp2 = sig["entry"], sig["sl"], sig["tp1"], sig["tp2"]
        side, risk = sig["side"], sig["risk"]
        created = datetime.datetime.fromisoformat(sig["created_at"])
        if created.tzinfo is None: created = created.replace(tzinfo=UTC)
        age = (now - created).total_seconds() / 3600

        # Expiry
        if age > sig.get("max_hours", MAX_HOURS_OPEN):
            sig["result"] = "EXPIRED"; sig["exit_price"] = current_price
            sig["pips"] = 0; sig["closed_at"] = now.isoformat()
            sig["age_hours"] = round(age, 1)
            resolved.append(sig); continue

        # Determine hit
        hit = None
        if side == "LONG":
            if current_price <= sl: hit = "SL"
            elif current_price >= tp2: hit = "TP2"
            elif current_price >= tp1: hit = "TP1"
        else:
            if current_price >= sl: hit = "SL"
            elif current_price <= tp2: hit = "TP2"
            elif current_price <= tp1: hit = "TP1"

        if hit == "SL":
            if sig.get("tp1_hit"):
                sig["result"] = "BREAKEVEN"
                sig["pips"] = round(0.5 * risk / PIP, 1)  # 50% took 1R
            else:
                sig["result"] = "LOSS"
                sig["pips"] = round(-risk / PIP, 1)
            sig["exit_price"] = sl; sig["closed_at"] = now.isoformat()
            sig["age_hours"] = round(age, 1); resolved.append(sig)
        elif hit == "TP2":
            sig["result"] = "WIN"
            sig["pips"] = round(1.5 * risk / PIP, 1)  # 0.5×1R + 0.5×2R
            sig["exit_price"] = tp2; sig["closed_at"] = now.isoformat()
            sig["age_hours"] = round(age, 1); resolved.append(sig)
        elif hit == "TP1":
            if not sig.get("tp1_hit"):
                sig["tp1_hit"] = True; sig["tp1_hit_at"] = now.isoformat()
                print(f"[tracker] {sig['id']} hit TP1 @ {tp1:.2f}")
            still_open.append(sig)
        else:
            still_open.append(sig)

    for sig in resolved:
        st["history"].append(sig)
        r = sig["result"]
        if r == "WIN": st["stats"]["wins"] += 1
        elif r == "LOSS": st["stats"]["losses"] += 1
        elif r == "EXPIRED": st["stats"]["expired"] += 1
        elif r == "BREAKEVEN": st["stats"]["breakeven"] += 1
        st["stats"]["total"] += 1
        st["stats"]["total_pips"] = round(st["stats"]["total_pips"] + sig.get("pips", 0), 1)

    st["history"] = st["history"][-100:]
    return resolved, still_open


def filter_cooldown(candidates, st):
    now = datetime.datetime.now(UTC); out = []; seen = set()
    for name, emoji, sig in candidates:
        key = (sig["side"], round(sig["entry"], 1))
        if key in seen: continue
        same = [s for s in st["open_signals"] if s["side"] == sig["side"]]
        blocked = False
        for s in same:
            try:
                t = datetime.datetime.fromisoformat(s["created_at"])
                if t.tzinfo is None: t = t.replace(tzinfo=UTC)
                if (now - t).total_seconds()/3600 < COOLDOWN_HOURS:
                    blocked = True; break
            except Exception: pass
        if blocked: continue
        seen.add(key); out.append((name, emoji, sig))
    return out


# ═══════════════════════ FORMATTING ═══════════════════════
def fmt_signal(sig):
    pips = sig["risk"]/PIP
    arrow = "🟢 BUY" if sig["side"] == "LONG" else "🔴 SELL"
    grade = "🔥 HIGH" if pips <= 50 else ("✅ GOOD" if pips <= 80 else "🟡 OK")
    return (f"{arrow} @ {sig['entry']:.2f}   [{grade}]\n"
            f"  SL:  {sig['sl']:.2f}   ({pips:.0f} pips / ${sig['risk']:.2f})\n"
            f"  TP1: {sig['tp1']:.2f}   (1R · close 50%, SL→BE)\n"
            f"  TP2: {sig['tp2']:.2f}   (2R · close rest)\n"
            f"  Why: {sig['why']}\n"
            f"  Hold: {sig['hold']} · {sig['tf']}")


def fmt_prev_results(resolved):
    if not resolved: return ""
    lines = ["\n📈 LAST SIGNAL RESULT(S)"]
    for sig in resolved:
        icon = {"WIN": "🟢", "LOSS": "🔴", "BREAKEVEN": "🟡", "EXPIRED": "⚪"}.get(sig["result"], "⚪")
        arr = "BUY" if sig["side"] == "LONG" else "SELL"
        lines.append(f"  {icon} {sig['result']} · {arr} {sig['entry']:.2f} → "
                     f"{sig['exit_price']:.2f} ({sig['pips']:+.0f} pips) · {sig['age_hours']}h")
    return "\n".join(lines)


def fmt_stats(st):
    s = st["stats"]; total = s["total"]
    if total == 0: return ""
    decided = s["wins"] + s["losses"]
    wr = (s["wins"]/decided*100) if decided > 0 else 0
    return (f"\n📊 STATS ({total} closed)\n"
            f"  W:{s['wins']} · L:{s['losses']} · BE:{s['breakeven']} · Exp:{s['expired']}\n"
            f"  Win rate: {wr:.1f}% · Total: {s['total_pips']:+.0f} pips")


def fmt_open_inventory(st):
    n = len(st["open_signals"])
    if n == 0: return ""
    lines = [f"\n🔓 OPEN TRADES ({n})"]
    for s in st["open_signals"]:
        arr = "BUY" if s["side"] == "LONG" else "SELL"
        lines.append(f"  · {arr} @ {s['entry']:.2f} · SL {s['sl']:.2f} · TP2 {s['tp2']:.2f}")
    return "\n".join(lines)


def send_tg(text):
    pairs = [("TG_TOKEN","TG_CHAT"),("TELEGRAM_TOKEN","TELEGRAM_CHAT_ID"),
             ("TELEGRAM_TOKEN","TELEGRAM_CHAT"),("TELEGRAM_BOT_TOKEN","TELEGRAM_CHAT_ID"),
             ("TELEGRAM_BOT_TOKEN","CHAT_ID"),("BOT_TOKEN","CHAT_ID")]
    token = chat = used = None
    for tk, ck in pairs:
        t, c = os.environ.get(tk), os.environ.get(ck)
        if t and c: token, chat, used = t, c, f"{tk}/{ck}"; break

    try:
        os.makedirs("signals", exist_ok=True)
        fn = f"signals/{datetime.datetime.now(UTC).strftime('%Y%m%d')}.log"
        with open(fn, "a") as f:
            f.write(f"\n\n=== {datetime.datetime.now(UTC).isoformat()} ===\n{text}\n")
    except Exception: pass

    if not token or not chat:
        print("[tg] ⚠️ no env vars found"); return
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat, "text": text}, timeout=15)
        print(f"[tg] {used} → status={r.status_code}")
        if r.status_code != 200: print(f"[tg] body={r.text[:300]}")
    except Exception as e:
        print("[tg] error:", e)


# ═══════════════════════ MAIN ═══════════════════════
def main():
    print("=== agent.py · technical + freshness gate ===")
    m15, _ = fetch_fresh("15m", "10d", 1)
    h1, _  = fetch_fresh("1h",  "60d", 3)
    d1r, _ = fetch_fresh("1d",  "1y",  48)
    h4 = bucketed(h1, 4) if h1 else []

    if not h1 or len(h1) < 300:
        msg = "🚨 GOLD BOT ABORT\nH1 data unavailable."
        print(msg); send_tg(msg); return

    # ── Data freshness gate ──
    problems = validate_data(h1, h4, d1r, m15)
    live_price, live_age, live_src = get_live_quote()

    if live_price is None:
        msg = "🚨 GOLD BOT ABORT\nLive price fetch failed."
        print(msg); send_tg(msg); return

    print(f"[live] {live_price:.2f} ({live_src}, age {live_age:.0f}min)")

    if problems:
        msg = ("🚨 GOLD BOT ABORT — STALE DATA\n\n"
               + "\n".join(f"· {p}" for p in problems)
               + f"\n\nLive: {live_price:.2f}\nNo signal fired.")
        print(msg); send_tg(msg); return

    # ── Price consistency check ──
    h1_close = h1[-1]["c"]
    gap = abs(live_price - h1_close)
    limit = max(live_price * 0.003, 3.0)   # 0.3% ya $3 — jo bada ho
    if gap > limit:
        msg = (f"⚠️ PRICE MISMATCH ABORT\n"
               f"Live: {live_price:.2f}\n"
               f"H1 last close: {h1_close:.2f}\n"
               f"Gap: ${gap:.2f} (limit ${limit:.2f})\n\n"
               f"Feed inconsistent. No signal.")
        print(msg); send_tg(msg); return

    print(f"[data] CLEAN · live={live_price:.2f} · H1 close={h1_close:.2f} · gap=${gap:.2f}")

    # ── Build states ──
    S = {"m15": state(m15, "M15") if m15 else None,
         "h1": state(h1, "H1"),
         "h4": state(h4, "H4") if h4 else None,
         "d1": state(d1r, "D1") if d1r else None}

    if not (S["h1"] and S["h4"] and S["d1"]):
        msg = "🚨 GOLD BOT ABORT\nHTF states missing."
        print(msg); send_tg(msg); return

    m, hh, h4s, dd = S["m15"], S["h1"], S["h4"], S["d1"]
    spot = hh["price"] = m["price"] if m else live_price  # placeholder, real price below

    # Use live price everywhere for entry
    if m:   m["price"] = live_price
    hh["price"] = live_price
    h4s["price"] = live_price
    dd["price"] = live_price
    spot = live_price
    now_pkt = now.astimezone(PKT).strftime("%Y-%m-%d %H:%M PKT")

    print(f"spot={spot:.2f} · H1={hh['bias']:+.2f} · H4={h4s['bias']:+.2f} · D1={dd['bias']:+.2f}")

    st = load_state()
    print(f"[state] open={len(st['open_signals'])} · history={len(st['history'])}")

    # ── Step 1: Resolve any open signals ──
    resolved, still_open = check_open_signals(st, spot, now)
    st["open_signals"] = still_open
    if resolved:
        for r in resolved:
            print(f"[tracker] {r['result']}: {r['side']} @ {r['entry']:.2f} → {r['pips']:+.0f} pips")

    # ── Step 2: Find new setups ──
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

    found = filter_cooldown(candidates, st)
    print(f"after cooldown={len(found)}")

    # ── Step 3: Save new signals to state ──
    for name, emoji, sig in found:
        sig_id = f"{sig['side'][0]}{now.strftime('%Y%m%d%H%M%S')}"
        open_sig = {**sig, "id": sig_id, "created_at": now.isoformat(),
                    "max_hours": MAX_HOURS_OPEN}
        st["open_signals"].append(open_sig)
        print(f"\n{emoji} {name} — {fmt_signal(sig)}")

    save_state(st)

    # ── Step 4: Send to Telegram (if anything happened) ──
    if not found and not resolved:
        print("no new signal, no resolution — quiet")
        return

    parts = []
    if found:
        head = f"🥇 GOLD SIGNAL{'S' if len(found)>1 else ''} — {now_pkt}\nSpot: {spot:.2f}"
        body = "\n\n".join(f"{n}\n{fmt_signal(s)}" for n, e, s in found[:5])
        parts.append(f"{head}\n\n{body}")
    if resolved:
        parts.append(fmt_prev_results(resolved).strip())
    if found:
        parts.append(fmt_open_inventory(st).strip())
    parts.append(fmt_stats(st).strip())

    text = "\n\n".join(p for p in parts if p)
    send_tg(text)
    print("→ sent to Telegram")


if __name_
