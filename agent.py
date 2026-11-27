#!/usr/bin/env python3
"""
agent.py v5 — XAUUSD signals + tracking + spot-consistent + all fixes baked in
"""
import os, json, datetime, requests

UTC = datetime.timezone.utc
PKT = datetime.timezone(datetime.timedelta(hours=5))
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
PIP = 0.10
COOLDOWN_HOURS = 1
MAX_HOURS_OPEN = 24
MAX_SL_PIPS = 100
STATE_FILE = "signal_state.json"
MAX_AGE_M15_H = 1.0
MAX_AGE_H1_H  = 3.0
MAX_AGE_D1_H  = 72.0

CFG = {
    "scalp":    {"sl": 0.35, "max_sl": 10.0, "hold": "30m-4h"},
    "fast":     {"sl": 0.50, "max_sl": 10.0, "hold": "1-6h"},
    "intraday": {"sl": 0.65, "max_sl": 10.0, "hold": "4-24h"},
}


# ========== DATA SOURCES ==========
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
        j = r.json()
    except Exception:
        return []
    res = (j.get("chart") or {}).get("result")
    if not res:
        return []
    res = res[0]
    ts = res.get("timestamp") or []
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    o = q.get("open") or []
    h = q.get("high") or []
    l = q.get("low") or []
    c = q.get("close") or []
    out = []
    for i in range(len(ts)):
        if i >= len(c):
            break
        if None in (o[i], h[i], l[i], c[i]):
            continue
        out.append({"t": int(ts[i]), "o": float(o[i]), "h": float(h[i]),
                    "l": float(l[i]), "c": float(c[i])})
    return out


def fetch_twelvedata(interval="1h", outputsize=500):
    key = os.environ.get("TWELVEDATA_KEY")
    if not key:
        print("[td] no key")
        return []
    try:
        r = requests.get(
            "https://api.twelvedata.com/time_series",
            params={"symbol": "XAU/USD", "interval": interval,
                    "outputsize": outputsize, "apikey": key,
                    "timezone": "UTC", "order": "ASC"},
            timeout=20)
        j = r.json()
    except Exception as e:
        print(f"[td] request: {e}")
        return []
    if j.get("status") == "error" or "values" not in j:
        print(f"[td] error: {j.get('message','no values')}")
        return []
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
    if rows:
        print(f"[td] {len(rows)} bars · last={rows[-1]['c']:.2f} @ {j['values'][-1]['datetime']}")
    return rows


def fetch_fresh(interval, rng, max_age_h):
    attempts = [
        ("GC=F",     True,  "query1"),
        ("GC=F",     False, "query1"),
        ("GC=F",     True,  "query2"),
        ("XAUUSD=X", True,  "query1"),
        ("XAUUSD=X", False, "query1"),
    ]
    best = None
    for sym, periods, server in attempts:
        rows = fetch(interval, rng, sym, use_periods=periods, server=server)
        if not rows:
            continue
        age = (datetime.datetime.now(UTC).timestamp() - rows[-1]["t"]) / 3600.0
        tag = f"{sym} {interval} {server}{'/p' if periods else '/r'}"
        if -0.5 <= age <= max_age_h:
            print(f"[data] OK {tag} · {age:.1f}h · {len(rows)} bars")
            return rows, tag
        if best is None or abs(age) < abs(best[1]):
            best = (tag, age)
    td_map = {"1h":"1h","60m":"1h","15m":"15min","5m":"5min","1m":"1min","1d":"1day"}
    tid = td_map.get(interval)
    if tid:
        rows = fetch_twelvedata(tid)
        if rows:
            age = (datetime.datetime.now(UTC).timestamp() - rows[-1]["t"]) / 3600.0
            if -0.5 <= age <= max_age_h:
                print(f"[data] OK TwelveData {tid} · {age:.1f}h · {len(rows)} bars")
                return rows, f"TwelveData {tid}"
            print(f"[data] TwelveData {tid} STALE {age:.1f}h")
    if best:
        print(f"[data] FAIL {interval}: best {best[0]} @ {best[1]:.1f}h")
    else:
        print(f"[data] FAIL {interval}: no data")
    return [], None


def derive_h1():
    h1, src = fetch_fresh("1h", "60d", MAX_AGE_H1_H)
    if h1 and len(h1) >= 100:
        return h1, src
    for small, rng in (("15m","30d"), ("5m","10d"), ("1m","5d")):
        rows, src2 = fetch_fresh(small, rng, 1.0)
        if rows:
            h1 = bucketed(rows, 1)
            if len(h1) >= 100:
                print(f"[data] H1 from {src2} → {len(h1)} bars")
                return h1, f"{src2}→H1"
    return [], None


