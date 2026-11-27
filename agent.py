#!/usr/bin/env python3
"""
agent.py v11.3 — SL→BE fix + max-1-per-side + volume warnings
Fixes:
1. TP1 hit → actually move SL to entry (BE)
2. Block new signal if any open position on same side (no pyramiding)
3. Warn on volume <0.5x for momentum signals (not just breakouts)
4. Fix EXPIRED pips to include banked TP1 profit
"""
import os, json, datetime, requests

UTC = datetime.timezone.utc
PKT = datetime.timezone(datetime.timedelta(hours=5))
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
PIP = 0.10
COOLDOWN_HOURS = 1
MAX_HOURS_OPEN = 24
MAX_SL_PIPS = 150
STATE_FILE = "signal_state.json"
HEARTBEAT_HOURS = 4
NEWS_BUFFER_MIN = 30
DXY_CONFLICT_PCT = 0.4
VOLUME_WEAK_RATIO = 0.8
VOLUME_VERY_WEAK = 0.5

CFG = {
    "scalp":    {"sl_mult": 1.2, "min_pips": 30, "max_pips": 80,
                 "tp1_r": 1.0, "tp2_r": 2.0, "hold": "30m-4h"},
    "intraday": {"sl_mult": 0.6, "min_pips": 50, "max_pips": 120,
                 "tp1_r": 1.0, "tp2_r": 2.0, "hold": "4-24h"},
    "reversal": {"sl_mult": 0.5, "min_pips": 50, "max_pips": 130,
                 "tp1_r": 1.0, "tp2_r": 2.0, "hold": "1-6h"},
}


# ==================== DATA FETCH ====================

def fetch(interval, rng, symbol="GC=F", use_periods=False, server="query1"):
    url = f"https://{server}.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"interval": interval}
    if use_periods:
        end = int(datetime.datetime.now(UTC).timestamp())
        secs = {"1d":86400,"5d":432000,"10d":864000,"30d":2592000,
                "60d":5184000,"3mo":7776000,"6mo":15552000,"1y":31536000,
                "2y":63072000,"5y":157680000,"730d":63072000}
        params["period1"] = end - secs.get(rng, 2592000)
        params["period2"] = end
    else:
        params["range"] = rng
    try:
        r = requests.get(url, params=params, headers=UA, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


def fetch_ohlc(interval, rng, symbol="GC=F", server="query1"):
    j = fetch(interval, rng, symbol, use_periods=True, server=server)
    res = (j.get("chart") or {}).get("result")
    if not res: return []
    res = res[0]
    ts = res.get("timestamp") or []
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    o = q.get("open") or []; h = q.get("high") or []
    l = q.get("low") or [];  c = q.get("close") or []
    out = []
    for i in range(len(ts)):
        if i >= len(c): break
        if None in (o[i], h[i], l[i], c[i]): continue
        out.append({"t": int(ts[i]), "o": float(o[i]), "h": float(h[i]),
                    "l": float(l[i]), "c": float(c[i])})
    return out


def fetch_twelvedata(interval="1h", outputsize=500):
    key = os.environ.get("TWELVEDATA_KEY")
    if not key: return []
    try:
        r = requests.get("https://api.twelvedata.com/time_series",
            params={"symbol": "XAU/USD", "interval": interval,
                    "outputsize": outputsize, "apikey": key,
                    "timezone": "UTC", "order": "ASC"}, timeout=30)
        j = r.json()
    except Exception:
        return []
    if j.get("status") == "error" or "values" not in j: return []
    rows = []
    for v in j["values"]:
        try:
            ds = v["datetime"]
            fmt = "%Y-%m-%d %H:%M:%S" if " " in ds else "%Y-%m-%d"
            dt = datetime.datetime.strptime(ds, fmt).replace(tzinfo=UTC)
            rows.append({"t": int(dt.timestamp()),
                         "o": float(v["open"]), "h": float(v["high"]),
                         "l": float(v["low"]), "c": float(v["close"])})
        except Exception:
            continue
    return rows


def closed_only(rows, tf_hours):
    if not rows: return rows
    now_ts = datetime.datetime.now(UTC).timestamp()
    if now_ts - rows[-1]["t"] < tf_hours * 3600:
        return rows[:-1]
    return rows


# ==================== FILTERS ====================

def fetch_news_events():
    try:
        r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json",
                         headers=UA, timeout=10)
        if r.status_code != 200: return []
        events = r.json()
    except Exception:
        return []
    out = []
    for e in events:
        try:
            if (e.get("country") or "").upper() not in ("USD", "US"): continue
            if (e.get("impact") or "").lower() != "high": continue
            ds = (e.get("date") or "").strip()
            if not ds: continue
            dt = datetime.datetime.fromisoformat(ds.replace("Z", "+00:00"))
            if dt.tzinfo is None: dt = dt.replace(tzinfo=UTC)
            out.append({"title": (e.get("title") or "?")[:40], "dt": dt})
        except Exception:
            continue
    return out


