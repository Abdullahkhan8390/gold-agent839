#!/usr/bin/env python3
# signals.py — Gold multi-timeframe signal engine
# Tiers: DAILY BRIEF (always) · SCALP (M15) · INTRADAY (H1) · SWING (H4/D1)
import os
import datetime
import requests

UTC = datetime.timezone.utc
PKT = datetime.timezone(datetime.timedelta(hours=5))
SYMBOL = "GC=F"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}

TIER_CFG = {
    "scalp":    {"sl": 0.9, "tp1": 1.0, "tp2": 1.8, "hold": "2-8 hours"},
    "intraday": {"sl": 1.3, "tp1": 2.0, "tp2": 3.0, "hold": "1-2 days"},
    "swing":    {"sl": 2.0, "tp1": 4.0, "tp2": 6.0, "hold": "3-10 days"},
}


# ─────────────────────────── data ───────────────────────────
def fetch(interval, rng):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{SYMBOL}"
    params = {"interval": interval, "range": rng}
    try:
        r = requests.get(url, params=params, headers=UA, timeout=30)
        j = r.json()
    except Exception as e:
        print(f"[data] {interval} fetch failed: {e}")
        return []
    res = (j.get("chart") or {}).get("result")
    if not res:
        print(f"[data] {interval} returned nothing")
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


# ───────────────────────── indicators ───────────────────────
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
        "last_t": rows[-1]["t"],
    }


# ─────────────────────── setup builder ──────────────────────
def build(kind, tf, side, why):
    cfg = TIER_CFG[kind]
    price, a = tf["price"], tf["atr"]
    if side == "LONG":
        sl_atr = price - cfg["sl"] * a
        sl_struct = tf["lo_prev"] - 0.15 * a
        sl = max(sl_atr, sl_struct) if (price - sl_struct) <= 2.2 * a else sl_atr
        risk = price - sl
        tp1 = price + cfg["tp1"] * a
        tp2 = price + cfg["tp2"] * a
    else:
        sl_atr = price + cfg["sl"] * a
        sl_struct = tf["hi_prev"] + 0.15 * a
        sl = min(sl_atr, sl_struct) if (sl_struct - price) <= 2.2 * a else sl_atr
        risk = sl - price
        tp1 = price - cfg["tp1"] * a
        tp2 = price - cfg["tp2"] * a
    if risk <= 0:
        return None
    return {"kind": kind, "side": side, "entry": price, "sl": sl,
            "tp1": tp1, "tp2": tp2, "risk": risk,
            "rr1": abs(tp1 - price) / risk, "rr2": abs(tp2 - price) / risk,
            "why": why, "hold": cfg["hold"], "tf": tf["label"]}


def print_setup(title, emoji, s):
    arrow = "LONG 🟢" if s["side"] == "LONG" else "SHORT 🔴"
    print(f"\n{emoji} {title} — ACTIONABLE {arrow}")
    print(f"   Setup     : {s['why']}  [{s['tf']}]")
    print(f"   Entry     : {s['entry']:.2f}")
    print(f"   Stop loss : {s['sl']:.2f}   (risk ${s['risk']:.2f}/oz)")
    print(f"   Target 1  : {s['tp1']:.2f}   (R:R {s['rr1']:.1f})")
    print(f"   Target 2  : {s['tp2']:.2f}   (R:R {s['rr2']:.1f})")
    print(f"   Hold time : {s['hold']}")


def print_wait(title, emoji, waits):
    print(f"\n{emoji} {title} — NO SETUP (waiting)")
    for w in waits:
        print(f"   · {w}")


