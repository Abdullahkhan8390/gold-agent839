#!/usr/bin/env python3
"""
agent.py — XAUUSD technical signals + tracking + freshness gate
SINGLE FILE — copy entire file, do not paste partial blocks.
"""
import os, json, datetime, requests

UTC = datetime.timezone.utc
PKT = datetime.timezone(datetime.timedelta(hours=5))
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
PIP = 0.10
COOLDOWN_HOURS = 1
MAX_HOURS_OPEN = 24
STATE_FILE = "signal_state.json"

# freshness thresholds
MAX_AGE_M15_H = 0.5
MAX_AGE_H1_H  = 3.0
MAX_AGE_H4_H  = 5.0
MAX_AGE_D1_H  = 72.0

CFG = {
    "scalp":    {"sl": 0.35, "max_sl": 10.0, "hold": "30m–4h"},
    "fast":     {"sl": 0.50, "max_sl": 10.0, "hold": "1–6h"},
    "intraday": {"sl": 0.65, "max_sl": 12.0, "hold": "4–24h"},
}


# ═══════════════ DATA ═══════════════
def fetch(interval, rng, symbol="GC=F"):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    try:
        r = requests.get(url, params={"interval": interval, "range": rng},
                         headers=UA, timeout=30)
        j = r.json()
    except Exception as e:
        print(f"[data] {symbol} {interval}: {e}")
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


def fetch_fresh(interval, rng, max_age_h):
    for sym in ("GC=F", "XAUUSD=X"):
        rows = fetch(interval, rng, sym)
        if not rows:
            continue
        age_h = (datetime.datetime.now(UTC).timestamp() - rows[-1]["t"]) / 3600.0
        if age_h <= max_age_h:
            print(f"[data] {sym} {interval}: OK · {len(rows)} bars · last {age_h:.1f}h old")
            return rows, sym
        print(f"[data] {sym} {interval}: STALE ({age_h:.1f}h > {max_age_h}h)")
    return [], None


def get_live_quote():
    sources = [
        ("XAUUSD=X", "https://query1.finance.yahoo.com/v8/finance/chart/XAUUSD=X?interval=1m&range=1d"),
        ("GC=F",     "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?interval=1m&range=1d"),
    ]
    for name, url in sources:
        try:
            r = requests.get(url, headers=UA, timeout=15)
            j = r.json()
            res = (j.get("chart") or {}).get("result")
            if not res:
                continue
            meta = res[0].get("meta") or {}
            p = meta.get("regularMarketPrice")
            t = meta.get("regularMarketTime")
            if p:
                age_min = (datetime.datetime.now(UTC).timestamp() - t) / 60.0 if t else 9999
                return float(p), age_min, name
        except Exception as e:
            print(f"[live] {name}: {e}")
    return None, None, None


# ═══════════════ HELPERS ═══════════════
def bucketed(rows, hours):
    sec = hours * 3600
    buckets = {}
    for r in rows:
        k = r["t"] // sec
        b = buckets.get(k)
        if b is None:
            buckets[k] = {"t": r["t"], "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"]}
        else:
            if r["h"] > b["h"]: b["h"] = r["h"]
            if r["l"] < b["l"]: b["l"] = r["l"]
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
    if l == 0:
        return 100.0
    return 100 - 100 / (1 + g / l)


def atr_last(hs, ls, cs, n=14):
    if len(cs) < n + 1:
        return 0.0
    trs = []
    for i in range(len(cs) - n, len(cs)):
        trs.append(max(hs[i] - ls[i],
                       abs(hs[i] - cs[i - 1]),
                       abs(ls[i] - cs[i - 1])))
    return sum(trs) / len(trs)