def derive_m15():
    m15, src = fetch_fresh("15m", "30d", MAX_AGE_M15_H)
    if m15 and len(m15) >= 60:
        return m15, src
    for small, rng in (("5m","10d"), ("1m","5d")):
        rows, src2 = fetch_fresh(small, rng, 1.0)
        if rows:
            m15 = bucketed(rows, 0.25)
            if len(m15) >= 60:
                print(f"[data] M15 from {src2} → {len(m15)} bars")
                return m15, f"{src2}→M15"
    return [], None


def get_live_quote(prefer_td=True):
    if prefer_td and os.environ.get("TWELVEDATA_KEY"):
        try:
            r = requests.get(
                "https://api.twelvedata.com/price",
                params={"symbol": "XAU/USD",
                        "apikey": os.environ["TWELVEDATA_KEY"]},
                timeout=15)
            p = float(r.json().get("price", 0))
            if p > 0:
                return p, 0.5, "TwelveData"
        except Exception as e:
            print(f"[live] TD: {e}")
    for name, url in [
        ("XAUUSD=X", "https://query1.finance.yahoo.com/v8/finance/chart/XAUUSD=X?interval=1m&range=1d"),
        ("GC=F",     "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?interval=1m&range=1d"),
    ]:
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
        except Exception:
            continue
    return None, None, None


# ========== HELPERS ==========
def bucketed(rows, hours):
    sec = hours * 3600
    b = {}
    for r in rows:
        k = r["t"] // sec
        if k not in b:
            b[k] = {"t": r["t"], "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"]}
        else:
            if r["h"] > b[k]["h"]:
                b[k]["h"] = r["h"]
            if r["l"] < b[k]["l"]:
                b[k]["l"] = r["l"]
            b[k]["c"] = r["c"]
    return [b[k] for k in sorted(b)]


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
    g = 0.0
    l = 0.0
    for i in range(1, len(seg)):
        d = seg[i] - seg[i - 1]
        if d > 0:
            g += d
        else:
            l += -d
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
    highs = [r["h"] for r in rows]
    lows = [r["l"] for r in rows]
    p = closes[-1]
    e20 = ema(closes, 20)[-1]
    e50 = ema(closes, 50)[-1]
    e200 = ema(closes, 200)[-1]
    rv = rsi_last(closes)
    a = atr_last(highs, lows, closes)
    if a <= 0:
        a = max(p * 0.0005, 0.01)
    score = 0
    score += 1 if p > e200 else -1
    score += 1 if e20 > e50 else -1
    score += 1 if p > e20 else -1
    return {"label": label, "price": p,
            "e20": e20, "e50": e50, "e200": e200,
            "rsi": rv, "atr": a, "bias": score / 3.0,
            "hi_prev": max(highs[-21:-1]) if len(highs) > 21 else max(highs),
            "lo_prev": min(lows[-21:-1]) if len(lows) > 21 else min(lows),
            "_bars": rows}


def build(kind, tf, side, why):
    cfg = CFG[kind]
    p = tf["price"]
    a = tf["atr"]
    mx = cfg["max_sl"]
    if side == "LONG":
        sl_a = p - cfg["sl"] * a
        sl_s = tf["lo_prev"] - 0.10 * a
        sl = max(sl_a, sl_s) if sl_s < p else sl_a
        if p - sl < 0.20 * a:
            sl = p - 0.20 * a
        if p - sl > mx:
            sl = p - mx
        risk = p - sl
        if risk <= 0.05:
            return None
        if risk / PIP > MAX_SL_PIPS:
            print(f"[build] rejected {side} {kind}: SL {risk/PIP:.0f}p > {MAX_SL_PIPS}p")
            return None
        tp1 = p + risk
        tp2 = p + risk * 2
    else:
        sl_a = p + cfg["sl"] * a
        sl_s = tf["hi_prev"] + 0.10 * a
        sl = min(sl_a, sl_s) if sl_s > p else sl_a
        if sl - p < 0.20 * a:
            sl = p + 0.20 * a
        if sl - p > mx:
            sl = p + mx
        risk = sl - p
        if risk <= 0.05:
            return None
        if risk / PIP > MAX_SL_PIPS:
            print(f"[build] rejected {side} {kind}: SL {risk/PIP:.0f}p > {MAX_SL_PIPS}p")
            return None
        tp1 = p - risk
        tp2 = p - risk * 2
    return {"kind": kind, "side": side, "entry": p, "sl": sl,
            "tp1": tp1, "tp2": tp2, "risk": risk,
            "why": why, "hold": cfg["hold"], "tf": tf["label"]}


