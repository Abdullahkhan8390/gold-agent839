#!/usr/bin/env python3
# Gold Agent — H1 XAUUSD signal bot (100% free stack)

import os, math, datetime, requests

TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TG_CHAT  = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

CONTRACT_SIZE = 100.0   # XAUUSD ~100 oz per 1.00 lot (Exness spec check karein)
RISK_PCT      = 0.01
EQUITY_USD    = float(os.environ.get("EQUITY_USD", "0") or 0)

ENTRY_T, STRONG_T = 0.35, 0.60
UTC = datetime.timezone.utc


# ───────────── sanity checks ─────────────
if not TG_TOKEN or not TG_CHAT:
    print("FATAL: TELEGRAM_TOKEN / TELEGRAM_CHAT_ID missing")
    raise SystemExit(1)


# ───────────── price sources ─────────────
def kraken_ohlc():
    r = requests.get("https://api.kraken.com/0/public/OHLC",
                     params={"pair": "PAXGUSD", "interval": 60}, timeout=25)
    j = r.json()
    if j.get("error"):
        raise RuntimeError(str(j["error"]))
    res = j["result"]
    key = [k for k in res if k != "last"][0]
    rows = res[key][:-1]                       # drop incomplete candle
    return [{"t": int(x[0]), "h": float(x[2]), "l": float(x[3]),
             "c": float(x[4])} for x in rows]


def binance_ohlc():
    r = requests.get("https://api.binance.com/api/v3/klines",
                     params={"symbol": "PAXGUSDT", "interval": "1h",
                             "limit": 500}, timeout=25)
    data = r.json()
    if isinstance(data, dict):
        raise RuntimeError(str(data))
    return [{"t": int(x[0]) // 1000, "h": float(x[2]), "l": float(x[3]),
             "c": float(x[4])} for x in data[:-1]]


def fetch_prices():
    errs = []
    for fn in (kraken_ohlc, binance_ohlc):
        try:
            rows = fn()
            if len(rows) >= 210:
                print(f"price source: {fn.__name__} ({len(rows)} candles)")
                return rows
            errs.append(f"{fn.__name__}: only {len(rows)}")
        except Exception as e:
            errs.append(f"{fn.__name__}: {e}")
    raise RuntimeError("all price sources failed -> " + " | ".join(errs))


# ───────────── indicators ─────────────
def ema(v, n):
    if len(v) < n:
        return v[-1]
    k = 2 / (n + 1)
    e = sum(v[:n]) / n
    for x in v[n:]:
        e = x * k + e * (1 - k)
    return e


def rsi(v, n=14):
    if len(v) < n + 1:
        return 50.0
    g = l = 0.0
    for i in range(1, n + 1):
        d = v[i] - v[i - 1]
        g += max(d, 0); l += max(-d, 0)
    ag, al = g / n, l / n
    for i in range(n + 1, len(v)):
        d = v[i] - v[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def atr(h, l, c, n=14):
    if len(c) < n + 1:
        return 0.0
    trs = [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
           for i in range(1, len(c))]
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


# ───────────── macro (no API key needed) ─────────────
def fred_last(sid, want=6):
    r = requests.get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}",
                     timeout=25)
    r.raise_for_status()
    vals = []
    for line in r.text.strip().splitlines()[1:]:
        p = line.split(",")
        if len(p) < 2:
            continue
        try:
            vals.append(float(p[1]))
        except ValueError:
            pass
    return vals[-want:]


def macro_block():
    m = {"yield_chg": None, "dxy_pct": None}
    try:
        y = fred_last("DFII10")
        if len(y) >= 2:
            m["yield_chg"] = y[-1] - y[0]
    except Exception as e:
        print("DFII10 fail:", e)
    try:
        d = fred_last("DTWEXBGS")
        if len(d) >= 2 and d[0]:
            m["dxy_pct"] = (d[-1] / d[0] - 1) * 100
    except Exception as e:
        print("DTWEXBGS fail:", e)
    return m