def state(rows, label):
    if len(rows) < 60:
        return None
    closes = [r["c"] for r in rows]
    highs  = [r["h"] for r in rows]
    lows   = [r["l"] for r in rows]
    price  = closes[-1]
    e20    = ema(closes, 20)[-1]
    e50    = ema(closes, 50)[-1]
    e200   = ema(closes, 200)[-1]
    rv     = rsi_last(closes)
    a      = atr_last(highs, lows, closes)
    if a <= 0:
        a = max(price * 0.0005, 0.01)
    score = 0
    score += 1 if price > e200 else -1
    score += 1 if e20 > e50 else -1
    score += 1 if price > e20 else -1
    return {"label": label, "price": price,
            "e20": e20, "e50": e50, "e200": e200,
            "rsi": rv, "atr": a, "bias": score / 3.0,
            "hi_prev": max(highs[-21:-1]) if len(highs) > 21 else max(highs),
            "lo_prev": min(lows[-21:-1])  if len(lows)  > 21 else min(lows),
            "_bars": rows}


# ═══════════════ BUILD ═══════════════
def build(kind, tf, side, why):
    cfg = CFG[kind]
    price = tf["price"]; a = tf["atr"]; max_sl = cfg["max_sl"]

    if side == "LONG":
        sl_atr    = price - cfg["sl"] * a
        sl_struct = tf["lo_prev"] - 0.10 * a
        sl = max(sl_atr, sl_struct) if sl_struct < price else sl_atr
        if price - sl < 0.20 * a: sl = price - 0.20 * a
        if price - sl > max_sl:   sl = price - max_sl
        risk = price - sl
        if risk <= 0.05: return None
        tp1 = price + risk
        tp2 = price + risk * 2
    else:
        sl_atr    = price + cfg["sl"] * a
        sl_struct = tf["hi_prev"] + 0.10 * a
        sl = min(sl_atr, sl_struct) if sl_struct > price else sl_atr
        if sl - price < 0.20 * a: sl = price + 0.20 * a
        if sl - price > max_sl:   sl = price + max_sl
        risk = sl - price
        if risk <= 0.05: return None
        tp1 = price - risk
        tp2 = price - risk * 2

    return {"kind": kind, "side": side, "entry": price,
            "sl": sl, "tp1": tp1, "tp2": tp2, "risk": risk,
            "why": why, "hold": cfg["hold"], "tf": tf["label"]}


# ═══════════════ SETUPS ═══════════════
def find_scalp(m, hh, h4s):
    if not m or len(m["_bars"]) < 25:
        return []
    a = m["atr"]; price = m["price"]; rsi = m["rsi"]
    bars = m["_bars"]; last = bars[-1]; prev = bars[-2]
    out = []
    lb  = last["c"] > last["o"]
    lbe = last["c"] < last["o"]
    pb  = prev["c"] > prev["o"]
    body = abs(last["c"] - last["o"])
    near  = abs(price - m["e20"]) <= 1.0 * a
    above = price >= m["hi_prev"]
    below = price <= m["lo_prev"]

    if hh["bias"] >= 0:
        if lb and body > 0.35 * a and rsi < 78:
            out.append(("LONG", f"M15 momentum bar (H1 up, RSI {rsi:.0f})"))
        if near and lb and rsi < 72:
            out.append(("LONG", "M15 pullback to EMA20, holding"))
        if above:
            out.append(("LONG", f"M15 breakout > {m['hi_prev']:.2f}"))
        if lb and pb:
            out.append(("LONG", "M15 2-bar bullish follow"))
        if rsi < 32:
            out.append(("LONG", f"M15 oversold bounce (RSI {rsi:.0f})"))
        if hh["bias"] >= 0.3 and h4s["bias"] >= 0.3 and lb:
            out.append(("LONG", "H1+H4 bull confluence"))

    if hh["bias"] <= 0:
        if lbe and body > 0.35 * a and rsi > 22:
            out.append(("SHORT", f"M15 momentum bar (H1 down, RSI {rsi:.0f})"))
        if near and lbe and rsi > 28:
            out.append(("SHORT", "M15 pullback to EMA20, rejecting"))
        if below:
            out.append(("SHORT", f"M15 breakdown < {m['lo_prev']:.2f}"))
        if lbe and not pb:
            out.append(("SHORT", "M15 2-bar bearish follow"))
        if rsi > 68:
            out.append(("SHORT", f"M15 overbought fade (RSI {rsi:.0f})"))
        if hh["bias"] <= -0.3 and h4s["bias"] <= -0.3 and lbe:
            out.append(("SHORT", "H1+H4 bear confluence"))

    if len(bars) >= 24:
        rl = min(b["l"] for b in bars[-21:-3])
        rh = max(b["h"] for b in bars[-21:-3])
        for b in bars[-3:]:
            if b["l"] < rl and b["c"] > rl and hh["bias"] >= 0:
                out.append(("LONG", f"M15 swept {rl:.2f}, reclaimed")); break
        for b in bars[-3:]:
            if b["h"] > rh and b["c"] < rh and hh["bias"] <= 0:
                out.append(("SHORT", f"M15 swept {rh:.2f}, rejected")); break
    return out