# ========== SETUPS ==========
def find_scalp(m, hh, h4s):
    if not m or len(m["_bars"]) < 25:
        return []
    a = m["atr"]
    p = m["price"]
    rsi = m["rsi"]
    bars = m["_bars"]
    last = bars[-1]
    prev = bars[-2]
    out = []
    lb = last["c"] > last["o"]
    lbe = last["c"] < last["o"]
    pb = prev["c"] > prev["o"]
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
        if hh["bias"] >= 0.3 and h4s["bias"] >= 0.3 and lb:
            out.append(("LONG", "H1+H4 bull confluence"))
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
        if hh["bias"] <= -0.3 and h4s["bias"] <= -0.3 and lbe:
            out.append(("SHORT", "H1+H4 bear confluence"))
    if len(bars) >= 24:
        rl = min(b["l"] for b in bars[-21:-3])
        rh = max(b["h"] for b in bars[-21:-3])
        for b in bars[-3:]:
            if b["l"] < rl and b["c"] > rl and hh["bias"] >= 0:
                out.append(("LONG", f"M15 swept {rl:.2f}"))
                break
        for b in bars[-3:]:
            if b["h"] > rh and b["c"] < rh and hh["bias"] <= 0:
                out.append(("SHORT", f"M15 swept {rh:.2f}"))
                break
    return out


def find_fast(m, hh):
    if not m or len(m["_bars"]) < 3:
        return []
    a = m["atr"]
    bars = m["_bars"]
    last = bars[-1]
    prev = bars[-2]
    body = abs(last["c"] - last["o"])
    out = []
    if last["c"] > last["o"] and body > 0.55 * a and hh["bias"] >= 0:
        out.append(("LONG", f"M15 thrust ({body/a:.1f}xATR)"))
    if last["c"] < last["o"] and body > 0.55 * a and hh["bias"] <= 0:
        out.append(("SHORT", f"M15 thrust ({body/a:.1f}xATR)"))
    if (last["c"] > last["o"] and prev["c"] < prev["o"]
            and last["c"] > prev["o"] and last["o"] < prev["c"]):
        out.append(("LONG", "M15 bull engulfing"))
    if (last["c"] < last["o"] and prev["c"] > prev["o"]
            and last["c"] < prev["o"] and last["o"] > prev["c"]):
        out.append(("SHORT", "M15 bear engulfing"))
    return out


def find_intraday(hh, h4s):
    if not hh or len(hh["_bars"]) < 21:
        return []
    a = hh["atr"]
    p = hh["price"]
    rsi = hh["rsi"]
    bars = hh["_bars"]
    last = bars[-1]
    prev = bars[-2]
    out = []
    lb = last["c"] > last["o"]
    lbe = last["c"] < last["o"]
    pb = prev["c"] > prev["o"]
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
        if hh["bias"] >= 0.3 and h4s["bias"] >= 0.3:
            out.append(("LONG", "H1+H4 bull confluence"))
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
        if hh["bias"] <= -0.3 and h4s["bias"] <= -0.3:
            out.append(("SHORT", "H1+H4 bear confluence"))
    if len(bars) >= 24:
        rl = min(b["l"] for b in bars[-21:-3])
        rh = max(b["h"] for b in bars[-21:-3])
        for b in bars[-3:]:
            if b["l"] < rl and b["c"] > rl:
                out.append(("LONG", f"H1 swept {rl:.2f}"))
                break
        for b in bars[-3:]:
            if b["h"] > rh and b["c"] < rh:
                out.append(("SHORT", f"H1 swept {rh:.2f}"))
                break
    return out


# ========== STATE + TRACKING ==========
def load_state():
    st = {"open_signals": [], "history": [],
          "stats": {"total": 0, "wins": 0, "losses": 0,
                    "expired": 0, "breakeven": 0, "total_pips": 0}}
    if not os.path.exists(STATE_FILE):
        return st
    try:
        loaded = json.load(open(STATE_FILE))
    except Exception:
        return st
    st["open_signals"] = [s for s in loaded.get("open_signals", [])
                          if not str(s.get("id", "")).startswith("TEST-")]
    st["history"] = [s for s in loaded.get("history", [])
                     if not str(s.get("id", "")).startswith("TEST-")]
    for k in st["stats"]:
        try:
            st["stats"][k] = loaded.get("stats", {}).get(k, 0) or 0
        except Exception:
            st["stats"][k] = 0
    return st