# ───────────── scoring ─────────────
def score(rows, m):
    c = [x["c"] for x in rows]
    h = [x["h"] for x in rows]
    l = [x["l"] for x in rows]
    if len(c) < 210:
        raise RuntimeError("not enough candles")

    price = c[-1]
    e20, e50, e200 = ema(c, 20), ema(c, 50), ema(c, 200)
    r, a = rsi(c), atr(h, l, c)

    trend = (0.6 if price > e200 else -0.6) + (0.4 if e20 > e50 else -0.4)

    if r >= 72:   mom = -0.8
    elif r <= 28: mom =  0.8
    else:         mom = (r - 50) / 35.0

    y = m.get("yield_chg")
    ys = -math.tanh(y * 6) if y is not None else 0.0

    dp = m.get("dxy_pct")
    ds = -math.tanh(dp * 0.8) if dp is not None else 0.0

    total = 0.40 * trend + 0.15 * mom + 0.25 * ys + 0.20 * ds

    sw_lo = min(l[-12:]); sw_hi = max(h[-12:])

    return dict(price=price, e20=e20, e50=e50, e200=e200, rsi=r, atr=a,
                trend=trend, mom=mom, ys=ys, ds=ds, total=total,
                sw_lo=sw_lo, sw_hi=sw_hi, ts=rows[-1]["t"])


# ───────────── events ─────────────
def events_next(hours=30):
    try:
        r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json",
                         timeout=25, headers={"User-Agent": "Mozilla/5.0"})
        data = r.json()
    except Exception as e:
        print("calendar fail:", e)
        return []
    now = datetime.datetime.now(UTC)
    out = []
    for ev in data:
        if ev.get("country") != "USD" or ev.get("impact") != "High":
            continue
        try:
            dt = datetime.datetime.fromisoformat(ev["date"])
        except Exception:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        dh = (dt - now).total_seconds() / 3600
        if -0.5 < dh <= hours:
            out.append((dt, ev.get("title", "?"), dh))
    out.sort()
    return out


# ───────────── telegram ─────────────
def send(text):
    for chunk in [text[i:i + 3900] for i in range(0, len(text), 3900)]:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data={"chat_id": TG_CHAT, "text": chunk,
                                "parse_mode": "HTML",
                                "disable_web_page_preview": "true"}, timeout=25)
        if not r.ok:
            print("TG ERROR:", r.status_code, r.text[:300])
        else:
            print("sent ok")