def check_news_blackout(now, events, buffer_min=NEWS_BUFFER_MIN):
    for e in events:
        if abs((now - e["dt"]).total_seconds()) < buffer_min * 60:
            mins = int((e["dt"] - now).total_seconds() / 60)
            return False, {"title": e["title"], "in_min": mins}
    return True, None


def fetch_dxy_4h_change():
    try:
        j = fetch("1h", "5d", "DX-Y.NYB")
        res = (j.get("chart") or {}).get("result") or [{}]
        q = ((res[0].get("indicators") or {}).get("quote") or [{}])[0]
        c = [x for x in (q.get("close") or []) if x is not None and x > 0]
        if len(c) < 5: return None
        return round((c[-1] - c[-5]) / c[-5] * 100, 2)
    except Exception:
        return None


def fetch_gc_volume_ratio():
    try:
        j = fetch("1h", "5d", "GC=F")
        res = (j.get("chart") or {}).get("result") or [{}]
        q = ((res[0].get("indicators") or {}).get("quote") or [{}])[0]
        v = [x for x in (q.get("volume") or []) if x is not None and x > 0]
        if len(v) < 21: return None
        last = v[-1]; avg = sum(v[-21:-1]) / 20
        return round(last / avg, 2) if avg > 0 else None
    except Exception:
        return None


def grade_signal(sig, ctx, h1_close=None, ref_atr=None):
    score = 100; notes = []
    
    # Chase check
    if h1_close is not None and ref_atr and ref_atr > 0:
        if sig["side"] == "SHORT":
            moved = h1_close - sig["entry"]
        else:
            moved = sig["entry"] - h1_close
        moved_atr = moved / ref_atr
        if moved_atr > 1.5:
            score -= 50; notes.append(f"🚫 Late chase {moved_atr:+.1f}×ATR past H1")
        elif moved_atr > 1.0:
            score -= 30; notes.append(f"⚠️ Late entry {moved_atr:+.1f}×ATR past H1")
        elif moved_atr > 0.5:
            score -= 15; notes.append(f"⚠️ Entry {moved_atr:+.1f}×ATR into move")
        elif moved_atr < -0.3:
            notes.append(f"✅ Pullback entry ({moved_atr:+.1f}×ATR)")
    
    # News
    if not ctx["news_ok"]:
        score -= 50
        ne = ctx["news_event"] or {}
        mins = ne.get("in_min", 0)
        when = f"in {mins}min" if mins >= 0 else f"{abs(mins)}min ago"
        notes.append(f"🚫 News: {ne.get('title','?')} ({when})")
    
    # DXY
    dxy = ctx.get("dxy_change_4h")
    if dxy is not None:
        if sig["side"] == "LONG" and dxy > DXY_CONFLICT_PCT:
            score -= 25; notes.append(f"⚠️ DXY +{dxy:.2f}% (against LONG)")
        elif sig["side"] == "SHORT" and dxy < -DXY_CONFLICT_PCT:
            score -= 25; notes.append(f"⚠️ DXY {dxy:.2f}% (against SHORT)")
        elif abs(dxy) >= DXY_CONFLICT_PCT:
            notes.append(f"✅ DXY {dxy:+.2f}% (aligned)")
    
    # Volume — v11.3: applies to momentum too
    vr = ctx.get("volume_ratio")
    if vr is not None:
        why_lower = sig.get("why", "").lower()
        if "breakout" in why_lower:
            if vr < VOLUME_WEAK_RATIO:
                score -= 15; notes.append(f"⚠️ Weak volume {vr}x on breakout")
            else:
                notes.append(f"✅ Volume {vr}x")
        elif "momentum" in why_lower:
            if vr < VOLUME_VERY_WEAK:
                score -= 15
                notes.append(f"⚠️ Very weak volume {vr}x on momentum")
            elif vr < VOLUME_WEAK_RATIO:
                score -= 8
                notes.append(f"⚠️ Weak volume {vr}x on momentum")
    
    if score >= 80: grade = "A"
    elif score >= 60: grade = "B"
    elif score >= 40: grade = "C"
    else: grade = "D"
    return grade, score, notes


# ==================== TF / INDICATORS ====================

def derive_core_tfs():
    raw = fetch_twelvedata("1h", 5000)
    if raw and len(raw) >= 4000:
        raw = closed_only(raw, 1.0)
        h1 = raw[-500:]; h4 = bucketed(raw, 4); d1 = bucketed(raw, 24)
        print(f"[core] TD 1h · {len(raw)} → H1={len(h1)} H4={len(h4)} D1={len(d1)}")
        return h1, h4, d1, "TwelveData 1h"
    raw = fetch_ohlc("1h", "60d")
    if raw and len(raw) >= 400:
        raw = closed_only(raw, 1.0)
        h1 = raw[-500:] if len(raw) > 500 else raw
        h4 = bucketed(raw, 4); d1 = bucketed(raw, 24)
        print(f"[core] Yahoo 1h · {len(raw)}")
        return h1, h4, d1, "Yahoo 1h"
    return [], [], [], None