def find_fast(m, hh):
    if not m or len(m["_bars"]) < 3:
        return []
    a = m["atr"]; bars = m["_bars"]
    last = bars[-1]; prev = bars[-2]
    body = abs(last["c"] - last["o"])
    out = []
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
    if not hh or len(hh["_bars"]) < 21:
        return []
    a = hh["atr"]; price = hh["price"]; rsi = hh["rsi"]
    bars = hh["_bars"]; last = bars[-1]; prev = bars[-2]
    out = []
    lb  = last["c"] > last["o"]
    lbe = last["c"] < last["o"]
    pb  = prev["c"] > prev["o"]
    body = abs(last["c"] - last["o"])
    near  = abs(price - hh["e20"]) <= 1.0 * a
    above = price >= hh["hi_prev"]
    below = price <= hh["lo_prev"]

    if hh["bias"] >= 0 or h4s["bias"] >= 0:
        if lb and body > 0.35 * a and rsi < 76:
            out.append(("LONG", f"H1 momentum bar (RSI {rsi:.0f})"))
        if near and lb and rsi < 72:
            out.append(("LONG", "H1 pullback to EMA20, holding"))
        if above:
            out.append(("LONG", f"H1 breakout > {hh['hi_prev']:.2f}"))
        if lb and pb:
            out.append(("LONG", "H1 2-bar bullish follow"))
        if rsi < 32:
            out.append(("LONG", f"H1 oversold bounce (RSI {rsi:.0f})"))
        if hh["bias"] >= 0.3 and h4s["bias"] >= 0.3:
            out.append(("LONG", "H1+H4 bull confluence"))

    if hh["bias"] <= 0 or h4s["bias"] <= 0:
        if lbe and body > 0.35 * a and rsi > 24:
            out.append(("SHORT", f"H1 momentum bar (RSI {rsi:.0f})"))
        if near and lbe and rsi > 28:
            out.append(("SHORT", "H1 pullback to EMA20, rejecting"))
        if below:
            out.append(("SHORT", f"H1 breakdown < {hh['lo_prev']:.2f}"))
        if lbe and not pb:
            out.append(("SHORT", "H1 2-bar bearish follow"))
        if rsi > 68:
            out.append(("SHORT", f"H1 overbought fade (RSI {rsi:.0f})"))
        if hh["bias"] <= -0.3 and h4s["bias"] <= -0.3:
            out.append(("SHORT", "H1+H4 bear confluence"))

    if len(bars) >= 24:
        rl = min(b["l"] for b in bars[-21:-3])
        rh = max(b["h"] for b in bars[-21:-3])
        for b in bars[-3:]:
            if b["l"] < rl and b["c"] > rl:
                out.append(("LONG", f"H1 swept {rl:.2f}, reclaimed")); break
        for b in bars[-3:]:
            if b["h"] > rh and b["c"] < rh:
                out.append(("SHORT", f"H1 swept {rh:.2f}, rejected")); break
    return out


# ═══════════════ STATE ═══════════════
def load_state():
    st = {"open_signals": [], "history": [],
          "stats": {"total": 0, "wins": 0, "losses": 0,
                    "expired": 0, "breakeven": 0, "total_pips": 0}}
    if not os.path.exists(STATE_FILE):
        return st
    try:
        loaded = json.load(open(STATE_FILE))
    except Exception as e:
        print(f"[state] load failed ({e}) — fresh start")
        return st
    st["open_signals"] = [s for s in loaded.get("open_signals", [])
                          if not str(s.get("id", "")).startswith("TEST-")]
    st["history"]      = [s for s in loaded.get("history", [])
                          if not str(s.get("id", "")).startswith("TEST-")]
    ls = loaded.get("stats", {})
    for k in st["stats"]:
        try: st["stats"][k] = ls.get(k, 0) or 0
        except Exception: st["stats"][k] = 0
    return st