# ───────────── messages ─────────────
def build_signal(c, m, evs):
    price, a = c["price"], c["atr"]

    if c["total"] > 0:
        side, dot = "BUY", "🟢"
        sl = min(c["sw_lo"], price - 1.2 * a)
        risk = price - sl
        tps = [price + risk * k for k in (1, 2, 3)]
        inv = f"{sl:.2f} close neeche → idea cancel"
    else:
        side, dot = "SELL", "🔴"
        sl = max(c["sw_hi"], price + 1.2 * a)
        risk = sl - price
        tps = [price - risk * k for k in (1, 2, 3)]
        inv = f"{sl:.2f} close upar → idea cancel"

    conf = min(95, round(abs(c["total"]) * 75 + 15))
    candle = datetime.datetime.fromtimestamp(c["ts"], UTC).strftime("%d %b %H:%M")

    lines = [
        f"{dot} <b>XAUUSD — {side}</b>",
        f"<i>H1 · candle close {candle} UTC</i>",
        "",
        f"<b>Entry</b>   {price:.2f}",
        f"<b>SL</b>      {sl:.2f}   ({risk:.2f})",
        f"<b>TP1</b>     {tps[0]:.2f}   (1R)",
        f"<b>TP2</b>     {tps[1]:.2f}   (2R)",
        f"<b>TP3</b>     {tps[2]:.2f}   (3R)",
        "",
        f"<b>Confidence</b> {conf}%   ·   score {c['total']:+.2f}",
        "",
        "<b>Wajah</b>",
    ]

    lines.append("• " + ("Price EMA200 ke upar, EMA20 > EMA50 (bull trend)"
                         if c["trend"] > 0 else
                         "Price EMA200 ke neeche, EMA20 < EMA50 (bear trend)"))
    lines.append(f"• RSI {c['rsi']:.0f} — " +
                 ("overbought, buy se bachein" if c["rsi"] > 72 else
                  "oversold, sell se bachein" if c["rsi"] < 28 else "neutral"))
    if m["yield_chg"] is not None:
        lines.append(f"• Real yield 10y {m['yield_chg']:+.2f}pt → " +
                     ("bullish" if m["yield_chg"] < 0 else "bearish"))
    if m["dxy_pct"] is not None:
        lines.append(f"• Dollar index {m['dxy_pct']:+.1f}% → " +
                     ("bullish" if m["dxy_pct"] < 0 else "bearish"))

    loss_1lot = risk * CONTRACT_SIZE
    lines += ["", "<b>Risk / Lot size</b>",
              f"1.00 lot par SL hit = ${loss_1lot:.0f}",
              f"0.10 lot par = ${loss_1lot*0.1:.1f}",
              f"0.01 lot par = ${loss_1lot*0.01:.2f}"]

    if EQUITY_USD > 0:
        lots = (EQUITY_USD * RISK_PCT) / (risk * CONTRACT_SIZE)
        lines.append(f"1% risk par (${EQUITY_USD:.0f} equity) ≈ {lots:.2f} lot")

    lines += ["", f"<b>Invalidation:</b> {inv}"]

    near = [e for e in evs if e[2] <= 12]
    if near:
        lines += ["", "⚠️ <b>HIGH IMPACT jald</b>"]
        for dt, title, dh in near[:3]:
            lines.append(f"• {dt.strftime('%H:%M')} UTC — {title} ({dh:.0f}h)")
        lines.append("<i>Size chhota rakhein ya wait karein.</i>")

    return "\n".join(lines)


def build_daily(c, m, evs):
    bias = ("BULLISH" if c["total"] >= ENTRY_T else
            "BEARISH" if c["total"] <= -ENTRY_T else "NEUTRAL / no trade")
    lines = ["📊 <b>XAUUSD — Daily Bias</b>",
             f"<i>{datetime.datetime.now(UTC).strftime('%d %b %Y')}</i>", "",
             f"<b>Bias</b>   {bias}",
             f"<b>Score</b>  {c['total']:+.2f}  (-1 se +1)",
             f"<b>Price</b>  {c['price']:.2f}",
             f"<b>ATR</b>    {c['atr']:.2f}  (roz ki avg range)",
             f"<b>RSI</b>    {c['rsi']:.0f}", "",
             "<b>Aaj ke events</b>"]
    if evs:
        for dt, title, dh in evs[:6]:
            lines.append(f"• {dt.strftime('%H:%M')} UTC — {title}")
    else:
        lines.append("• Koi high-impact USD event nahi")
    lines += ["", "<i>Yeh analysis hai, guarantee nahi. SL lazmi.</i>"]
    return "\n".join(lines)


# ───────────── main ─────────────
def main():
    rows = fetch_prices()
    m = macro_block()

    cur = score(rows, m)
    prev = score(rows[:-1], m)

    d_now = 1 if cur["total"] >= ENTRY_T else (-1 if cur["total"] <= -ENTRY_T else 0)
    d_old = 1 if prev["total"] >= ENTRY_T else (-1 if prev["total"] <= -ENTRY_T else 0)

    flip = d_now != 0 and d_now != d_old
    strong = abs(cur["total"]) >= STRONG_T
    near_ema = abs(cur["price"] - cur["e20"]) <= 0.35 * cur["atr"]

    evs = events_next()
    now = datetime.datetime.now(UTC)

    print(f"score={cur['total']:+.2f} prev={prev['total']:+.2f} "
          f"flip={flip} strong={strong} near={near_ema}")

    if flip or (strong and near_ema):
        send(build_signal(cur, m, evs))
    elif now.hour == 7:
        send(build_daily(cur, m, evs))
    else:
        print("no signal — staying quiet")


if __name__ == "__main__":
    main()
          