def save_state(st):
    try:
        json.dump(st, open(STATE_FILE, "w"), indent=2, default=str)
    except Exception as e:
        print(f"[state] save failed: {e}")


def check_open_signals(st, price, now):
    resolved = []
    still = []
    for sig in st["open_signals"]:
        try:
            cr = datetime.datetime.fromisoformat(sig["created_at"])
            if cr.tzinfo is None:
                cr = cr.replace(tzinfo=UTC)
        except Exception:
            continue
        age = (now - cr).total_seconds() / 3600.0
        if age > sig.get("max_hours", MAX_HOURS_OPEN):
            sig.update({"result": "EXPIRED", "exit_price": price, "pips": 0.0,
                        "closed_at": now.isoformat(), "age_hours": round(age, 1)})
            resolved.append(sig)
            continue
        side = sig["side"]
        sl = sig["sl"]
        tp1 = sig["tp1"]
        tp2 = sig["tp2"]
        risk = sig["risk"]
        hit = None
        if side == "LONG":
            if price <= sl:
                hit = "SL"
            elif price >= tp2:
                hit = "TP2"
            elif price >= tp1:
                hit = "TP1"
        else:
            if price >= sl:
                hit = "SL"
            elif price <= tp2:
                hit = "TP2"
            elif price <= tp1:
                hit = "TP1"
        if hit == "SL":
            if sig.get("tp1_hit"):
                sig.update({"result": "BREAKEVEN", "pips": round(0.5 * risk / PIP, 1)})
            else:
                sig.update({"result": "LOSS", "pips": round(-risk / PIP, 1)})
            sig.update({"exit_price": sl, "closed_at": now.isoformat(),
                        "age_hours": round(age, 1)})
            resolved.append(sig)
        elif hit == "TP2":
            sig.update({"result": "WIN", "pips": round(1.5 * risk / PIP, 1),
                        "exit_price": tp2, "closed_at": now.isoformat(),
                        "age_hours": round(age, 1)})
            resolved.append(sig)
        elif hit == "TP1":
            if not sig.get("tp1_hit"):
                sig["tp1_hit"] = True
                sig["tp1_hit_at"] = now.isoformat()
                print(f"[tracker] {sig.get('id','?')} TP1 @ {tp1:.2f}")
            still.append(sig)
        else:
            still.append(sig)
    for sig in resolved:
        st["history"].append(sig)
        r = sig["result"]
        if r in ("WIN", "LOSS", "EXPIRED", "BREAKEVEN"):
            key = {"WIN": "wins", "LOSS": "losses",
                   "EXPIRED": "expired", "BREAKEVEN": "breakeven"}[r]
            st["stats"][key] += 1
        st["stats"]["total"] += 1
        st["stats"]["total_pips"] = round(st["stats"]["total_pips"] + sig.get("pips", 0), 1)
    st["history"] = st["history"][-100:]
    return resolved, still


def filter_cooldown(cands, st):
    now = datetime.datetime.now(UTC)
    out = []
    seen = set()
    for name, emoji, sig in cands:
        k = (sig["side"], round(sig["entry"], 1))
        if k in seen:
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
                    blocked = True
                    break
            except Exception:
                pass
        if blocked:
            continue
        seen.add(k)
        out.append((name, emoji, sig))

    before = len(out)
    out = [c for c in out if c[2]["risk"] / PIP <= MAX_SL_PIPS]
    if len(out) < before:
        print(f"[filter] rejected {before - len(out)} over-cap SL")

    if not out:
        return []

    sides = set(c[2]["side"] for c in out)
    if len(sides) > 1:
        def score(c):
            pips = c[2]["risk"] / PIP
            s = 200 - pips
            w = c[2]["why"].lower()
            if "confluence" in w:
                s += 30
            if "overbought" in w or "oversold" in w:
                s += 20
            return s
        best = max(out, key=score)
        print(f"[filter] contradictory ({len(out)} signals) → keeping {best[2]['side']}")
        return [best]

    return out[:1]