def save_state(st):
    try:
        json.dump(st, open(STATE_FILE, "w"), indent=2, default=str)
    except Exception as e:
        print(f"[state] save failed: {e}")


def check_open_signals(st, current_price, now):
    resolved, still_open = [], []
    for sig in st["open_signals"]:
        entry = sig["entry"]; sl = sig["sl"]
        tp1 = sig["tp1"]; tp2 = sig["tp2"]
        side = sig["side"]; risk = sig["risk"]
        try:
            created = datetime.datetime.fromisoformat(sig["created_at"])
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
        except Exception:
            continue
        age = (now - created).total_seconds() / 3600.0

        if age > sig.get("max_hours", MAX_HOURS_OPEN):
            sig["result"] = "EXPIRED"
            sig["exit_price"] = current_price
            sig["pips"] = 0.0
            sig["closed_at"] = now.isoformat()
            sig["age_hours"] = round(age, 1)
            resolved.append(sig); continue

        hit = None
        if side == "LONG":
            if current_price <= sl:    hit = "SL"
            elif current_price >= tp2: hit = "TP2"
            elif current_price >= tp1: hit = "TP1"
        else:
            if current_price >= sl:    hit = "SL"
            elif current_price <= tp2: hit = "TP2"
            elif current_price <= tp1: hit = "TP1"

        if hit == "SL":
            if sig.get("tp1_hit"):
                sig["result"] = "BREAKEVEN"
                sig["pips"] = round(0.5 * risk / PIP, 1)
            else:
                sig["result"] = "LOSS"
                sig["pips"] = round(-risk / PIP, 1)
            sig["exit_price"] = sl
            sig["closed_at"] = now.isoformat()
            sig["age_hours"] = round(age, 1)
            resolved.append(sig)
        elif hit == "TP2":
            sig["result"] = "WIN"
            sig["pips"] = round(1.5 * risk / PIP, 1)
            sig["exit_price"] = tp2
            sig["closed_at"] = now.isoformat()
            sig["age_hours"] = round(age, 1)
            resolved.append(sig)
        elif hit == "TP1":
            if not sig.get("tp1_hit"):
                sig["tp1_hit"] = True
                sig["tp1_hit_at"] = now.isoformat()
                print(f"[tracker] {sig.get('id','?')} TP1 hit @ {tp1:.2f}")
            still_open.append(sig)
        else:
            still_open.append(sig)

    for sig in resolved:
        st["history"].append(sig)
        r = sig["result"]
        if   r == "WIN":       st["stats"]["wins"]      += 1
        elif r == "LOSS":      st["stats"]["losses"]    += 1
        elif r == "EXPIRED":   st["stats"]["expired"]   += 1
        elif r == "BREAKEVEN": st["stats"]["breakeven"] += 1
        st["stats"]["total"] += 1
        st["stats"]["total_pips"] = round(st["stats"]["total_pips"] + sig.get("pips", 0), 1)

    st["history"] = st["history"][-100:]
    return resolved, still_open


def filter_cooldown(candidates, st):
    now = datetime.datetime.now(UTC)
    out = []; seen = set()
    for name, emoji, sig in candidates:
        key = (sig["side"], round(sig["entry"], 1))
        if key in seen:
            continue
        blocked = False
        for s in st["open_signals"]:
            if s["side"] != sig["side"]:
                continue
            try:
                t = datetime.datetime.fromisoformat(s["created_at"])
                if t.tzinfo is None:
                    t = t.replace(tzinfo=UTC)
                if (now - t).total_seconds() / 3600 < COOLDOWN_HOURS:
                    blocked = True; break
            except Exception:
                pass
        if blocked:
            continue
        seen.add(key)
        out.append((name, emoji, sig))
    return out