def derive_m15():
    raw = fetch_twelvedata("15min", 500)
    if raw and len(raw) >= 60:
        raw = closed_only(raw, 0.25)
        print(f"[m15] TD 15min · {len(raw)} bars")
        return raw, "TwelveData 15min"
    raw = fetch_ohlc("15m", "30d")
    if raw and len(raw) >= 60:
        raw = closed_only(raw, 0.25)
        print(f"[m15] Yahoo 15m · {len(raw)}")
        return raw, "Yahoo 15m"
    return [], None


def get_live_quote():
    if os.environ.get("TWELVEDATA_KEY"):
        try:
            r = requests.get("https://api.twelvedata.com/price",
                params={"symbol": "XAU/USD",
                        "apikey": os.environ["TWELVEDATA_KEY"]}, timeout=15)
            p = float(r.json().get("price", 0))
            if p > 0: return p, "TwelveData"
        except Exception:
            pass
    for name, url in [
        ("XAUUSD=X", "https://query1.finance.yahoo.com/v8/finance/chart/XAUUSD=X?interval=1m&range=1d"),
        ("GC=F", "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?interval=1m&range=1d"),
    ]:
        try:
            r = requests.get(url, headers=UA, timeout=15)
            res = ((r.json().get("chart") or {}).get("result") or [{}])[0]
            p = (res.get("meta") or {}).get("regularMarketPrice")
            if p: return float(p), name
        except Exception:
            continue
    return None, None


def bucketed(rows, hours):
    sec = hours * 3600; b = {}
    for r in rows:
        k = r["t"] // sec
        if k not in b:
            b[k] = {"t": r["t"], "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"]}
        else:
            if r["h"] > b[k]["h"]: b[k]["h"] = r["h"]
            if r["l"] < b[k]["l"]: b[k]["l"] = r["l"]
            b[k]["c"] = r["c"]
    return [b[k] for k in sorted(b)]


def ema(vals, n):
    n = max(2, min(n, len(vals)))
    k = 2.0 / (n + 1); e = vals[0]; out = [e]
    for v in vals[1:]:
        e = v * k + e * (1 - k); out.append(e)
    return out


def rsi_last(vals, n=14):
    if len(vals) < n + 1: return 50.0
    seg = vals[-(n + 1):]; g = 0.0; l = 0.0
    for i in range(1, len(seg)):
        d = seg[i] - seg[i - 1]
        if d > 0: g += d
        else: l += -d
    if l == 0: return 100.0
    return 100 - 100 / (1 + g / l)


def atr_last(hs, ls, cs, n=14):
    if len(cs) < n + 1: return 0.0
    trs = []
    for i in range(len(cs) - n, len(cs)):
        trs.append(max(hs[i] - ls[i], abs(hs[i] - cs[i - 1]), abs(ls[i] - cs[i - 1])))
    return sum(trs) / len(trs)


def state(rows, label):
    if len(rows) < 60: return None
    closes = [r["c"] for r in rows]
    highs = [r["h"] for r in rows]
    lows = [r["l"] for r in rows]
    p = closes[-1]
    e20 = ema(closes, 20)[-1]; e50 = ema(closes, 50)[-1]; e200 = ema(closes, 200)[-1]
    rv = rsi_last(closes)
    a = atr_last(highs, lows, closes) or max(p * 0.0005, 0.01)
    score = 0
    score += 1 if p > e200 else -1
    score += 1 if e20 > e50 else -1
    score += 1 if p > e20 else -1
    return {"label": label, "price": p, "e20": e20, "e50": e50, "e200": e200,
            "rsi": rv, "atr": a, "bias": score / 3.0,
            "hi_prev": max(highs[-21:-1]) if len(highs) > 21 else max(highs),
            "lo_prev": min(lows[-21:-1]) if len(lows) > 21 else min(lows),
            "_bars": rows}


# ==================== BUILD ====================

def build(kind, tf, side, why, ref_atr=None):
    cfg = CFG[kind]
    p = tf["price"]
    a = ref_atr if ref_atr else tf["atr"]
    if a <= 0: return None

    sl_mult = cfg["sl_mult"]
    min_dist = cfg["min_pips"] * PIP
    max_dist = cfg["max_pips"] * PIP

    atr_dist = sl_mult * a
    struct_buf = 0.3 * a
    if side == "LONG":
        struct_dist = p - (tf["lo_prev"] - struct_buf)
    else:
        struct_dist = (tf["hi_prev"] + struct_buf) - p

    sl_dist = max(atr_dist, struct_dist)
    sl_dist = min(sl_dist, 2.0 * atr_dist)
    sl_dist = max(sl_dist, min_dist)
    sl_dist = min(sl_dist, max_dist)

    sl = p - sl_dist if side == "LONG" else p + sl_dist
    risk = abs(p - sl)
    if risk < PIP * 20: return None

    tp1 = p + risk * cfg["tp1_r"] if side == "LONG" else p - risk * cfg["tp1_r"]
    tp2 = p + risk * cfg["tp2_r"] if side == "LONG" else p - risk * cfg["tp2_r"]

    return {"kind": kind, "side": side, "entry": p, "sl": sl,
            "tp1": tp1, "tp2": tp2, "risk": risk,
            "why": why, "hold": cfg["hold"], "tf": tf["label"],
            "sl_pips": round(risk / PIP, 1),
            "atr_used": a,
            "tp1_r": cfg["tp1_r"],
            "tp2_r": cfg["tp2_r"]}