# ========== FORMAT ==========
def fmt_signal(s):
    pips = s["risk"] / PIP
    arrow = "🟢 BUY" if s["side"] == "LONG" else "🔴 SELL"
    grade = "🔥 HIGH" if pips <= 50 else ("✅ GOOD" if pips <= 80 else "🟡 OK")
    return (f"{arrow} @ {s['entry']:.2f}  [{grade}]\n"
            f"  SL:  {s['sl']:.2f}  ({pips:.0f}p / ${s['risk']:.2f})\n"
            f"  TP1: {s['tp1']:.2f}  (1R · close 50%, SL→BE)\n"
            f"  TP2: {s['tp2']:.2f}  (2R · rest)\n"
            f"  Why: {s['why']}\n"
            f"  Hold: {s['hold']} · {s['tf']}")


def fmt_prev(res):
    if not res:
        return ""
    lines = ["📈 LAST RESULT(S)"]
    for s in res:
        ic = {"WIN": "🟢", "LOSS": "🔴",
              "BREAKEVEN": "🟡", "EXPIRED": "⚪"}.get(s["result"], "⚪")
        arr = "BUY" if s["side"] == "LONG" else "SELL"
        lines.append(f"  {ic} {s['result']} · {arr} {s['entry']:.2f} → "
                     f"{s['exit_price']:.2f} ({s['pips']:+.0f}p) · {s['age_hours']}h")
    return "\n".join(lines)


def fmt_stats(st):
    s = st["stats"]
    if s["total"] == 0:
        return ""
    dec = s["wins"] + s["losses"]
    wr = (s["wins"] / dec * 100) if dec > 0 else 0
    return (f"📊 STATS ({s['total']})\n"
            f"  W:{s['wins']} L:{s['losses']} BE:{s['breakeven']} Exp:{s['expired']}\n"
            f"  Win: {wr:.1f}%  Total: {s['total_pips']:+.0f}p")


def fmt_open(st):
    if not st["open_signals"]:
        return ""
    lines = [f"🔓 OPEN ({len(st['open_signals'])})"]
    for s in st["open_signals"]:
        arr = "BUY" if s["side"] == "LONG" else "SELL"
        lines.append(f"  · {arr} @ {s['entry']:.2f} · SL {s['sl']:.2f} · TP2 {s['tp2']:.2f}")
    return "\n".join(lines)


# ========== TELEGRAM ==========
def send_tg(text):
    pairs = [("TG_TOKEN", "TG_CHAT"),
             ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"),
             ("TELEGRAM_TOKEN", "TELEGRAM_CHAT"),
             ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"),
             ("TELEGRAM_BOT_TOKEN", "CHAT_ID"),
             ("BOT_TOKEN", "CHAT_ID")]
    tk = ck = None
    for a, b in pairs:
        if os.environ.get(a) and os.environ.get(b):
            tk, ck = a, b
            break
    try:
        os.makedirs("signals", exist_ok=True)
        fn = f"signals/{datetime.datetime.now(UTC).strftime('%Y%m%d')}.log"
        with open(fn, "a") as f:
            f.write(f"\n\n=== {datetime.datetime.now(UTC).isoformat()} ===\n{text}\n")
    except Exception:
        pass
    if not tk:
        print("[tg] no env vars")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{os.environ[tk]}/sendMessage",
            data={"chat_id": os.environ[ck], "text": text},
            timeout=15)
        print(f"[tg] {tk}/{ck} → {r.status_code}")
        if r.status_code != 200:
            print(f"[tg] {r.text[:200]}")
    except Exception as e:
        print(f"[tg] {e}")