# ═══════════════ FORMAT ═══════════════
def fmt_signal(sig):
    pips = sig["risk"] / PIP
    arrow = "🟢 BUY" if sig["side"] == "LONG" else "🔴 SELL"
    grade = "🔥 HIGH" if pips <= 50 else ("✅ GOOD" if pips <= 80 else "🟡 OK")
    return (f"{arrow} @ {sig['entry']:.2f}   [{grade}]\n"
            f"  SL:  {sig['sl']:.2f}   ({pips:.0f} pips / ${sig['risk']:.2f})\n"
            f"  TP1: {sig['tp1']:.2f}   (1R · close 50%, SL→BE)\n"
            f"  TP2: {sig['tp2']:.2f}   (2R · close rest)\n"
            f"  Why: {sig['why']}\n"
            f"  Hold: {sig['hold']} · {sig['tf']}")


def fmt_prev_results(resolved):
    if not resolved:
        return ""
    lines = ["📈 LAST SIGNAL RESULT(S)"]
    for sig in resolved:
        icon = {"WIN": "🟢", "LOSS": "🔴",
                "BREAKEVEN": "🟡", "EXPIRED": "⚪"}.get(sig["result"], "⚪")
        arr = "BUY" if sig["side"] == "LONG" else "SELL"
        lines.append(f"  {icon} {sig['result']} · {arr} {sig['entry']:.2f} → "
                     f"{sig['exit_price']:.2f} ({sig['pips']:+.0f} pips) · {sig['age_hours']}h")
    return "\n".join(lines)


def fmt_stats(st):
    s = st["stats"]; total = s["total"]
    if total == 0:
        return ""
    decided = s["wins"] + s["losses"]
    wr = (s["wins"] / decided * 100) if decided > 0 else 0
    return (f"📊 STATS ({total} closed)\n"
            f"  W:{s['wins']} · L:{s['losses']} · BE:{s['breakeven']} · Exp:{s['expired']}\n"
            f"  Win rate: {wr:.1f}% · Total: {s['total_pips']:+.0f} pips")


def fmt_open_inventory(st):
    n = len(st["open_signals"])
    if n == 0:
        return ""
    lines = [f"🔓 OPEN TRADES ({n})"]
    for s in st["open_signals"]:
        arr = "BUY" if s["side"] == "LONG" else "SELL"
        lines.append(f"  · {arr} @ {s['entry']:.2f} · SL {s['sl']:.2f} · TP2 {s['tp2']:.2f}")
    return "\n".join(lines)


# ═══════════════ TELEGRAM ═══════════════
def send_tg(text):
    pairs = [("TG_TOKEN","TG_CHAT"),
             ("TELEGRAM_TOKEN","TELEGRAM_CHAT_ID"),
             ("TELEGRAM_TOKEN","TELEGRAM_CHAT"),
             ("TELEGRAM_BOT_TOKEN","TELEGRAM_CHAT_ID"),
             ("TELEGRAM_BOT_TOKEN","CHAT_ID"),
             ("BOT_TOKEN","CHAT_ID")]
    token = chat = used = None
    for tk, ck in pairs:
        t, c = os.environ.get(tk), os.environ.get(ck)
        if t and c:
            token, chat, used = t, c, f"{tk}/{ck}"
            break

    try:
        os.makedirs("signals", exist_ok=True)
        fn = f"signals/{datetime.datetime.now(UTC).strftime('%Y%m%d')}.log"
        with open(fn, "a") as f:
            f.write(f"\n\n=== {datetime.datetime.now(UTC).isoformat()} ===\n{text}\n")
    except Exception:
        pass

    if not token or not chat:
        print("[tg] ⚠️ no env vars — skipping")
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat, "text": text}, timeout=15)
        print(f"[tg] {used} → status={r.status_code}")
        if r.status_code != 200:
            print(f"[tg] body={r.text[:300]}")
    except Exception as e:
        print(f"[tg] error: {e}")