# ──────────────────────────── main ──────────────────────────
def main():
    m15 = fetch("15m", "30d")
    h1 = fetch("1h", "730d")
    d1r = fetch("1d", "5y")
    h4 = bucketed(h1, 4) if h1 else []

    if not h1 or len(h1) < 300:
        print("❌ H1 data unavailable — cannot run")
        return

    S = {"m15": state(m15, "M15") if m15 else None,
         "h1": state(h1, "H1"),
         "h4": state(h4, "H4") if h4 else None,
         "d1": state(d1r, "D1") if d1r else None}

    if not (S["h1"] and S["h4"] and S["d1"]):
        print("❌ Not enough higher-timeframe data")
        return

    m, hh, h4s, dd = S["m15"], S["h1"], S["h4"], S["d1"]
    spot = hh["price"]
    now_pkt = datetime.datetime.now(PKT).strftime("%Y-%m-%d %H:%M PKT")
    fresh = datetime.datetime.fromtimestamp(hh["last_t"], PKT).strftime("%Y-%m-%d %H:%M")

    print("\n" + "═" * 66)
    print("  🥇 GOLD MULTI-TIMEFRAME SIGNAL REPORT")
    print(f"  {now_pkt}   |   GC=F ≈ {spot:.2f}")
    print(f"  Last H1 bar: {fresh} PKT")
    print(f"  Data: M15 {len(m15)} · H1 {len(h1)} · H4 {len(h4)} · D1 {len(d1r)}")
    print("═" * 66)

    # ── DAILY BRIEF (always) ────────────────────────────────
    b = dd["bias"]
    bias_txt = "🟢 BULLISH" if b >= 0.33 else ("🔴 BEARISH" if b <= -0.33 else "🟡 MIXED")
    print(f"\n📅 DAILY BRIEF  (published every run — zero silence)")
    print(f"   D1 bias        : {bias_txt}  (score {b:+.2f})")
    print(f"     · price vs D1 EMA200 : {'above ✅' if dd['price'] > dd['e200'] else 'below ❌'}")
    print(f"     · D1 EMA20 vs EMA50  : {'bull ✅' if dd['e20'] > dd['e50'] else 'bear ❌'}")
    print(f"     · price vs D1 EMA20  : {'above ✅' if dd['price'] > dd['e20'] else 'below ❌'}")
    print(f"   D1 RSI(14)     : {dd['rsi']:.1f}   "
          f"({'overbought' if dd['rsi'] > 70 else 'oversold' if dd['rsi'] < 30 else 'neutral'})")
    print(f"   D1 ATR(14)     : {dd['atr']:.2f}   (avg daily range)")

    if len(d1r) >= 2:
        prev = d1r[-2]
        print(f"\n   📍 Key levels")
        print(f"     Prev day  H {prev['h']:.2f}  ·  L {prev['l']:.2f}  ·  C {prev['c']:.2f}")
        print(f"     Last 5d   H {max(r['h'] for r in d1r[-5:]):.2f}  ·  "
              f"L {min(r['l'] for r in d1r[-5:]):.2f}")
    print(f"     D1 EMA20 {dd['e20']:.2f} · EMA50 {dd['e50']:.2f} · EMA200 {dd['e200']:.2f}")

    print(f"\n   🔒 Bias lock  : only LONG above {hh['hi_prev']:.2f} · "
          f"only SHORT below {hh['lo_prev']:.2f} · between = no trade")
    print(f"   🕐 Sessions   : London 12:00 PKT · NY 18:30 PKT")

    # ── tier evaluation ─────────────────────────────────────
    found = 0

    # SCALP — M15
    if m:
        a15 = m["atr"]
        pull_up = (0 <= m["price"] - m["e20"] <= 0.7 * a15)
        pull_dn = (0 <= m["e20"] - m["price"] <= 0.7 * a15)
        brk_up = m["price"] >= m["hi_prev"]
        brk_dn = m["price"] <= m["lo_prev"]
        sig = None
        if hh["bias"] > 0 and m["rsi"] < 72 and (pull_up or brk_up):
            why = "H1 up + " + ("M15 breakout above 20-bar high" if brk_up
                                else "M15 pullback holding EMA20")
            sig = build("scalp", m, "LONG", why)
        elif hh["bias"] < 0 and m["rsi"] > 28 and (pull_dn or brk_dn):
            why = "H1 down + " + ("M15 breakdown below 20-bar low" if brk_dn
                                  else "M15 pullback rejecting EMA20")
            sig = build("scalp", m, "SHORT", why)
        if sig:
            print_setup("⚡ SCALP", "⚡", sig)
            found += 1
        else:
            print_wait("⚡ SCALP", "⚡", [
                f"LONG if H1 stays up + M15 closes above {m['hi_prev']:.2f}",
                f"LONG if pullback to M15 EMA20 {m['e20']:.2f} and holds (RSI 40-68)",
                f"SHORT if M15 closes below {m['lo_prev']:.2f} with H1 down",
            ])
    else:
        print_wait("⚡ SCALP", "⚡", ["M15 data unavailable — tier skipped"])

    # INTRADAY — H1
    a1 = hh["atr"]
    pull_up = (0 <= hh["price"] - hh["e20"] <= 0.8 * a1)
    pull_dn = (0 <= hh["e20"] - hh["price"] <= 0.8 * a1)
    brk_up = hh["price"] >= hh["hi_prev"]
    brk_dn = hh["price"] <= hh["lo_prev"]
    sig = None
    if h4s["bias"] > 0 and hh["bias"] > 0 and hh["rsi"] < 74 and (pull_up or brk_up):
        why = "H4 bull + H1 " + ("breakout" if brk_up else "pullback to EMA20")
        sig = build("intraday", hh, "LONG", why)
    elif h4s["bias"] < 0 and hh["bias"] < 0 and hh["rsi"] > 26 and (pull_dn or brk_dn):
        why = "H4 bear + H1 " + ("breakdown" if brk_dn else "pullback to EMA20")
        sig = build("intraday", hh, "SHORT", why)
    if sig:
        print_setup("🎯 INTRADAY", "🎯", sig)
        found += 1
    else:
        print_wait("🎯 INTRADAY", "🎯", [
            f"needs H4 bias {'up' if h4s['bias'] <= 0 else 'down'} + H1 trigger",
            f"LONG trigger: H1 close above {hh['hi_prev']:.2f}  (or pullback to {hh['e20']:.2f})",
            f"SHORT trigger: H1 close below {hh['lo_prev']:.2f}  (or rally to {hh['e20']:.2f})",
        ])

    # SWING — H4 + D1
    a4 = h4s["atr"]
    pull_up4 = (0 <= h4s["price"] - h4s["e20"] <= 0.9 * a4)
    pull_dn4 = (0 <= h4s["e20"] - h4s["price"] <= 0.9 * a4)
    sig = None
    if dd["bias"] >= 0.33 and h4s["bias"] > 0 and (pull_up4 or h4s["price"] >= h4s["hi_prev"]):
        why = "D1 bull + H4 " + ("breakout" if h4s["price"] >= h4s["hi_prev"]
                                 else "pullback to EMA20")
        sig = build("swing", h4s, "LONG", why)
    elif dd["bias"] <= -0.33 and h4s["bias"] < 0 and (pull_dn4 or h4s["price"] <= h4s["lo_prev"]):
        why = "D1 bear + H4 " + ("breakdown" if h4s["price"] <= h4s["lo_prev"]
                                 else "pullback to EMA20")
        sig = build("swing", h4s, "SHORT", why)
    if sig:
        print_setup("🌊 SWING", "🌊", sig)
        found += 1
    else:
        print_wait("🌊 SWING", "🌊", [
            f"needs D1 bias clear + H4 pullback (D1 bias now {dd['bias']:+.2f})",
            f"H4 EMA20 = {h4s['e20']:.2f} · H4 20-bar range {h4s['lo_prev']:.2f}–{h4s['hi_prev']:.2f}",
        ])

    # ── status ──────────────────────────────────────────────
    print("\n" + "─" * 66)
    if found:
        print(f"  ✅ STATUS: {found} actionable setup(s) — plan above")
    else:
        print("  ⏳ STATUS: NO ACTION right now — 3 trigger maps published above")
        print("     Market khamosh nahi, bot khamosh nahi. Triggers hit hote hi signal aayega.")
    print("  ⚠️  Technical-only engine. Macro/LLM layer included nahi.")
    print("─" * 66 + "\n")

    # ── optional Telegram ───────────────────────────────────
    token = os.environ.get("TG_TOKEN")
    chat = os.environ.get("TG_CHAT")
    if token and chat:
        try:
            import io, contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                pass
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat,
                                "text": f"Gold scan complete — see log"}, timeout=15)
        except Exception as e:
            print("telegram skipped:", e)


if __name__ == "__main__":
    main()
      