# ==================== FINDERS ====================

def find_scalp(m, hh, h4s):
    if not m or len(m["_bars"]) < 25: return []
    a = m["atr"]; p = m["price"]; rsi = m["rsi"]
    bars = m["_bars"]; last = bars[-1]; prev = bars[-2]
    out = []
    lb = last["c"] > last["o"]; lbe = last["c"] < last["o"]; pb = prev["c"] > prev["o"]
    body = abs(last["c"] - last["o"])
    near = abs(p - m["e20"]) <= 1.0 * a
    if hh["bias"] >= 0:
        if lb and body > 0.35 * a and rsi < 78:
            out.append(("LONG", f"M15 momentum (RSI {rsi:.0f})"))
        if near and lb and rsi < 72:
            out.append(("LONG", "M15 pullback EMA20"))
        if p >= m["hi_prev"]:
            out.append(("LONG", f"M15 breakout > {m['hi_prev']:.2f}"))
        if lb and pb:
            out.append(("LONG", "M15 2-bar bull"))
        if rsi < 32:
            out.append(("LONG", f"M15 oversold (RSI {rsi:.0f})"))
    if hh["bias"] <= 0:
        if lbe and body > 0.35 * a and rsi > 22:
            out.append(("SHORT", f"M15 momentum (RSI {rsi:.0f})"))
        if near and lbe and rsi > 28:
            out.append(("SHORT", "M15 reject EMA20"))
        if p <= m["lo_prev"]:
            out.append(("SHORT", f"M15 breakdown < {m['lo_prev']:.2f}"))
        if lbe and not pb:
            out.append(("SHORT", "M15 2-bar bear"))
        if rsi > 68:
            out.append(("SHORT", f"M15 overbought (RSI {rsi:.0f})"))
    return out


def find_intraday(hh, h4s):
    if not hh or len(hh["_bars"]) < 21: return []
    a = hh["atr"]; p = hh["price"]; rsi = hh["rsi"]
    bars = hh["_bars"]; last = bars[-1]; prev = bars[-2]
    out = []
    lb = last["c"] > last["o"]; lbe = last["c"] < last["o"]; pb = prev["c"] > prev["o"]
    body = abs(last["c"] - last["o"])
    near = abs(p - hh["e20"]) <= 1.0 * a
    if hh["bias"] >= 0 or h4s["bias"] >= 0:
        if lb and body > 0.35 * a and rsi < 76:
            out.append(("LONG", f"H1 momentum (RSI {rsi:.0f})"))
        if near and lb and rsi < 72:
            out.append(("LONG", "H1 pullback EMA20"))
        if p >= hh["hi_prev"]:
            out.append(("LONG", f"H1 breakout > {hh['hi_prev']:.2f}"))
        if lb and pb:
            out.append(("LONG", "H1 2-bar bull"))
        if rsi < 32:
            out.append(("LONG", f"H1 oversold (RSI {rsi:.0f})"))
    if hh["bias"] <= 0 or h4s["bias"] <= 0:
        if lbe and body > 0.35 * a and rsi > 24:
            out.append(("SHORT", f"H1 momentum (RSI {rsi:.0f})"))
        if near and lbe and rsi > 28:
            out.append(("SHORT", "H1 reject EMA20"))
        if p <= hh["lo_prev"]:
            out.append(("SHORT", f"H1 breakdown < {hh['lo_prev']:.2f}"))
        if lbe and not pb:
            out.append(("SHORT", "H1 2-bar bear"))
        if rsi > 68:
            out.append(("SHORT", f"H1 overbought (RSI {rsi:.0f})"))
    return out


def find_reversal(m, hh, h4s, dd):
    if not m or len(m["_bars"]) < 6: return []
    bars = m["_bars"]; last = bars[-1]; prev = bars[-2]; recent = bars[-4:]
    body = abs(last["c"] - last["o"]) or 0.01
    upper = last["h"] - max(last["c"], last["o"])
    lower = min(last["c"], last["o"]) - last["l"]
    any_bear = any(b["c"] < b["o"] for b in recent)
    any_bull = any(b["c"] > b["o"] for b in recent)
    weaker_close = last["c"] < prev["c"]
    stronger_close = last["c"] > prev["c"]
    out = []
    if hh["rsi"] >= 82 and h4s["bias"] <= -0.25 and dd["bias"] <= -0.25:
        if upper > body * 1.0 or any_bear or last["c"] < last["o"] or weaker_close:
            out.append(("SHORT", f"H1 RSI {hh['rsi']:.0f} exhausted · H4/D1 bearish"))
    if hh["rsi"] <= 18 and h4s["bias"] >= 0.25 and dd["bias"] >= 0.25:
        if lower > body * 1.0 or any_bull or last["c"] > last["o"] or stronger_close:
            out.append(("LONG", f"H1 RSI {hh['rsi']:.0f} exhausted · H4/D1 bullish"))
    return out