# ═══════════════ MAIN ═══════════════
def main():
    print("=== agent.py · technical + tracking + freshness gate ===")
    now = datetime.datetime.now(UTC)
    now_pkt = now.astimezone(PKT).strftime("%Y-%m-%d %H:%M PKT")

    if now.weekday() >= 5:
        print(f"[skip] {now_pkt} — weekend, market closed")
        return

    m15, _ = fetch_fresh("15m", "10d", MAX_AGE_M15_H)
    h1,  _ = fetch_fresh("1h",  "60d", MAX_AGE_H1_H)
    d1r, _ = fetch_fresh("1d",  "1y",  MAX_AGE_D1_H)

    if not h1 or len(h1) < 300:
        msg = "🚨 GOLD BOT ABORT\nH1 data missing or too short."
        print(msg); send_tg(msg); return
    if not d1r or len(d1r) < 200:
        msg = "🚨 GOLD BOT ABORT\nD1 data missing or too short."
        print(msg); send_tg(msg); return

    h4 = bucketed(h1, 4)

    live_price, live_age, live_src = get_live_quote()
    if live_price is None:
        msg = "🚨 GOLD BOT ABORT\nLive price unavailable."
        print(msg); send_tg(msg); return
    print(f"[live] {live_price:.2f}  ({live_src}, {live_age:.0f} min old)")

    h1_close = h1[-1]["c"]
    gap = abs(live_price - h1_close)
    limit = max(live_price * 0.003, 3.0)
    if gap > limit:
        msg = (f"⚠️ GOLD BOT ABORT — PRICE MISMATCH\n\n"
               f"Live price   : {live_price:.2f}\n"
               f"H1 last close: {h1_close:.2f}\n"
               f"Gap          : ${gap:.2f}  (limit ${limit:.2f})\n\n"
               f"Data feeds inconsistent. No signal.")
        print(msg); send_tg(msg); return
    print(f"[data] CLEAN · H1 close={h1_close:.2f} · gap=${gap:.2f}")

    S = {"m15": state(m15, "M15") if m15 else None,
         "h1":  state(h1,  "H1"),
         "h4":  state(h4,  "H4"),
         "d1":  state(d1r, "D1")}
    if not (S["h1"] and S["h4"] and S["d1"]):
        msg = "🚨 GOLD BOT ABORT\nHTF state computation failed."
        print(msg); send_tg(msg); return

    m, hh, h4s, dd = S["m15"], S["h1"], S["h4"], S["d1"]
    for tf in (m, hh, h4s, dd):
        if tf is not None:
            tf["price"] = live_price
    spot = live_price

    print(f"spot={spot:.2f} · H1={hh['bias']:+.2f} · H4={h4s['bias']:+.2f} · D1={dd['bias']:+.2f}")

    st = load_state()
    print(f"[state] open={len(st['open_signals'])} · history={len(st['history'])}")

    resolved, still_open = check_open_signals(st, spot, now)
    st["open_signals"] = still_open
    for r in resolved:
        print(f"[tracker] {r['result']}: {r['side']} @ {r['entry']:.2f} → {r['pips']:+.0f} pips")

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

    for name, emoji, sig in found:
        sig_id = f"{sig['side'][0]}{now.strftime('%Y%m%d%H%M%S')}"
        open_sig = {**sig, "id": sig_id, "created_at": now.isoformat(),
                    "max_hours": MAX_HOURS_OPEN}
        st["open_signals"].append(open_sig)
        print(f"\n{name} — {fmt_signal(sig)}")

    save_state(st)

    if not found and not resolved:
        print("quiet — no new signal, no resolution")
        return

    parts = []
    if found:
        head = f"🥇 GOLD SIGNAL{'S' if len(found)>1 else ''} — {now_pkt}\nSpot: {spot:.2f}"
        body = "\n\n".join(f"{n}\n{fmt_signal(s)}" for n, e, s in found[:5])
        parts.append(f"{head}\n\n{body}")
    if resolved:
        parts.append(fmt_prev_results(resolved))
    if found:
        parts.append(fmt_open_inventory(st))
    stats_txt = fmt_stats(st)
    if stats_txt:
        parts.append(stats_txt)

    send_tg("\n\n".join(p for p in parts if p))
    print("→ processed")


if __name__ == "__main__":
    main()
