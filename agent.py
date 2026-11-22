#!/usr/bin/env python3
# Gold Agent v2 — H1 XAUUSD + LLM + macro + news + ETF + weekend guard
import os, math, datetime, requests, json
import xml.etree.ElementTree as ET

TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TG_CHAT  = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
GROQ_KEY = os.environ.get("GROQ_API_KEY", "").strip()

CONTRACT_SIZE = 100.0
RISK_PCT      = 0.01
EQUITY_USD    = float(os.environ.get("EQUITY_USD", "0") or 0)

ENTRY_T, STRONG_T = 0.35, 0.60
UTC = datetime.timezone.utc

if not TG_TOKEN or not TG_CHAT:
    print("FATAL: TG secrets missing"); raise SystemExit(1)


def is_weekend():
    return datetime.datetime.now(UTC).weekday() >= 5


def kraken_ohlc():
    r = requests.get("https://api.kraken.com/0/public/OHLC",
                     params={"pair": "PAXGUSD", "interval": 60}, timeout=25)
    j = r.json()
    if j.get("error"): raise RuntimeError(str(j["error"]))
    res = j["result"]; key = [k for k in res if k != "last"][0]
    rows = res[key][:-1]
    return [{"t": int(x[0]), "h": float(x[2]), "l": float(x[3]), "c": float(x[4])} for x in rows]


def binance_ohlc():
    r = requests.get("https://api.binance.com/api/v3/klines",
                     params={"symbol": "PAXGUSDT", "interval": "1h", "limit": 500}, timeout=25)
    data = r.json()
    if isinstance(data, dict): raise RuntimeError(str(data))
    return [{"t": int(x[0])//1000, "h": float(x[2]), "l": float(x[3]), "c": float(x[4])} for x in data[:-1]]


def fetch_prices():
    errs = []
    for fn in (kraken_ohlc, binance_ohlc):
        try:
            rows = fn()
            if len(rows) >= 210:
                print(f"price: {fn.__name__} ({len(rows)})")
                return rows
            errs.append(f"{fn.__name__}: {len(rows)}")
        except Exception as e:
            errs.append(f"{fn.__name__}: {e}")
    raise RuntimeError("price fail: " + " | ".join(errs))


def ema(v, n):
    if len(v) < n: return v[-1]
    k = 2/(n+1); e = sum(v[:n])/n
    for x in v[n:]: e = x*k + e*(1-k)
    return e


def rsi(v, n=14):
    if len(v) < n+1: return 50.0
    g = l = 0.0
    for i in range(1, n+1):
        d = v[i]-v[i-1]; g += max(d,0); l += max(-d,0)
    ag, al = g/n, l/n
    for i in range(n+1, len(v)):
        d = v[i]-v[i-1]
        ag = (ag*(n-1) + max(d,0))/n
        al = (al*(n-1) + max(-d,0))/n
    if al == 0: return 100.0
    return 100 - 100/(1 + ag/al)


def atr(h, l, c, n=14):
    if len(c) < n+1: return 0.0
    trs = [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])) for i in range(1, len(c))]
    a = sum(trs[:n])/n
    for tr in trs[n:]: a = (a*(n-1) + tr)/n
    return a


def live_dxy():
    try:
        r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB",
                         params={"range": "5d", "interval": "1h"},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
        j = r.json()
        closes = [x for x in j["chart"]["result"][0]["indicators"]["quote"][0]["close"] if x]
        if len(closes) >= 2:
            return (closes[-1]/closes[0] - 1) * 100
    except Exception as e:
        print("dxy fail:", e)
    return None


def fred_yield_chg():
    try:
        r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFII10", timeout=25)
        r.raise_for_status()
        vals = []
        for line in r.text.strip().splitlines()[1:]:
            p = line.split(",")
            if len(p) < 2: continue
            try: vals.append(float(p[1]))
            except ValueError: pass
        if len(vals) >= 6: return vals[-1] - vals[-6]
        if len(vals) >= 2: return vals[-1] - vals[0]
    except Exception as e:
        print("DFII10 fail:", e)
    return None