# ==================== STATE ====================

def load_state():
    st = {"open_signals": [], "history": [],
          "stats": {"total": 0, "wins": 0, "losses": 0,
                    "expired": 0, "breakeven": 0, "total_pips": 0},
          "last_heartbeat": None, "last_c_warning": None}
    if not os.path.exists(STATE_FILE): return st
    try: loaded = json.load(open(STATE_FILE))
    except Exception: return st
    st["open_signals"] = [s for s in loaded.get("open_signals", [])
                          if not str(s.get("id", "")).startswith("TEST-")]
    st["history"] = [s for s in loaded.get("history", [])
                     if not str(s.get("id", "")).startswith("TEST-")]
    for k in st["stats"]:
        try: st["stats"][k] = loaded.get("stats", {}).get(k, 0) or 0
        except Exception: st["stats"][k] = 0
    st["last_heartbeat"] = loaded.get("last_heartbeat")
    st["last_c_warning"] = loaded.get("last_c_warning")
    return st


def save_state(st):
    try: json.dump(st, open(STATE_FILE, "w"), indent=2, default=str)
    except Exception as e: print(f"[state] {e}")


def check_open_signals(st, price, now):
    """
    v11.3 fixes:
    - On TP1: actually move SL to entry (BE)
    - On SL hit after TP1: exit at BE (entry), record +0.5R
    - On TP2: proper 1.5R if TP1 was hit
    - On EXPIRED: include banked TP1 profit
    """
    resolved = []; still = []
    for sig in st["open_signals"]:
        try:
            cr = datetime.datetime.fromisoformat(sig["created_at"])
            if cr.tzinfo is None: cr = cr.replace(tzinfo=UTC)
        except Exception: continue
        age = (now - cr).total_seconds() / 3600.0
        side = sig["side"]
        entry = sig["entry"]
        sl = sig["sl"]
        tp1 = sig["tp1"]
        tp2 = sig["tp2"]
        risk = sig["risk"]
        tp1_done = sig.get("tp1_hit", False)
        
        # Expire check FIRST
        if age > sig.get("max_hours", MAX_HOURS_OPEN):
            if tp1_done:
                # Banked +0.5R at TP1, exit remaining at current price
                if side == "LONG":
                    remain_r = (price - entry) / risk
                else:
                    remain_r = (entry - price) / risk
                total_r = 0.5 + 0.5 * remain_r
                pips = round(total_r * risk / PIP, 1)
            else:
                pips = 0.0
            sig.update({"result": "EXPIRED", "exit_price": price, "pips": pips,
                        "closed_at": now.isoformat(), "age_hours": round(age, 1)})
            resolved.append(sig)
            continue
        
        # Determine hit - SL takes priority
        hit = None
        if side == "LONG":
            if price <= sl: hit = "SL"
            elif price >= tp2: hit = "TP2"
            elif price >= tp1 and not tp1_done: hit = "TP1"
        else:
            if price >= sl: hit = "SL"
            elif price <= tp2: hit = "TP2"
            elif price <= tp1 and not tp1_done: hit = "TP1"
        
        if hit == "SL":
            if tp1_done:
                # After BE move, SL is at entry = breakeven exit + banked TP1
                sig.update({"result": "BREAKEVEN",
                            "pips": round(0.5 * risk / PIP, 1),
                            "exit_price": entry})  # exit at BE, not original SL
            else:
                sig.update({"result": "LOSS",
                            "pips": round(-risk / PIP, 1),
                            "exit_price": sl})
            sig.update({"closed_at": now.isoformat(), "age_hours": round(age, 1)})
            resolved.append(sig)
        elif hit == "TP2":
            # Half at TP1 (+1R) + Half at TP2 (+2R) = 1.5R
            sig.update({"result": "WIN",
                        "pips": round(1.5 * risk / PIP, 1),
                        "exit_price": tp2})
            sig.update({"closed_at": now.isoformat(), "age_hours": round(age, 1)})
            resolved.append(sig)
        elif hit == "TP1":
            # v11.3: ACTUALLY move SL to entry (BE)
            sig["tp1_hit"] = True
            sig["tp1_hit_at"] = now.isoformat()
            sig["sl"] = entry  # ← REAL BE MOVEMENT
            sig["sl_moved_to_be"] = True
            print(f"[tracker] {sig.get('id','?')} TP1 @ {tp1:.2f} → SL moved to BE {entry:.2f}")
            still.append(sig)
        else:
            still.append(sig)
    
    for sig in resolved:
        st["history"].append(sig)
        r = sig["result"]
        if r in ("WIN", "LOSS", "EXPIRED", "BREAKEVEN"):
            key = {"WIN":"wins","LOSS":"losses",
                   "EXPIRED":"expired","BREAKEVEN":"breakeven"}[r]
            st["stats"][key] += 1
        st["stats"]["total"] += 1
        st["stats"]["total_pips"] = round(st["stats"]["total_pips"] + sig.get("pips", 0), 1)
    st["history"] = st["history"][-100:]
    return resolved, still