# ========== MAIN ==========
def main():
    print("=== agent.py v5 · all fixes baked in ===")
    now = datetime.datetime.now(UTC)
    now_pkt = now.astimezone(PKT).strftime("%Y-%m-%d %H:%M PKT")

    if now.weekday() >= 5:
        print(f"[skip] {now_pkt} — weekend, market closed")
        return

    td = os.environ.get("TWELVEDATA_KEY")
    tg_ok = any(os.environ.get(tk) and os.environ.get(ck)
                for tk, ck in [("TG_TOKEN", "TG_CHAT"),
                               ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"),
                               ("TELEGRAM_TOKEN", "TELEGRAM_CHAT"),
                               ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")])
    print(f"[env] TWELVEDATA_KEY={'✓' if td else '❌'} · TG={'✓' if tg_ok else '❌'}")

    h1, h1_src = derive_h1()
    if not h1 or len(h1) < 100:
        msg = "🚨 GOLD ABORT\nH1 staled on all sources."
        print(msg)
        send_tg(msg)
        return

    d1r, d1_src = fetch_fresh("1d", "1y", MAX_AGE_D1_H)
    if not d1r or len(d1r) < 100:
        msg = "🚨 GOLD ABORT\nD1 staled."
        print(msg)
        send_tg(msg)
        return

    if "TwelveData" in h1_src and "GC=F" in d1_src:
        td_d1 = fetch_twelvedata("1day", 500)
        if td_d1 and len(td_d1) >= 100:
            d1r = td_d1
            d1_src = "TwelveData 1day"
            print("[d1] switched to TwelveData for source consistency")

    print(f"[main] H1={h1_src} · {len(h1)} bars · close={h1[-1]['c']:.2f}")
    print(f"[main] D1={d1_src} · close={d1r[-1]['c']:.2f}")

    h4 = bucketed(h1, 4)
    m15, m15_src = derive_m15()

    live_p, live_age, live_src = get_live_quote(prefer_td=True)
    if live_p is None:
        live_p = h1[-1]["c"]
        live_src = f"{h1_src}(close)"
        print("[live] fallback to H1 close")
    print(f"[live] {live_p:.2f} from {live_src}")

    gap = abs(live_p - h1[-1]["c"])
    limit = max(live_p * 0.003, 3.0)
    if gap > limit:
        msg = (f"⚠️ GOLD ABORT — PRICE MISMATCH\n"
               f"Live: {live_p:.2f} ({live_src})\n"
               f"H1:   {h1[-1]['c']:.2f} ({h1_src})\n"
               f"Gap:  ${gap:.2f} > ${limit:.2f}")
        print(msg)
        send_tg(msg)
        return
    print(f"[data] CLEAN · gap=${gap:.2f}")

    S = {"m15": state(m15, "M15") if m15 else None,
         "h1": state(h1, "H1"),
         "h4": state(h4, "H4"),
         "d1": state(d1r, "D1")}
    if not (S["h1"] and S["h4"] and S["d1"]):
        msg = "🚨 GOLD ABORT\nState failed."
        print(msg)
        send_tg(msg)
        return

    m = S["m15"]
    hh = S["h1"]
    h4s = S["h4"]
    dd = S["d1"]
    for tf in (m, hh, h4s, dd):
        if tf is not None:
            tf["price"] = live_p
    spot = live_p

    print(f"spot={spot:.2f} · H1={hh['bias']:+.2f} · H4={h4s['bias']:+.2f} · D1={dd['bias']:+.2f}")

    st = load_state()
    print(f"[state] open={len(st['open_signals'])} · history={len(st['history'])}")

    resolved, still = check_open_signals(st, spot, now)
    st["open_signals"] = still
    for r in resolved:
        print(f"[tracker] {r['result']}: {r['side']} → {r['pips']:+.0f}p")

    if hh["rsi"] > 90 or hh["rsi"] < 10:
        print(f"[skip] H1 RSI {hh['rsi']:.0f} — extreme, no new signals")
        cands = []
    else:
        cands = []
        if m:
            for side, why in find_scalp(m, hh, h4s):
                b = build("scalp", m, side, why)
                if b:
                    cands.append(("⚡ SCALP", "⚡", b))
            for side, why in find_fast(m, hh):
                b = build("fast", m, side, why)
                if b:
                    cands.append(("💨 FAST", "💨", b))
        for side, why in find_intraday(hh, h4s):
            b = build("intraday", hh, side, why)
            if b:
                cands.append(("🎯 INTRADAY", "🎯", b))
        print(f"candidates={len(cands)}")

    found = filter_cooldown(cands, st)
    print(f"after filter={len(found)}")

    for name, emoji, sig in found:
        sid = f"{sig['side'][0]}{now.strftime('%Y%m%d%H%M%S')}"
        st["open_signals"].append({**sig, "id": sid,
                                   "created_at": now.isoformat(),
                                   "max_hours": MAX_HOURS_OPEN})
        print(f"\n{name} — {fmt_signal(sig)}")
    save_state(st)

    if not found and not resolved:
        print("quiet — no new signal, no resolution")
        return

    parts = []
    if found:
        head = f"🥇 GOLD — {now_pkt}\nSpot: {spot:.2f}"
        body = "\n\n".join(f"{n}\n{fmt_signal(s)}" for n, e, s in found)
        parts.append(f"{head}\n\n{body}")
    if resolved:
        parts.append(fmt_prev(resolved))
    if found:
        parts.append(fmt_open(st))
    s = fmt_stats(st)
    if s:
        parts.append(s)
    send_tg("\n\n".join(p for p in parts if p))
    print("→ processed")


if __name__ == "__main__":
    main()