def google_news():
    try:
        q = '"gold price" OR "central bank gold" OR "gold ETF" when:1d'
        url = ("https://news.google.com/rss/search?q="
               + requests.utils.quote(q) + "&hl=en-US&gl=US&ceid=US:en")
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        return [{"title": item.findtext("title") or "",
                 "src": item.findtext("source") or ""}
                for item in root.findall(".//item")[:15]]
    except Exception as e:
        print("gnews fail:", e); return []


def fed_news():
    try:
        r = requests.get("https://www.federalreserve.gov/feeds/press_all.xml",
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        return [{"title": item.findtext("title") or "",
                 "date": item.findtext("pubDate") or ""}
                for item in root.findall(".//item")[:10]]
    except Exception as e:
        print("fed fail:", e); return []


def gld_flow():
    try:
        r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/GLD",
                         params={"range": "1mo", "interval": "1d"},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
        j = r.json()
        closes = [x for x in j["chart"]["result"][0]["indicators"]["quote"][0]["close"] if x]
        if len(closes) >= 5:
            return (closes[-1]/closes[-5] - 1) * 100
    except Exception as e:
        print("gld fail:", e)
    return None


def llm_sentiment(news_items, fed_items):
    if not GROQ_KEY:
        return None
    headlines = "\n".join(f"- {x['title']}" for x in news_items[:10]) or "(none)"
    fedlines  = "\n".join(f"- {x['title']}" for x in fed_items[:5])  or "(none)"
    prompt = f"""You are a gold trading analyst. Analyze these headlines. Return STRICT JSON only.

GOLD / GEOPOLITICAL / CENTRAL BANK / ETF NEWS:
{headlines}

FED NEWS:
{fedlines}

Return ONLY this JSON (no markdown, no extra text):
{{"geopolitical": <-1 to 1>, "fed": <-1 to 1>, "reason_geo": "<short reason>", "reason_fed": "<short reason>"}}

RULES:
- geopolitical: war/sanctions/crisis/central-bank-buying = POSITIVE (gold up). peace/de-escalation/CB-selling = NEGATIVE.
- fed: hawkish (hikes/tightening) = NEGATIVE for gold. dovish (cuts/easing) = POSITIVE.
- Both values MUST be floats between -1 and +1.
"""
    try:
        r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                          headers={"Authorization": f"Bearer {GROQ_KEY}",
                                   "Content-Type": "application/json"},
                          json={"model": "llama-3.3-70b-versatile",
                                "messages": [{"role": "user", "content": prompt}],
                                "temperature": 0.1, "max_tokens": 200,
                                "response_format": {"type": "json_object"}},
                          timeout=30)
        r.raise_for_status()
        data = json.loads(r.json()["choices"][0]["message"]["content"])
        return {"geopolitical": max(-1, min(1, float(data.get("geopolitical", 0)))),
                "fed":          max(-1, min(1, float(data.get("fed", 0)))),
                "reason_geo":   str(data.get("reason_geo",""))[:200],
                "reason_fed":   str(data.get("reason_fed",""))[:200]}
    except Exception as e:
        print("llm fail:", e); return None


def events_next(hours=30):
    try:
        r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json",
                         timeout=25, headers={"User-Agent": "Mozilla/5.0"})
        data = r.json()
    except Exception as e:
        print("calendar fail:", e); return []
    now = datetime.datetime.now(UTC); out = []
    for ev in data:
        if ev.get("country") != "USD" or ev.get("impact") != "High": continue
        try: dt = datetime.datetime.fromisoformat(ev["date"])
        except Exception: continue
        if dt.tzinfo is None: dt = dt.replace(tzinfo=UTC)
        dh = (dt - now).total_seconds()/3600
        if -0.5 < dh <= hours: out.append((dt, ev.get("title","?"), dh))
    out.sort(); return out