def filter_cooldown(cands, st):
    """
    v11.3: Max 1 open signal per side.
    If any open position exists (LONG or SHORT), block new signals on that side.
    This prevents pyramiding into correlated risk.
    """
    now = datetime.datetime.now(UTC)
    out = []; seen = set()
    open_sides = set(s["side"] for s in st["open_signals"])
    
    for name, emoji, sig in cands:
        k = (sig["side"], round(sig["entry"], 1))
        if k in seen: continue
        
        # v11.3: Block if same side already open
        if sig["side"] in open_sides:
            print(f"[filter] blocked {sig['side']} — existing open position")
            continue
        
        # Also keep cooldown for recently-closed signals
        blocked = False
        for s in st["open_signals"]:
            if s["side"] != sig["side"]: continue
            try:
                t = datetime.datetime.fromisoformat(s["created_at"])
                if t.tzinfo is None: t = t.replace(tzinfo=UTC)
                if (now - t).total_seconds() / 3600 < COOLDOWN_HOURS:
                    blocked = True; break
            except Exception: pass
        if blocked: continue
        seen.add(k); out.append((name, emoji, sig))
        
    before = len(out)
    out = [c for c in out if c[2]["risk"] / PIP <= MAX_SL_PIPS]
    if len(out) < before:
        print(f"[filter] SL cap rejected {before - len(out)}")
    if not out: return []
    sides = set(c[2]["side"] for c in out)
    if len(sides) > 1:
        def sc(c):
            pips = c[2]["risk"] / PIP
            s = 200 - pips
            w = c[2]["why"].lower()
            if "exhausted" in w: s += 30
            if "confluence" in w: s += 20
            return s
        best = max(out, key=sc)
        print(f"[filter] contradictory → {best[2]['side']}")
        return [best]
    return out[:1]


# ==================== FORMAT ====================

def fmt_signal(sig, notes=None):
    pips = sig["risk"] / PIP
    arrow = "🟢 BUY" if sig["side"] == "LONG" else "🔴 SELL"
    grade = sig.get("grade", "?")
    badge = {"A": "🏆 A", "B": "⚠️ B", "C": "🟡 C", "D": "⚪ D"}.get(grade, grade)
    atr_used = sig.get("atr_used", 0)
    t1r = sig.get("tp1_r", 1.0)
    t2r = sig.get("tp2_r", 2.0)
    body = (f"{arrow} @ {sig['entry']:.2f}  [{badge} · {sig.get('score','?')}]\n"
            f"  SL:  {sig['sl']:.2f}  ({pips:.0f}p / ${sig['risk']:.2f})\n"
            f"  TP1: {sig['tp1']:.2f}  ({t1r}R · 50%, SL→BE)\n"
            f"  TP2: {sig['tp2']:.2f}  ({t2r}R · rest)\n"
            f"  Why: {sig['why']}\n"
            f"  ATR: {atr_used:.2f} · sized to {pips:.0f}p\n"
            f"  Hold: {sig['hold']} · {sig['tf']}")
    if notes:
        body += "\n  " + "\n  ".join(notes)
    return body


def fmt_prev(res):
    if not res: return ""
    lines = ["📈 RESULT(S)"]
    for s in res:
        ic = {"WIN":"🟢","LOSS":"🔴","BREAKEVEN":"🟡","EXPIRED":"⚪"}.get(s["result"],"⚪")
        arr = "BUY" if s["side"] == "LONG" else "SELL"
        lines.append(f"  {ic} {s['result']} · {arr} {s['entry']:.2f} → "
                     f"{s['exit_price']:.2f} ({s['pips']:+.0f}p) · {s['age_hours']}h")
    return "\n".join(lines)


def fmt_stats(st):
    s = st["stats"]
    if s["total"] == 0: return ""
    dec = s["wins"] + s["losses"]
    wr = (s["wins"] / dec * 100) if dec > 0 else 0
    return (f"📊 STATS ({s['total']})\n"
            f"  W:{s['wins']} L:{s['losses']} BE:{s['breakeven']} Exp:{s['expired']}\n"
            f"  Win: {wr:.1f}% · Pips: {s['total_pips']:+.0f}")


def fmt_open(st):
    if not st["open_signals"]: return ""
    lines = [f"🔓 OPEN ({len(st['open_signals'])})"]
    for s in st["open_signals"]:
        arr = "BUY" if s["side"] == "LONG" else "SELL"
        be_note = " · BE" if s.get("tp1_hit") else ""
        lines.append(f"  · {arr} @ {s['entry']:.2f} SL {s['sl']:.2f} "
                     f"TP1 {s['tp1']:.2f} TP2 {s['tp2']:.2f}{be_note}")
    return "\n".join(lines)