def score(rows, macro, sentiment, gld):
    c = [x["c"] for x in rows]; h = [x["h"] for x in rows]; l = [x["l"] for x in rows]
    if len(c) < 210: raise RuntimeError("not enough candles")
    price = c[-1]
    e20, e50, e200 = ema(c,20), ema(c,50), ema(c,200)
    r, a = rsi(c), atr(h,l,c)

    trend = (0.6 if price>e200 else -0.6) + (0.4 if e20>e50 else -0.4)
    if r >= 72:   mom = -0.8
    elif r <= 28: mom =  0.8
    else:         mom = (r-50)/35.0

    yc = macro.get("yield_chg")
    ys = -math.tanh(yc*6) if yc is not None else 0.0

    dp = macro.get("dxy_pct")
    ds = -math.tanh(dp*0.8) if dp is not None else 0.0

    geo = sentiment["geopolitical"] if sentiment else 0.0
    fed = sentiment["fed"]          if sentiment else 0.0

    etf = math.tanh(gld*0.3) if gld is not None else 0.0

    total = (0.35*trend + 0.15*mom + 0.12*ys + 0.08*ds
             + 0.15*geo + 0.10*fed + 0.05*etf)

    sw_lo = min(l[-12:]); sw_hi = max(h[-12:])
    return dict(price=price, e20=e20, e50=e50, e200=e200, rsi=r, atr=a,
                trend=trend, mom=mom, ys=ys, ds=ds, geo=geo, fed=fed, etf=etf,
                total=total, sw_lo=sw_lo, sw_hi=sw_hi, ts=rows[-1]["t"])


def send(text):
    for chunk in [text[i:i+3900] for i in range(0, len(text), 3900)]:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data={"chat_id": TG_CHAT, "text": chunk,
                                "parse_mode": "HTML",
                                "disable_web_page_preview": "true"}, timeout=25)
        print("sent ok" if r.ok else f"TG ERR {r.status_code}")


def build_signal(c, evs, sentiment):
    price, a = c["price"], c["atr"]
    if c["total"] > 0:
        side, dot = "BUY", "🟢"
        sl = min(c["sw_lo"], price - 1.2*a)
        risk = price - sl
        tps = [price + risk*k for k in (1,2,3)]
        inv = f"{sl:.2f} close neeche → cancel"
    else:
        side, dot = "SELL", "🔴"
        sl = max(c["sw_hi"], price + 1.2*a)
        risk = sl - price
        tps = [price - risk*k for k in (1,2,3)]
        inv = f"{sl:.2f} close upar → cancel"

    candle = datetime.datetime.fromtimestamp(c["ts"], UTC).strftime("%d %b %H:%M")
    lines = [
        f"{dot} <b>XAUUSD — {side}</b>",
        f"<i>H1 · candle {candle} UTC</i>", "",
        f"<b>Entry</b>  {price:.2f}",
        f"<b>SL</b>     {sl:.2f}   ({risk:.2f})",
        f"<b>TP1</b>    {tps[0]:.2f}   (1R)",
        f"<b>TP2</b>    {tps[1]:.2f}   (2R)",
        f"<b>TP3</b>    {tps[2]:.2f}   (3R)", "",
        f"<b>Score</b> {c['total']:+.2f}",
        "", "<b>Wajah</b>",
    ]
    lines.append("• " + ("Trend bullish (EMA stack)" if c["trend"]>0 else "Trend bearish (EMA stack)"))
    lines.append(f"• RSI {c['rsi']:.0f} — "
                 + ("overbought" if c["rsi"]>72 else "oversold" if c["rsi"]<28 else "neutral"))
    if c["ys"]:  lines.append(f"• Real yield → " + ("bullish" if c["ys"]>0 else "bearish"))
    if c["ds"]:  lines.append(f"• Live DXY → "  + ("bullish" if c["ds"]>0 else "bearish"))
    if c["etf"]: lines.append(f"• GLD flow → "   + ("inflow"  if c["etf"]>0 else "outflow"))
    if sentiment:
        if c["geo"]:
            lines.append(f"• Geo {c['geo']:+.2f} — {sentiment['reason_geo']}")
        if c["fed"]:
            lines.append(f"• Fed {c['fed']:+.2f} — {sentiment['reason_fed']}")

    loss_1 = risk * CONTRACT_SIZE
    lines += ["", "<b>Risk</b>",
              f"1.00 lot → ${loss_1:.0f}",
              f"0.10 lot → ${loss_1*0.1:.1f}",
              f"0.01 lot → ${loss_1*0.01:.2f}"]
    if EQUITY_USD > 0:
        lots = (EQUITY_USD*RISK_PCT)/(risk*CONTRACT_SIZE)
        lines.append(f"1% risk ({EQUITY_USD:.0f}$) ≈ {lots:.2f} lot")
    lines += ["", f"<b>Invalidation:</b> {inv}"]

    near = [e for e in evs if e[2] <= 12]
    if near:
        lines += ["", "⚠️ <b>HIGH IMPACT jald</b>"]
        for dt, t, dh in near[:3]:
            lines.append(f"• {dt.strftime('%H:%M')} UTC — {t} ({dh:.0f}h)")
    return "\n".join(lines)


def build_daily(c, evs):
    bias = ("BULLISH" if c["total"]>=ENTRY_T else
            "BEARISH" if c["total"]<=-ENTRY_T else "NEUTRAL")
    lines = ["📊 <b>XAUUSD — Daily Bias</b>",
             f"<i>{datetime.datetime.now(UTC).strftime('%d %b %Y')}</i>", "",
             f"<b>Bias</b>  {bias}",
             f"<b>Score</b> {c['total']:+.2f}",
             f"<b>Price</b> {c['price']:.2f}",
             f"<b>ATR</b>   {c['atr']:.2f}",
             f"<b>RSI</b>   {c['rsi']:.0f}",
             "", "<b>Components</b>",
             f"• Trend  {c['trend']:+.2f}",
             f"• Mom    {c['mom']:+.2f}",
             f"• Yield  {c['ys']:+.2f}",
             f"• DXY    {c['ds']:+.2f}",
             f"• Geo    {c['geo']:+.2f}",
             f"• Fed    {c['fed']:+.2f}",
             f"• ETF    {c['etf']:+.2f}",
             "", "<b>Aaj ke events</b>"]
    if evs:
        for dt, t, dh in evs[:6]:
            lines.append(f"• {dt.strftime('%H:%M')} UTC — {t}")
    else:
        lines.append("• Koi high-impact USD event nahi")
    lines += ["", "<i>Analysis, guarantee nahi. SL lazmi.</i>"]
    return "\n".join(lines)


def main():
    if is_weekend():
        print("WEEKEND GUARD — market closed, skipping")
        return

    rows = fetch_prices()
    dxy  = live_dxy()
    yld  = fred_yield_chg()
    gld  = gld_flow()
    news = google_news()
    fed  = fed_news()

    sentiment = llm_sentiment(news, fed)

    macro = {"yield_chg": yld, "dxy_pct": dxy}
    cur  = score(rows, macro, sentiment, gld)
    prev = score(rows[:-1], macro, sentiment, gld)

    d_now = 1 if cur["total"]>=ENTRY_T else (-1 if cur["total"]<=-ENTRY_T else 0)
    d_old = 1 if prev["total"]>=ENTRY_T else (-1 if prev["total"]<=-ENTRY_T else 0)

    flip     = d_now != 0 and d_now != d_old
    strong   = abs(cur["total"]) >= STRONG_T
    near_ema = abs(cur["price"] - cur["e20"]) <= 0.35*cur["atr"]

    evs = events_next()
    now = datetime.datetime.now(UTC)

    print(f"score={cur['total']:+.2f} prev={prev['total']:+.2f} "
          f"flip={flip} strong={strong} near={near_ema}")
    print(f"news={len(news)} fed={len(fed)} llm={'yes' if sentiment else 'no'}")

    if flip or (strong and near_ema):
        send(build_signal(cur, evs, sentiment))
    elif now.hour == 7:
        send(build_daily(cur, evs))
    else:
        print("no signal — staying quiet")


if __name__ == "__main__":
    main()