def send_tg(text):
    pairs = [("TG_TOKEN","TG_CHAT"), ("TELEGRAM_TOKEN","TELEGRAM_CHAT_ID"),
             ("TELEGRAM_TOKEN","TELEGRAM_CHAT"), ("TELEGRAM_BOT_TOKEN","TELEGRAM_CHAT_ID"),
             ("TELEGRAM_BOT_TOKEN","CHAT_ID"), ("BOT_TOKEN","CHAT_ID")]
    tk = ck = None
    for a, b in pairs:
        if os.environ.get(a) and os.environ.get(b):
            tk, ck = a, b; break
    try:
        os.makedirs("signals", exist_ok=True)
        fn = f"signals/{datetime.datetime.now(UTC).strftime('%Y%m%d')}.log"
        with open(fn, "a") as f:
            f.write(f"\n\n=== {datetime.datetime.now(UTC).isoformat()} ===\n{text}\n")
    except Exception: pass
    if not tk: return
    try:
        r = requests.post(f"https://api.telegram.org/bot{os.environ[tk]}/sendMessage",
                          data={"chat_id": os.environ[ck], "text": text}, timeout=15)
        print(f"[tg] {tk} → {r.status_code}")
    except Exception as e:
        print(f"[tg] {e}")


# ==================== MAIN ====================

def main():
    print("=== agent.py v11.3 · BE fix + max-1-per-side + volume warnings ===")
    now = datetime.datetime.now(UTC)
    now_pkt = now.astimezone(PKT).strftime("%Y-%m-%d %H:%M PKT")

    if now.weekday() >= 5:
        print(f"[skip] {now_pkt} weekend"); return

    print(f"[env] TD={'✓' if os.environ.get('TWELVEDATA_KEY') else '❌'} · "
          f"TG={'✓' if os.environ.get('TELEGRAM_TOKEN') else '❌'}")

    h1, h4, d1r, core_src = derive_core_tfs()
    if not h1 or not h4 or not d1r or len(h1) < 100 or len(d1r) < 10:
        msg = "🚨 GOLD ABORT\nCore TFs failed."
        print(msg); send_tg(msg); return

    live_p, live_src = get_live_quote()
    if live_p is None:
        live_p = h1[-1]["c"]; live_src = f"{core_src}(close)"
    print(f"[live] {live_p:.2f} from {live_src}")

    h1_close = h1[-1]["c"]
    gap = abs(live_p - h1_close)
    if gap > max(live_p * 0.01, 15.0):
        msg = f"⚠️ ABORT — MISMATCH\nLive {live_p:.2f} vs H1 {h1_close:.2f}"
        print(msg); send_tg(msg); return
    print(f"[data] CLEAN · gap=${gap:.2f}")

    m15, m15_src = derive_m15()

    S = {"m15": state(m15, "M15") if m15 else None,
         "h1": state(h1, "H1"), "h4": state(h4, "H4"), "d1": state(d1r, "D1")}
    if not (S["h1"] and S["h4"] and S["d1"]):
        msg = "🚨 GOLD ABORT\nState failed."
        print(msg); send_tg(msg); return

    m = S["m15"]; hh = S["h1"]; h4s = S["h4"]; dd = S["d1"]
    for tf in (m, hh, h4s, dd):
        if tf is not None: tf["price"] = live_p
    spot = live_p

    print(f"[main] H1 RSI={hh['rsi']:.0f} · ATR={hh['atr']:.2f}  "
          f"(M15 ATR={(m['atr'] if m else 0):.2f})")
    print(f"[main] D1 close={d1r[-1]['c']:.2f} · RSI={dd['rsi']:.0f}")
    print(f"bias · M15={(m['bias'] if m else 0):+.2f} H1={hh['bias']:+.2f} "
          f"H4={h4s['bias']:+.2f} D1={dd['bias']:+.2f}")

    print("[filters] Loading...")
    news_events = fetch_news_events()
    news_ok, news_event = check_news_blackout(now, news_events)
    dxy_change = fetch_dxy_4h_change()
    volume_ratio = fetch_gc_volume_ratio()
    ctx = {"news_ok": news_ok, "news_event": news_event,
           "dxy_change_4h": dxy_change, "volume_ratio": volume_ratio}
    n_up = len([e for e in news_events if (e["dt"] - now).total_seconds() > 0])
    print(f"[filters] News: {len(news_events)} this week ({n_up} upcoming) · "
          f"blackout={'YES' if not news_ok else 'no'}")
    print(f"[filters] DXY 4h: {dxy_change if dxy_change is not None else 'N/A'}%")
    print(f"[filters] Volume: {volume_ratio if volume_ratio is not None else 'N/A'}x")

    st = load_state()
    open_by_side = {}
    for s in st["open_signals"]:
        open_by_side[s["side"]] = open_by_side.get(s["side"], 0) + 1
    print(f"[state] open={len(st['open_signals'])} history={len(st['history'])} "
          f"· {open_by_side if open_by_side else 'none'}")
    
    resolved, still = check_open_signals(st, spot, now)
    st["open_signals"] = still
    for r in resolved:
        print(f"[tracker] {r['result']}: {r['side']} @ {r['entry']:.2f} → "
              f"{r['exit_price']:.2f} ({r['pips']:+.0f}p)")

    extreme = hh["rsi"] > 85 or hh["rsi"] < 15
    cands = []

    if extreme:
        print(f"[gate] RSI {hh['rsi']:.0f} EXTREME → reversal only")
        if m:
            for side, why in find_reversal(m, hh, h4s, dd):
                b = build("reversal", m, side, why, ref_atr=hh["atr"])
                if b: cands.append(("🔄 REVERSAL", "🔄", b))
        if not cands:
            print("[gate] no reversal confirmation")
    else:
        print(f"[gate] RSI {hh['rsi']:.0f} → normal mode")
        if m:
            for side, why in find_scalp(m, hh, h4s):
                b = build("scalp", m, side, why)
                if b: cands.append(("⚡ SCALP", "⚡", b))
        for side, why in find_intraday(hh, h4s):
            b = build("intraday", hh, side, why)
            if b: cands.append(("🎯 INTRADAY", "🎯", b))
        if m:
            for side, why in find_reversal(m, hh, h4s, dd):
                b = build("reversal", m, side, why, ref_atr=hh["atr"])
                if b: cands.append(("🔄 REVERSAL", "🔄", b))
        print(f"candidates={len(cands)}")

    found = filter_cooldown(cands, st)
    print(f"after filter={len(found)}")

    graded = []
    for name, emoji, sig in found:
        g, sc, notes = grade_signal(sig, ctx, h1_close=h1_close, ref_atr=hh["atr"])
        sig["grade"] = g; sig["score"] = sc; sig["filter_notes"] = notes
        print(f"[grade] {name}: {g} ({sc}) {notes}")
        graded.append((name, emoji, sig, g))

    to_fire = [(n, e, s) for n, e, s, g in graded if g in ("A", "B")]
    c_blocked = [(n, e, s) for n, e, s, g in graded if g == "C"]

    for name, emoji, sig in to_fire:
        sid = f"{sig['side'][0]}{now.strftime('%Y%m%d%H%M%S')}"
        st["open_signals"].append({**sig, "id": sid,
                                   "created_at": now.isoformat(),
                                   "max_hours": MAX_HOURS_OPEN})
    save_state(st)

    for n, e, s in to_fire:
        print(f"\n{n} — {fmt_signal(s, s.get('filter_notes'))}")

    if not to_fire and not resolved:
        last_hb = st.get("last_heartbeat"); should_hb = True
        if last_hb:
            try:
                lt = datetime.datetime.fromisoformat(last_hb)
                if lt.tzinfo is None: lt = lt.replace(tzinfo=UTC)
                if (now - lt).total_seconds() < HEARTBEAT_HOURS * 3600:
                    should_hb = False
            except Exception: pass
        send_c = False
        if c_blocked:
            last_c = st.get("last_c_warning"); send_c = True
            if last_c:
                try:
                    lt = datetime.datetime.fromisoformat(last_c)
                    if lt.tzinfo is None: lt = lt.replace(tzinfo=UTC)
                    if (now - lt).total_seconds() < 3600: send_c = False
                except Exception: pass
        if send_c:
            n, e, s = c_blocked[0]
            msg = (f"🟡 SIGNAL FILTERED [{s.get('grade','?')} · {s.get('score','?')}]\n"
                   f"Would fire: {'🟢 BUY' if s['side']=='LONG' else '🔴 SELL'} "
                   f"@ {s['entry']:.2f}\nWhy: {s['why']}\n")
            for note in s.get("filter_notes", []): msg += f"  {note}\n"
            msg += f"\n⏰ {now_pkt}"
            send_tg(msg); st["last_c_warning"] = now.isoformat()
            save_state(st); print("→ filtered-warning sent"); return
        if should_hb:
            lines = ["⏸ GOLD — No signal", f"⏰ {now_pkt}", f"💰 Spot: {spot:.2f}", "",
                     "📊 Market:",
                     f"  H1 RSI: {hh['rsi']:.0f}  ATR: {hh['atr']:.2f}",
                     f"  H1 bias: {hh['bias']:+.2f}",
                     f"  H4 bias: {h4s['bias']:+.2f}",
                     f"  D1 bias: {dd['bias']:+.2f}", "",
                     "🔍 Filters:",
                     f"  News: {'🚫' if not news_ok else '✅ clear'}",
                     f"  DXY: {dxy_change if dxy_change is not None else 'N/A'}%",
                     f"  Volume: {volume_ratio if volume_ratio is not None else 'N/A'}x",
                     "", f"🔓 Open: {len(st['open_signals'])}"]
            send_tg("\n".join(lines))
            st["last_heartbeat"] = now.isoformat()
            save_state(st); print("→ heartbeat sent")
        else: print("quiet")
        return

    parts = []
    if to_fire:
        head = f"🥇 GOLD — {now_pkt}\nSpot: {spot:.2f}"
        bodies = [f"{n} [{s.get('grade','?')}]\n{fmt_signal(s, s.get('filter_notes'))}"
                  for n, e, s in to_fire]
        parts.append(f"{head}\n\n" + "\n\n".join(bodies))
    if resolved: parts.append(fmt_prev(resolved))
    if to_fire or st["open_signals"]: parts.append(fmt_open(st))
    s = fmt_stats(st)
    if s: parts.append(s)
    send_tg("\n\n".join(p for p in parts if p))
    print("→ processed")


if __name__ == "__main__":
    main()
