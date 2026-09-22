"""
core.py - Indian Stock & Mutual Fund Screener: data / business logic only.

This is the original market_screener.py with all Tkinter/GUI code removed.
Every function here is pure Python (fetch data, compute returns/risk, build
the interactive chart HTML) and is imported by app.py (the Flask web app).

Data sources: NSE index lists, Yahoo Finance (yfinance), AMFI NAV via
mfapi.in, Google News RSS.

DISCLAIMER: Educational tool only. Not investment advice. Past performance
does not guarantee future returns. Always do your own research.
"""

import argparse
import html
import io
import json
import os
import re
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone, time as dtime
from pathlib import Path
from urllib.parse import quote_plus

import numpy as np
import pandas as pd
import requests
import yfinance as yf

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

INDEX_URLS = {
    "nifty50": "https://archives.nseindia.com/content/indices/ind_nifty50list.csv",
    "midcap": "https://archives.nseindia.com/content/indices/ind_niftymidcap150list.csv",
    "smallcap": "https://archives.nseindia.com/content/indices/ind_niftysmallcap250list.csv",
}

# Small backup lists, used only if the NSE download fails.
FALLBACK = {
    "nifty50": [
        "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "ITC", "LT", "SBIN",
        "BHARTIARTL", "AXISBANK", "KOTAKBANK", "HINDUNILVR", "MARUTI",
        "SUNPHARMA", "TITAN", "NTPC", "ONGC", "TATASTEEL", "WIPRO", "HCLTECH",
    ],
    "midcap": [
        "PERSISTENT", "MPHASIS", "VOLTAS", "POLYCAB", "TVSMOTOR", "BHARATFORG",
        "INDHOTEL", "ASHOKLEY", "MUTHOOTFIN", "LUPIN", "FEDERALBNK", "COFORGE",
    ],
    "smallcap": [
        "CDSL", "ANGELONE", "CASTROLIND", "KPITTECH", "REDINGTON", "RBLBANK",
        "BSE", "CESC", "NATIONALUM", "IEX",
    ],
}

# Look-back windows (calendar based)
PERIODS = {
    "1m": pd.DateOffset(months=1),
    "2m": pd.DateOffset(months=2),
    "6m": pd.DateOffset(months=6),
    "1y": pd.DateOffset(years=1),
    "3y": pd.DateOffset(years=3),
    "5y": pd.DateOffset(years=5),
}

NEGATIVE_WORDS = [
    "fraud", "probe", "penalty", "penalised", "fine", "fined", "downgrade",
    "downgrades", "downgraded", "loss", "losses", "default", "lawsuit", "sebi",
    "ban", "banned", "raid", "raids", "resigns", "resignation", "slump",
    "slumps", "plunge", "plunges", "crash", "crashes", "falls", "fall", "drops",
    "tumbles", "slides", "sinks", "decline", "declines", "weak", "misses",
    "miss", "cut", "cuts", "warning", "warns", "concern", "concerns",
    "investigation", "scam", "debt", "bankruptcy", "insolvency", "sell",
    "underperform", "pledge", "shortfall", "recall", "shutdown", "strike",
    "no dividend", "skips dividend", "dividend cut",
]
POSITIVE_WORDS = [
    "profit", "profits", "surge", "surges", "jumps", "rallies", "rally",
    "gains", "gain", "upgrade", "upgrades", "upgraded", "record", "beats",
    "wins", "order win", "bags", "buy", "outperform", "growth", "dividend",
    "bonus", "expansion", "strong", "soars", "rises", "climbs", "target",
]
NEG_RE = re.compile(r"\b(" + "|".join(map(re.escape, NEGATIVE_WORDS)) + r")\b", re.I)
POS_RE = re.compile(r"\b(" + "|".join(map(re.escape, POSITIVE_WORDS)) + r")\b", re.I)

WATCH_FILE = os.path.join(os.path.expanduser("~"), ".market_screener_watchlist.json")
DEFAULT_WATCH = [
    {"ticker": "^NSEI", "name": "NIFTY 50"}, {"ticker": "^NSEBANK", "name": "NIFTY BANK"},
    {"ticker": "^BSESN", "name": "SENSEX"}, {"ticker": "RELIANCE.NS", "name": "RELIANCE"},
    {"ticker": "TCS.NS", "name": "TCS"}, {"ticker": "HDFCBANK.NS", "name": "HDFCBANK"},
    {"ticker": "INFY.NS", "name": "INFY"}, {"ticker": "ICICIBANK.NS", "name": "ICICIBANK"},
    {"ticker": "SBIN.NS", "name": "SBIN"},
]


class ScreenerError(Exception):
    """Friendly error shown to the user (no traceback)."""


# Progress messages go here. The GUI replaces this to show them on screen.
LOGGER = print


def log(msg: str):
    LOGGER(msg)


# --------------------------------------------------------------------------
# MARKET CLOCK (NSE: Mon-Fri 09:15-15:30 IST)
# --------------------------------------------------------------------------
IST = timezone(timedelta(hours=5, minutes=30))


def ist_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(IST)


def is_market_open(now: datetime = None) -> bool:
    """True during NSE trading hours (exchange holidays are not detected)."""
    now = now or ist_now()
    if now.weekday() >= 5:
        return False
    return dtime(9, 15) <= now.time() <= dtime(15, 30)


# --------------------------------------------------------------------------
# HELPERS: RETURNS & RISK
# --------------------------------------------------------------------------
def pct_return(series: pd.Series, offset: pd.DateOffset) -> float:
    """% change between (last date - offset) and last date."""
    s = series.dropna()
    if s.empty:
        return np.nan
    end = s.index[-1]
    start = end - offset
    if s.index[0] > start + pd.Timedelta(days=7):  # not enough history
        return np.nan
    past = s.loc[:start]
    base = past.iloc[-1] if not past.empty else s.iloc[0]
    return (s.iloc[-1] / base - 1) * 100


def risk_metrics(series: pd.Series) -> dict:
    """1-year annualised volatility and max drawdown (in %)."""
    s = series.dropna()
    if len(s) < 30:
        return {"Volatility_1y_%": np.nan, "MaxDrawdown_1y_%": np.nan}
    last_year = s[s.index >= s.index[-1] - pd.DateOffset(years=1)]
    vol = last_year.pct_change().std() * np.sqrt(252) * 100
    dd = (last_year / last_year.cummax() - 1).min() * 100
    return {"Volatility_1y_%": round(vol, 1), "MaxDrawdown_1y_%": round(dd, 1)}


def cagr(total_return_pct: float, years: int) -> float:
    if pd.isna(total_return_pct):
        return np.nan
    return round(((1 + total_return_pct / 100) ** (1 / years) - 1) * 100, 2)


def returns_row(s: pd.Series) -> dict:
    row = {f"Ret_{label}_%": round(pct_return(s, off), 2) for label, off in PERIODS.items()}
    row["CAGR_3y_%"] = cagr(row["Ret_3y_%"], 3)
    row["CAGR_5y_%"] = cagr(row["Ret_5y_%"], 5)
    return row


def range_stats(s: pd.Series) -> dict:
    """52-week high/low, distance from high and last-day change."""
    y = s[s.index >= s.index[-1] - pd.DateOffset(years=1)]
    hi, lo, last = float(y.max()), float(y.min()), float(s.iloc[-1])
    day = round((last / float(s.iloc[-2]) - 1) * 100, 2) if len(s) > 1 else np.nan
    return {"High_52w": round(hi, 2), "Low_52w": round(lo, 2),
            "From_High_%": round((last / hi - 1) * 100, 2), "Day_%": day}


def compute_buckets(df: pd.DataFrame, col: str, name_col: str) -> dict:
    """Names of stocks/funds that grew >= 10%, 20%, 50% in the chosen period."""
    return {th: df.loc[df[col] >= th, name_col].tolist() for th in (10, 20, 50)}


# --------------------------------------------------------------------------
# STOCK UNIVERSE & PRICES
# --------------------------------------------------------------------------
def load_index(name: str) -> pd.DataFrame:
    label = {"nifty50": "Nifty 50", "midcap": "Midcap 150", "smallcap": "Smallcap 250"}[name]
    try:
        r = requests.get(INDEX_URLS[name], headers=HEADERS, timeout=30)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text)).rename(columns={"Company Name": "Company"})
        df = df[["Symbol", "Company", "Industry"]].copy()
    except Exception as e:  # noqa: BLE001
        log(f"[warn] Could not download {label} list from NSE ({e}). Using small backup list.")
        df = pd.DataFrame({"Symbol": FALLBACK[name]})
        df["Company"] = df["Symbol"]
        df["Industry"] = ""
    df["Category"] = label
    return df


def get_universe(name: str) -> pd.DataFrame:
    names = ["nifty50", "midcap", "smallcap"] if name == "all" else [name]
    uni = pd.concat([load_index(n) for n in names], ignore_index=True)
    return uni.drop_duplicates(subset="Symbol").reset_index(drop=True)


_UNIVERSE_CACHE = None


def cached_universe() -> pd.DataFrame:
    """All Nifty 50 + Midcap + Smallcap stocks (downloaded once, then cached)."""
    global _UNIVERSE_CACHE
    if _UNIVERSE_CACHE is None:
        _UNIVERSE_CACHE = get_universe("all")
    return _UNIVERSE_CACHE


def download_prices(symbols, years: int = 5) -> pd.DataFrame:
    """Adjusted close prices (Yahoo Finance uses the .NS suffix for NSE)."""
    tickers = [f"{s}.NS" for s in symbols]
    frames = []
    for i in range(0, len(tickers), 100):
        chunk = tickers[i:i + 100]
        log(f"Downloading prices {i + 1}-{i + len(chunk)} of {len(tickers)} ...")
        try:
            raw = yf.download(chunk, period=f"{years}y", interval="1d",
                              auto_adjust=True, progress=False, threads=True)
        except Exception as e:  # noqa: BLE001
            log(f"[warn] price download failed for a batch: {e}")
            continue
        if raw is None or raw.empty:
            continue
        close = raw["Close"]
        if isinstance(close, pd.Series):
            close = close.to_frame(chunk[0])
        frames.append(close)
    if not frames:
        raise ScreenerError("No price data downloaded. Please check your internet connection.")
    prices = pd.concat(frames, axis=1).dropna(axis=1, how="all")
    prices.index = pd.to_datetime(prices.index).tz_localize(None)
    return prices.sort_index()


def build_stock_table(prices: pd.DataFrame, uni: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for tick in prices.columns:
        s = prices[tick].dropna()
        if len(s) < 30:
            continue
        row = {"Symbol": tick.replace(".NS", ""), "Price": round(float(s.iloc[-1]), 2)}
        for label, off in PERIODS.items():
            row[f"Ret_{label}_%"] = round(pct_return(s, off), 2)
        row.update(risk_metrics(s))
        rows.append(row)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.merge(uni[["Symbol", "Company", "Industry", "Category"]], on="Symbol", how="left")


def _close_series(raw) -> pd.Series:
    """Single tz-naive 'Close' Series from a yfinance download result."""
    close = raw["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    close = close.dropna()
    close.index = pd.to_datetime(close.index).tz_localize(None)
    return close


# --------------------------------------------------------------------------
# LIVE PRICES + INTRADAY
# --------------------------------------------------------------------------
def fetch_intraday(ticker: str, span: str = "1D"):
    """Intraday prices. '1D' = last trading day (1-minute), '5D' = last 5 days (15-minute)."""
    interval = "1m" if span == "1D" else "15m"
    raw = yf.download(ticker, period="5d", interval=interval, auto_adjust=True, progress=False)
    if raw is None or raw.empty:
        return None
    close = _close_series(raw)
    if span == "1D" and len(close):
        close = close[close.index.date == close.index[-1].date()]
    return close if len(close) > 1 else None


def _fast(fi, key):
    try:
        v = float(getattr(fi, key))
    except Exception:  # noqa: BLE001
        return None
    return None if np.isnan(v) else v


def fetch_live_quote(ticker: str) -> dict:
    """Latest (near real-time) price of a stock / index."""
    price = prev = hi = lo = vol = None
    try:
        fi = yf.Ticker(ticker).fast_info
        price, prev, hi, lo, vol = (_fast(fi, k) for k in
                                    ("last_price", "previous_close", "day_high", "day_low", "last_volume"))
    except Exception:  # noqa: BLE001
        pass
    if price is None:  # fall back to the latest 1-minute candle
        ser = fetch_intraday(ticker, "1D")
        if ser is not None:
            price, hi, lo = float(ser.iloc[-1]), float(ser.max()), float(ser.min())
    if price is None:
        raise ScreenerError(f"No live price found for {ticker}.")
    if prev is None:
        try:
            d = _close_series(yf.download(ticker, period="5d", interval="1d",
                                          auto_adjust=True, progress=False))
            prev = float(d.iloc[-2]) if len(d) > 1 else price
        except Exception:  # noqa: BLE001
            prev = price
    change = price - prev
    return {"ticker": ticker, "price": price, "prev_close": prev, "change": change,
            "pct": (change / prev * 100) if prev else 0.0, "high": hi, "low": lo, "volume": vol,
            "time": datetime.now().strftime("%H:%M:%S"), "open": is_market_open()}


def fetch_many_quotes(tickers) -> dict:
    """Live quotes for many tickers in parallel -> {ticker: quote or {'error': msg}}."""
    def one(t):
        try:
            return t, fetch_live_quote(t)
        except Exception as e:  # noqa: BLE001
            return t, {"error": str(e)}
    with ThreadPoolExecutor(max_workers=6) as ex:
        return dict(ex.map(one, tickers))


def load_watchlist() -> list:
    try:
        with open(WATCH_FILE, encoding="utf-8") as f:
            items = json.load(f)
        if isinstance(items, list) and items:
            return [i for i in items if isinstance(i, dict) and "ticker" in i]
    except Exception:  # noqa: BLE001
        pass
    return [dict(i) for i in DEFAULT_WATCH]


def save_watchlist(items: list):
    try:
        with open(WATCH_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=1)
    except Exception:  # noqa: BLE001
        pass


def fmt_volume(v) -> str:
    if v is None or not _isnum(v) or float(v) <= 0:
        return "-"
    v = float(v)
    return f"{v / 1e7:.2f} Cr" if v >= 1e7 else f"{v / 1e5:.2f} L" if v >= 1e5 else f"{v:,.0f}"


# --------------------------------------------------------------------------
# DIVIDENDS
# --------------------------------------------------------------------------
def get_dividend_series(ticker: str):
    """Dividend history (Series) - None if it could not be fetched."""
    try:
        div = yf.Ticker(ticker).dividends
    except Exception:  # noqa: BLE001
        return None
    if div is None:
        return None
    if not div.empty:
        div = div.copy()
        div.index = pd.to_datetime(div.index).tz_localize(None)
    return div


def dividend_summary(div, price: float) -> dict:
    """Dividend yield + whether dividends are growing, stable, cut or stopped."""
    empty = {"Div_12M_Rs": 0.0, "Div_Yield_%": 0.0, "Div_Trend": "No dividend", "Last_Div_Date": ""}
    if div is None:
        return {**empty, "Div_Trend": "Unknown"}
    if div.empty:
        return empty

    now = pd.Timestamp.today().normalize()
    last12 = float(div[div.index > now - pd.DateOffset(years=1)].sum())
    prev12 = float(div[(div.index > now - pd.DateOffset(years=2))
                       & (div.index <= now - pd.DateOffset(years=1))].sum())

    if last12 == 0 and prev12 > 0:
        trend = "STOPPED (negative)"
    elif last12 == 0:
        trend = "No recent dividend"
    elif prev12 == 0:
        trend = "New payer"
    elif last12 < prev12 * 0.9:
        trend = "CUT (negative)"
    elif last12 > prev12 * 1.1:
        trend = "Growing"
    else:
        trend = "Stable"

    return {
        "Div_12M_Rs": round(last12, 2),
        "Div_Yield_%": round(last12 / price * 100, 2) if price else 0.0,
        "Div_Trend": trend,
        "Last_Div_Date": div.index[-1].strftime("%Y-%m-%d"),
    }


def dividend_info(symbol: str, price: float) -> dict:
    return dividend_summary(get_dividend_series(f"{symbol}.NS"), price)


# --------------------------------------------------------------------------
# NEWS (Google News RSS - no API key needed)
# --------------------------------------------------------------------------
def news_items(name: str, topic: str = "share", max_items: int = 15) -> list:
    """Headlines of the last 30 days: [{'title','date','link','tone'}]. tone = neg/pos/neu."""
    clean = re.sub(r"\b(ltd|limited|inc|corp|corporation)\b\.?", "", name, flags=re.I).strip()
    q = quote_plus(f"{clean} {topic} when:30d")
    url = f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        out = []
        for it in root.findall("./channel/item")[:max_items]:
            title = it.findtext("title", "")
            n, p = len(NEG_RE.findall(title)), len(POS_RE.findall(title))
            out.append({"title": title, "date": it.findtext("pubDate", ""),
                        "link": it.findtext("link", ""),
                        "tone": "neg" if n > p else "pos" if p > n else "neu"})
        return out
    except Exception:  # noqa: BLE001
        return []


def sentiment_from_items(items: list) -> dict:
    neg = [i["title"] for i in items if i["tone"] == "neg"]
    pos = [i for i in items if i["tone"] == "pos"]
    if not items:
        flag = "No news found"
    elif len(neg) >= 3 and len(neg) > len(pos):
        flag = "NEGATIVE"
    elif len(neg) > len(pos):
        flag = "Mildly negative"
    elif len(pos) > len(neg):
        flag = "Positive"
    else:
        flag = "Neutral"
    return {"News_Count": len(items), "News_Neg": len(neg), "News_Pos": len(pos),
            "News_Flag": flag, "Negative_Headlines": " || ".join(neg[:3])}


def news_sentiment(name: str, topic: str = "share") -> dict:
    return sentiment_from_items(news_items(name, topic))


def fund_search_name(name: str) -> str:
    """'Nippon India Small Cap Fund - Direct Plan - Growth' -> 'Nippon India Small Cap Fund'."""
    clean = re.sub(r"\b(direct|regular|plan|growth|option|idcw|dividend|payout|reinvestment)\b",
                   " ", name, flags=re.I)
    return re.sub(r"[-\u2013()]+", " ", clean).split("  ")[0].strip() or name


# --------------------------------------------------------------------------
# RED FLAGS & SIGNAL
# --------------------------------------------------------------------------
def red_flags(row) -> str:
    flags = []
    if "negative" in str(row.get("Div_Trend", "")).lower():
        flags.append("DIVIDEND")
    if row.get("News_Flag") in ("NEGATIVE", "Mildly negative"):
        flags.append("NEWS")
    dd, vol = row.get("MaxDrawdown_1y_%"), row.get("Volatility_1y_%")
    if dd is not None and pd.notna(dd) and dd <= -35:
        flags.append("BIG-DRAWDOWN")
    if vol is not None and pd.notna(vol) and vol >= 55:
        flags.append("HIGH-VOLATILITY")
    return ", ".join(flags) if flags else "-"


def signal_score(row) -> int:
    """+1 per positive period (1M,2M,6M,1Y), +1 if 6M >= 20%, -1 per red flag."""
    score = 0
    for p in ("1m", "2m", "6m", "1y"):
        v = row.get(f"Ret_{p}_%")
        if v is not None and pd.notna(v):
            score += 1 if v > 0 else -1
    v6 = row.get("Ret_6m_%")
    if v6 is not None and pd.notna(v6) and v6 >= 20:
        score += 1
    rf = str(row.get("Red_Flags", "-"))
    if rf not in ("-", "", "nan"):
        score -= len(rf.split(", "))
    return score


def score_label(score: int) -> str:
    return "STRONG" if score >= 4 else "GOOD" if score >= 2 else "WATCH" if score >= 0 else "AVOID"


def add_signal(df: pd.DataFrame) -> pd.DataFrame:
    df["Red_Flags"] = df.apply(red_flags, axis=1)
    df["Score"] = df.apply(signal_score, axis=1)
    df["Signal"] = df["Score"].map(score_label)
    return df


# --------------------------------------------------------------------------
# COLOUR HELPERS (pure python - used by the GUI heat-map)
# --------------------------------------------------------------------------
def _rgb(c: str):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def mix(c1: str, c2: str, t: float) -> str:
    a, b = _rgb(c1), _rgb(c2)
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


NEUTRAL = ("#eceff1", "#607d8b")
RET_SCALE = {"1m": 15, "2m": 20, "6m": 40, "1y": 60, "3y": 120, "5y": 200}
GREEN, AMBER, RED = ("#c8efd4", "#14532d"), ("#ffe8b3", "#7a4b00"), ("#f8c9c9", "#8b1a1a")

BADGES = {
    "Signal": {"STRONG": ("#1b7f3b", "#ffffff"), "GOOD": ("#5cb85c", "#ffffff"),
               "WATCH": ("#f0ad4e", "#222222"), "AVOID": ("#d9534f", "#ffffff")},
    "Category": {"Nifty 50": ("#d6e6ff", "#0b3d91"), "Midcap 150": ("#ead9ff", "#5b2a86"),
                 "Smallcap 250": ("#ffe5cc", "#8a4b08")},
    "News_Flag": {"Positive": GREEN, "Neutral": NEUTRAL, "Mildly negative": AMBER,
                  "NEGATIVE": RED, "No news found": NEUTRAL},
    "Div_Trend": {"Growing": GREEN, "New payer": ("#d1f2eb", "#0e6251"),
                  "Stable": ("#d9ecff", "#0b3d91"), "No dividend": NEUTRAL,
                  "No recent dividend": NEUTRAL, "Unknown": NEUTRAL,
                  "CUT (negative)": RED, "STOPPED (negative)": RED},
}


def _isnum(v) -> bool:
    try:
        return v is not None and not pd.isna(float(v))
    except (TypeError, ValueError):
        return False


def return_style(v, scale: float):
    """Green for gains, red for losses - colour gets deeper with size."""
    if not _isnum(v):
        return NEUTRAL
    v = float(v)
    t = min(abs(v) / scale, 1.0)
    bg = mix("#e6f6ea", "#1f9d55", t) if v >= 0 else mix("#fdeaea", "#d93b3b", t)
    return bg, ("#ffffff" if t > 0.6 else "#1b1b1b")


def cell_style(col: str, v):
    """-> (text, background, foreground) for one table cell."""
    plain = ("#ffffff", "#1b1b1b")
    if col.startswith("Ret_") or col.startswith("CAGR_"):
        scale = RET_SCALE.get(col.split("_")[1], 30) if col.startswith("Ret_") else 30
        bg, fg = return_style(v, scale)
        txt = f"{'▲' if float(v) >= 0 else '▼'} {float(v):.2f}" if _isnum(v) else "-"
        return txt, bg, fg
    if col in ("Volatility_1y_%", "MaxDrawdown_1y_%"):
        if not _isnum(v):
            return ("-",) + NEUTRAL
        v = float(v)
        if col == "Volatility_1y_%":
            bg, fg = GREEN if v < 25 else AMBER if v < 40 else RED
        else:
            bg, fg = GREEN if v > -15 else AMBER if v > -30 else RED
        return f"{v:.1f}%", bg, fg
    if col == "Div_Yield_%":
        if not _isnum(v):
            return ("-",) + NEUTRAL
        v = float(v)
        bg, fg = (("#8fd6a7", "#0b3d1e") if v >= 2 else GREEN if v >= 0.5
                  else ("#e6f6ea", "#2e7d32") if v > 0 else NEUTRAL)
        return f"{v:.2f}%", bg, fg
    if col == "Red_Flags":
        if str(v) in ("-", "", "nan", "None"):
            return ("✔ Clear", "#e6f6ea", "#1b7f3b")
        return ("⚠ " + str(v), "#f8c9c9", "#8b1a1a")
    if col in BADGES:
        bg, fg = BADGES[col].get(str(v), NEUTRAL)
        return str(v), bg, fg
    if col in ("Price", "NAV") and _isnum(v):
        return f"{float(v):,.2f}", plain[0], plain[1]
    return ("" if v is None else str(v)), plain[0], plain[1]


PRETTY = {
    "Ret_1m_%": "1M %", "Ret_2m_%": "2M %", "Ret_6m_%": "6M %", "Ret_1y_%": "1Y %",
    "Ret_3y_%": "3Y %", "Ret_5y_%": "5Y %", "CAGR_3y_%": "3Y CAGR %", "CAGR_5y_%": "5Y CAGR %",
    "Volatility_1y_%": "Volatility 1Y", "MaxDrawdown_1y_%": "Max Drop 1Y",
    "Div_Yield_%": "Div Yield", "Div_Trend": "Dividend Trend", "News_Flag": "News Mood",
    "Red_Flags": "Red Flags", "Price": "Price (Rs)", "NAV": "NAV (Rs)",
}
COLW = {"Symbol": 13, "Company": 27, "Category": 13, "Signal": 9, "Price": 11, "Fund": 55,
        "NAV": 10, "Div_Trend": 19, "News_Flag": 15, "Red_Flags": 32, "Div_Yield_%": 10,
        "Volatility_1y_%": 12, "MaxDrawdown_1y_%": 12, "CAGR_3y_%": 10, "CAGR_5y_%": 10}
LEFT_COLS = {"Symbol", "Company", "Fund"}


def pretty(col: str) -> str:
    return PRETTY.get(col, col.replace("_", " "))


# --------------------------------------------------------------------------
# CORE: STOCK SCREEN  (used by both GUI and command line)
# --------------------------------------------------------------------------
def screen_stocks(universe="all", period="6m", min_growth=None, top=10,
                  consistent=False, news=True, dividends=True):
    col = f"Ret_{period}_%"
    log(f"Universe: {universe} | ranking by {period.upper()} return")
    uni = get_universe(universe)
    log(f"Universe size: {len(uni)} stocks")
    prices = download_prices(uni["Symbol"].tolist(), years=5)
    df = build_stock_table(prices, uni)
    if df.empty:
        raise ScreenerError("No price data could be analysed.")

    df = df.dropna(subset=[col])
    buckets = compute_buckets(df, col, "Symbol")

    if consistent:
        df = df[(df["Ret_1m_%"] > 0) & (df["Ret_2m_%"] > 0) & (df["Ret_6m_%"] > 0)]
        log(f"After 'consistent' filter (positive in 1M, 2M, 6M): {len(df)} stocks")
    if min_growth is not None:
        df = df[df[col] >= min_growth]
        log(f"After min growth >= {min_growth}% filter: {len(df)} stocks")

    df = df.sort_values(col, ascending=False).head(top).reset_index(drop=True)
    if df.empty:
        raise ScreenerError("No stock passed your filters. Try a lower minimum growth.")

    if dividends:
        log("Fetching dividend history ...")
        divs = [dividend_info(r.Symbol, r.Price) for r in df.itertuples()]
        df = pd.concat([df, pd.DataFrame(divs)], axis=1)

    if news:
        log("Fetching latest news ...")
        rows = []
        for i, r in enumerate(df.itertuples(), 1):
            log(f"News {i}/{len(df)}: {r.Symbol}")
            rows.append(news_sentiment(r.Company if isinstance(r.Company, str) else r.Symbol))
            time.sleep(0.5)  # be polite to the server
        df = pd.concat([df, pd.DataFrame(rows)], axis=1)

    return add_signal(df), buckets


# --------------------------------------------------------------------------
# CORE: MUTUAL FUND SCREEN  (data: https://www.mfapi.in - free AMFI NAV API)
# --------------------------------------------------------------------------
MF_SEARCH = "https://api.mfapi.in/mf/search"
MF_NAV = "https://api.mfapi.in/mf/{}"


def search_funds(query: str) -> list:
    r = requests.get(MF_SEARCH, params={"q": query}, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_fund(code):
    """NAV history (Series) and meta info (dict) of one scheme."""
    r = requests.get(MF_NAV.format(code), headers=HEADERS, timeout=30)
    r.raise_for_status()
    j = r.json()
    data = pd.DataFrame(j.get("data", []))
    if data.empty:
        return pd.Series(dtype=float), j.get("meta", {}) or {}
    data["date"] = pd.to_datetime(data["date"], format="%d-%m-%Y")
    data["nav"] = data["nav"].astype(float)
    return data.set_index("date")["nav"].sort_index(), j.get("meta", {}) or {}


def fund_nav_series(code) -> pd.Series:
    return fetch_fund(code)[0]


def is_direct_growth(name: str) -> bool:
    n = name.lower()
    return "direct" in n and "growth" in n and not any(
        x in n for x in ("idcw", "dividend", "bonus", "payout", "reinvest"))


def screen_funds(query="flexi cap", period="1y", min_growth=None, top=10,
                 max_funds=40, all_plans=False, news=False):
    """max_funds: how many matching schemes to scan (0 / None = ALL of them)."""
    col = f"Ret_{period}_%"
    log(f"Searching funds for '{query}' ...")
    try:
        funds = search_funds(query)
    except Exception as e:  # noqa: BLE001
        raise ScreenerError(f"Could not reach the mutual fund data service: {e}")
    log(f"Schemes matching search: {len(funds)}")

    if not all_plans:  # keep Direct + Growth plans only
        funds = [f for f in funds if is_direct_growth(f["schemeName"])]
        log(f"After keeping Direct-Growth plans only: {len(funds)}")
    if max_funds:
        funds = funds[:max_funds]
    if not funds:
        raise ScreenerError("No fund found for this search. Try another name.")

    def load(f):
        try:
            return f, fund_nav_series(f["schemeCode"])
        except Exception:  # noqa: BLE001
            return f, None

    rows = []
    log(f"Downloading NAV history of {len(funds)} schemes (8 at a time) ...")
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(load, f) for f in funds]
        for i, fut in enumerate(as_completed(futures), 1):
            f, nav = fut.result()
            if i % 10 == 0 or i == len(funds):
                log(f"Downloaded {i}/{len(funds)} schemes ...")
            if nav is None or len(nav) < 30:
                continue
            row = {"Code": f["schemeCode"], "Fund": f["schemeName"], "NAV": round(float(nav.iloc[-1]), 2)}
            row.update(returns_row(nav))
            row.update(risk_metrics(nav))
            rows.append(row)

    df = pd.DataFrame(rows)
    if df.empty:
        raise ScreenerError("No fund data found. Try another search (e.g. 'flexi cap', 'nifty 50 index').")

    df = df.dropna(subset=[col])
    buckets = compute_buckets(df, col, "Fund")
    if min_growth is not None:
        df = df[df[col] >= min_growth]
    df = df.sort_values(col, ascending=False).head(top).reset_index(drop=True)
    if df.empty:
        raise ScreenerError("No fund passed your filters. Try a lower minimum growth.")

    if news:
        log("Fetching news ...")
        rows = [news_sentiment(fund_search_name(n), "mutual fund") for n in df["Fund"]]
        df = pd.concat([df, pd.DataFrame(rows)], axis=1)
    return add_signal(df), buckets


# --------------------------------------------------------------------------
# CORE: SEARCH ANY EQUITY / MUTUAL FUND  (Search tab) - NO result limit
# --------------------------------------------------------------------------
def _equity_candidates(q: str, yahoo_timeout: int = 15) -> list:
    """All stocks whose name / symbol contains the text -> [{'ticker','name','note'}]."""
    results, pos = [], {}

    def add(ticker, name, note):
        if ticker in pos:  # already listed - just upgrade a bare symbol to the full company name
            cur = results[pos[ticker]]
            if name and cur["name"] == ticker.split(".")[0] and name != cur["name"]:
                cur["name"] = name
            return
        pos[ticker] = len(results)
        results.append({"ticker": ticker, "name": name, "note": note})

    # 1) Nifty 50 / Midcap / Smallcap lists (name or symbol contains the text)
    try:
        uni = cached_universe()
        ql = q.lower()
        hit = uni[uni["Symbol"].str.lower().str.contains(re.escape(ql))
                  | uni["Company"].astype(str).str.lower().str.contains(re.escape(ql))]
        for r in hit.itertuples():
            add(f"{r.Symbol}.NS", str(r.Company), str(r.Category))
    except Exception:  # noqa: BLE001
        pass

    # 2) Yahoo Finance search (finds every listed NSE / BSE stock)
    try:
        r = requests.get("https://query2.finance.yahoo.com/v1/finance/search",
                         params={"q": q, "quotesCount": 50, "newsCount": 0, "listsCount": 0},
                         headers=HEADERS, timeout=yahoo_timeout)
        for it in r.json().get("quotes", []):
            sym = it.get("symbol", "")
            if it.get("quoteType") == "EQUITY" and sym.endswith((".NS", ".BO")):
                add(sym, it.get("longname") or it.get("shortname") or sym,
                    "NSE" if sym.endswith(".NS") else "BSE")
    except Exception:  # noqa: BLE001
        pass

    return results


def search_equities(query: str) -> list:
    """Find stocks by name or symbol -> [{'ticker','name','note'}] (all matches)."""
    q = query.strip()
    if not q:
        raise ScreenerError("Type a company name or symbol to search.")
    results = _equity_candidates(q)
    # Fall back to treating the text as a symbol
    if not results and re.fullmatch(r"[A-Za-z0-9&\-]{1,20}", q):
        results.append({"ticker": q.upper() + ".NS", "name": q.upper(), "note": "direct symbol"})
    if not results:
        raise ScreenerError(f"No equity found for '{q}'. Try the NSE symbol (e.g. TCS).")
    return results


# ---- auto-suggest (names pop up while typing) ----
_SUGGEST_CACHE = {}


def _rank(q: str, name: str, ticker: str = "") -> int:
    """0 = symbol starts with text, 1 = name starts with text, 2 = a word starts with it, 3 = contains."""
    ql, n = q.lower(), name.lower()
    t = ticker.lower().replace(".ns", "").replace(".bo", "")
    if t.startswith(ql):
        return 0
    if n.startswith(ql):
        return 1
    if re.search(r"\b" + re.escape(ql), n):
        return 2
    return 3


def suggest_equities(query: str, limit: int = 10) -> list:
    """Best-matching stock names for the text typed so far (used by the pop-up list)."""
    q = query.strip()
    if len(q) < 2:
        return []
    key = ("eq", q.lower(), limit)
    if key not in _SUGGEST_CACHE:
        cands = _equity_candidates(q, yahoo_timeout=6)
        cands.sort(key=lambda m: (_rank(q, m["name"], m["ticker"]), m["ticker"].endswith(".BO"),
                                  m["name"].lower()))  # best match first, NSE before BSE
        _SUGGEST_CACHE[key] = cands[:limit]
    return _SUGGEST_CACHE[key]


def suggest_funds(query: str, limit: int = 12) -> list:
    """Best-matching mutual fund names for the text typed so far."""
    q = query.strip()
    if len(q) < 3:
        return []
    key = ("mf", q.lower(), limit)
    if key not in _SUGGEST_CACHE:
        try:
            funds = search_funds(q)
        except Exception:  # noqa: BLE001
            return []
        funds.sort(key=lambda f: (_rank(q, f["schemeName"]), not is_direct_growth(f["schemeName"]),
                                  f["schemeName"]))
        _SUGGEST_CACHE[key] = [{"code": f["schemeCode"], "name": f["schemeName"]} for f in funds[:limit]]
    return _SUGGEST_CACHE[key]


def match_label(m: dict) -> str:
    """Text shown for one search match / suggestion."""
    return f"{m['name']}   [{m['ticker']}]   {m['note']}" if "ticker" in m else m["name"]


def search_mutual_funds(query: str) -> list:
    """Find ALL mutual fund schemes matching the text -> [{'code','name'}] (Direct-Growth first)."""
    q = query.strip()
    if not q:
        raise ScreenerError("Type a fund name to search.")
    try:
        funds = search_funds(q)
    except Exception as e:  # noqa: BLE001
        raise ScreenerError(f"Could not reach the mutual fund data service: {e}")
    if not funds:
        raise ScreenerError(f"No mutual fund found for '{q}'.")
    funds = sorted(funds, key=lambda f: (not is_direct_growth(f["schemeName"]), f["schemeName"]))
    return [{"code": f["schemeCode"], "name": f["schemeName"]} for f in funds]


def load_equity_history(ticker: str, name: str = None) -> dict:
    """Full price history (max) of a stock/index for charting."""
    raw = yf.download(ticker, period="max", interval="1d", auto_adjust=True, progress=False)
    if raw is None or raw.empty:
        raise ScreenerError(f"No price data found for {ticker}.")
    close = _close_series(raw)
    if len(close) < 2:
        raise ScreenerError(f"Not enough price history for {ticker}.")
    return {"kind": "equity", "name": name or ticker, "id": ticker, "series": close}


def load_fund_history(code, name: str = None) -> dict:
    nav, meta = fetch_fund(code)
    if len(nav) < 2:
        raise ScreenerError("Not enough NAV history for this scheme.")
    return {"kind": "fund", "name": name or meta.get("scheme_name") or str(code), "id": code, "series": nav}


def analyze_equity(ticker: str, name: str = None) -> dict:
    """Full picture of one stock: returns, risk, dividends, news, chart data."""
    log(f"Downloading full price history for {ticker} ...")
    raw = yf.download(ticker, period="max", interval="1d", auto_adjust=True, progress=False)
    if raw is None or raw.empty:
        raise ScreenerError(f"No price data found for {ticker}.")
    close = _close_series(raw)
    if len(close) < 5:
        raise ScreenerError(f"Not enough price history for {ticker}.")

    row = {"Price": round(float(close.iloc[-1]), 2)}
    row.update(returns_row(close))
    row.update(risk_metrics(close))
    row.update(range_stats(close))

    log("Fetching dividend history ...")
    div = get_dividend_series(ticker)
    row.update(dividend_summary(div, row["Price"]))
    recent = []
    if div is not None and not div.empty:
        recent = [(d.strftime("%Y-%m-%d"), float(a)) for d, a in div.tail(6).items()]

    info = {}
    log("Fetching company details ...")
    try:
        raw_info = yf.Ticker(ticker).info or {}
        name = name or raw_info.get("longName") or ticker
        if raw_info.get("sector"):
            info["Sector"] = raw_info["sector"]
        if raw_info.get("industry"):
            info["Industry"] = raw_info["industry"]
        if raw_info.get("marketCap"):
            info["Market Cap"] = f"Rs {raw_info['marketCap'] / 1e7:,.0f} Cr"
        if raw_info.get("trailingPE"):
            info["P/E Ratio"] = f"{raw_info['trailingPE']:.1f}"
    except Exception:  # noqa: BLE001
        pass
    name = name or ticker

    log("Fetching latest news ...")
    items = news_items(name)
    row.update(sentiment_from_items(items))
    row["Red_Flags"] = red_flags(row)
    row["Score"] = signal_score(row)
    row["Signal"] = score_label(row["Score"])
    return {"kind": "equity", "name": name, "id": ticker, "series": close, "row": row,
            "news": items, "dividends": recent, "info": info}


def analyze_fund(code, name: str = None) -> dict:
    """Full picture of one mutual fund scheme (complete NAV history since launch)."""
    log(f"Downloading full NAV history (scheme {code}) ...")
    try:
        nav, meta = fetch_fund(code)
    except Exception as e:  # noqa: BLE001
        raise ScreenerError(f"Could not download NAV data: {e}")
    if len(nav) < 30:
        raise ScreenerError("Not enough NAV history for this scheme.")
    name = name or meta.get("scheme_name") or str(code)

    row = {"NAV": round(float(nav.iloc[-1]), 2), "NAV_Date": nav.index[-1].strftime("%d %b %Y"),
           "Inception_Date": nav.index[0].strftime("%d %b %Y")}
    row.update(returns_row(nav))
    row.update(risk_metrics(nav))
    row.update(range_stats(nav))
    years = (nav.index[-1] - nav.index[0]).days / 365.25
    total = (float(nav.iloc[-1]) / float(nav.iloc[0]) - 1) * 100
    row["Ret_inception_%"] = round(total, 2)
    row["CAGR_inception_%"] = round(((1 + total / 100) ** (1 / years) - 1) * 100, 2) if years >= 1 else np.nan

    log("Fetching latest news ...")
    items = news_items(fund_search_name(name), "mutual fund")
    row.update(sentiment_from_items(items))
    row["Red_Flags"] = red_flags(row)
    row["Score"] = signal_score(row)
    row["Signal"] = score_label(row["Score"])
    info = {"Fund House": meta.get("fund_house"), "Category": meta.get("scheme_category"),
            "Type": meta.get("scheme_type")}
    info = {k: v for k, v in info.items() if v}
    return {"kind": "fund", "name": name, "id": code, "series": nav, "row": row,
            "news": items, "dividends": None, "info": info}


def build_stats(res: dict) -> list:
    """Coloured stat tiles for the Search tab: [(label, text, bg, fg)]."""
    row, is_eq = res["row"], res["kind"] == "equity"
    tiles = []

    def tile(label, col, key=None):
        t, bg, fg = cell_style(col, row.get(key or col))
        tiles.append((label, t, bg, fg))

    def plain(label, text, bg="#ffffff", fg="#1b1b1b"):
        tiles.append((label, text, bg, fg))

    price = row.get("Price") if is_eq else row.get("NAV")
    plain("Price" if is_eq else "NAV", f"Rs {price:,.2f}" if _isnum(price) else "-")
    plain("52-week High", f"Rs {row['High_52w']:,.2f}")
    plain("52-week Low", f"Rs {row['Low_52w']:,.2f}")
    bg, fg = return_style(row.get("From_High_%"), 30)
    plain("From 52W High", f"{row['From_High_%']:.1f}%" if _isnum(row.get("From_High_%")) else "-", bg, fg)
    tile("Volatility 1Y", "Volatility_1y_%")
    tile("Max Drop 1Y", "MaxDrawdown_1y_%")
    if is_eq:
        tile("Dividend Yield", "Div_Yield_%")
        tile("Dividend Trend", "Div_Trend")
        plain("Last Dividend", row.get("Last_Div_Date") or "-")
    else:
        tile("3Y CAGR", "CAGR_3y_%")
        tile("5Y CAGR", "CAGR_5y_%")
        tile("Since launch CAGR", "CAGR_inception_%")
        rt = row.get("Ret_inception_%")
        rbg, rfg = return_style(rt, 300)
        plain("Total return since launch", f"{rt:+,.1f}%" if _isnum(rt) else "-", rbg, rfg)
        plain("Latest NAV date", row.get("NAV_Date", "-"))
        plain("Launch (first NAV)", row.get("Inception_Date", "-"))
    tile("News Mood", "News_Flag")
    tile("Red Flags", "Red_Flags")
    for k, v in res.get("info", {}).items():
        plain(k, str(v))
    return tiles


# --------------------------------------------------------------------------
# INTERACTIVE HTML CHART (opens in the browser, works offline)
# --------------------------------------------------------------------------
CHART_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__ - Chart</title>
<style>
:root{--bg:#f4f6f9;--card:#ffffff;--text:#1b2a3a;--muted:#607d8b;--grid:#e3e8ee;--up:#1f9d55;--down:#d93b3b;--navy:#1f3a5f}
body.dark{--bg:#0f1720;--card:#16212e;--text:#e6edf3;--muted:#8aa0b4;--grid:#243447;--up:#3ddc84;--down:#ff6b6b;--navy:#9fb7d6}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,Arial,sans-serif}
#top{display:flex;align-items:center;gap:18px;padding:14px 20px;background:var(--navy);color:#fff;flex-wrap:wrap}
body.dark #top{color:#0f1720}
h1{margin:0;font-size:22px}
#sub{opacity:.85;font-size:13px}
#pricebox{margin-left:auto;text-align:right}
#price{font-size:26px;font-weight:700}
#chg{font-size:15px;font-weight:600;margin-left:8px}
button{font:inherit;cursor:pointer}
#theme{border:0;border-radius:6px;padding:6px 12px;background:rgba(255,255,255,.22);color:inherit}
#bar{display:flex;gap:6px;padding:12px 20px 4px;flex-wrap:wrap}
#bar button{border:1px solid var(--grid);background:var(--card);color:var(--text);padding:6px 16px;border-radius:6px;font-weight:600}
#bar button.on{background:var(--navy);color:#fff;border-color:var(--navy)}
body.dark #bar button.on{color:#0f1720}
#bar button:disabled{opacity:.35;cursor:not-allowed}
#stats{padding:6px 20px 8px;font-size:14px;color:var(--muted)}
#stats b{color:var(--text)}
#wrap{margin:0 20px;height:calc(100vh - 235px);min-height:320px;background:var(--card);border:1px solid var(--grid);border-radius:10px}
canvas{width:100%;height:100%;display:block;cursor:crosshair}
#foot{padding:8px 20px;font-size:12px;color:var(--muted)}
</style></head><body>
<div id="top">
  <div><h1 id="title"></h1><div id="sub"></div></div>
  <div id="pricebox"><span id="price"></span><span id="chg"></span><div id="asof" style="font-size:12px;opacity:.85"></div></div>
  <button id="theme">Dark mode</button>
</div>
<div id="bar"></div>
<div id="stats"></div>
<div id="wrap"><canvas id="cv"></canvas></div>
<div id="foot"></div>
<script>
const D = __DATA__;
const $ = id => document.getElementById(id);
const cv = $('cv'), ctx = cv.getContext('2d');
const RANGES = [['1D',null],['5D',null],['1M',{m:1}],['6M',{m:6}],['1Y',{y:1}],['3Y',{y:3}],['5Y',{y:5}],['MAX',{}]];
let cur = D.daily.d.length > 260 ? '1Y' : 'MAX', hoverX = null;
const pad = n => String(n).padStart(2, '0');
const parse = s => new Date(s.length > 10 ? s.replace(' ', 'T') : s + 'T00:00:00');
const fmtP = v => '\u20B9' + v.toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2});
const isIntra = r => r === '1D' || r === '5D';

function getSeries(r) {
  if (isIntra(r)) return D.intra[r] || null;
  const d = D.daily.d, v = D.daily.v, opt = RANGES.find(x => x[0] === r)[1];
  if (!opt || (!opt.m && !opt.y)) return D.daily;
  const cut = parse(d[d.length - 1]);
  if (opt.m) cut.setMonth(cut.getMonth() - opt.m);
  if (opt.y) cut.setFullYear(cut.getFullYear() - opt.y);
  const cs = cut.getFullYear() + '-' + pad(cut.getMonth() + 1) + '-' + pad(cut.getDate());
  let i = 0;
  while (i < d.length - 1 && d[i] < cs) i++;
  return {d: d.slice(i), v: v.slice(i)};
}
function colors() {
  const s = getComputedStyle(document.body), g = k => s.getPropertyValue(k).trim();
  return {up: g('--up'), down: g('--down'), grid: g('--grid'), muted: g('--muted'), text: g('--text'), card: g('--card')};
}
function xlabel(sd) {
  const dt = parse(sd);
  if (cur === '1D') return sd.slice(11, 16);
  if (cur === '5D') return dt.toLocaleDateString('en-GB', {day: '2-digit', month: 'short'}) + ' ' + sd.slice(11, 16);
  if (['1M', '6M', '1Y'].includes(cur)) return dt.toLocaleDateString('en-GB', {day: '2-digit', month: 'short'});
  return dt.toLocaleDateString('en-GB', {month: 'short', year: 'numeric'});
}
function draw() {
  document.querySelectorAll('#bar button').forEach(b => b.classList.toggle('on', b.textContent === cur));
  const s = getSeries(cur);
  const r = cv.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  cv.width = Math.round(r.width * dpr); cv.height = Math.round(r.height * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const W = r.width, H = r.height, C = colors();
  ctx.clearRect(0, 0, W, H);
  if (!s || s.v.length < 2) {
    ctx.fillStyle = C.muted; ctx.font = '15px Segoe UI, sans-serif'; ctx.textAlign = 'center';
    ctx.fillText('No data for this range', W / 2, H / 2); $('stats').textContent = ''; return;
  }
  const v = s.v, n = v.length, pl = 78, pr = 24, pt = 18, pb = 34;
  let lo = Math.min(...v), hi = Math.max(...v);
  const span = (hi - lo) || 1; lo -= span * 0.06; hi += span * 0.06;
  const sp = hi - lo;
  const X = i => pl + (W - pl - pr) * i / (n - 1), Y = p => pt + (H - pt - pb) * (1 - (p - lo) / sp);
  const up = v[n - 1] >= v[0], col = up ? C.up : C.down;
  ctx.font = '11px Segoe UI, sans-serif';
  for (let k = 0; k <= 5; k++) {
    const p = lo + sp * k / 5, y = Y(p);
    ctx.strokeStyle = C.grid; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(pl, y); ctx.lineTo(W - pr, y); ctx.stroke();
    ctx.fillStyle = C.muted; ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
    ctx.fillText(p.toLocaleString('en-IN', {maximumFractionDigits: hi >= 100 ? 0 : 2}), pl - 8, y);
  }
  ctx.textAlign = 'center'; ctx.textBaseline = 'top';
  for (let k = 0; k < 6; k++) {
    const i = Math.round((n - 1) * k / 5);
    ctx.fillText(xlabel(s.d[i]), Math.min(Math.max(X(i), pl + 24), W - pr - 24), H - pb + 9);
  }
  const g = ctx.createLinearGradient(0, pt, 0, H - pb);
  g.addColorStop(0, col + '55'); g.addColorStop(1, col + '05');
  ctx.beginPath(); ctx.moveTo(X(0), H - pb);
  for (let i = 0; i < n; i++) ctx.lineTo(X(i), Y(v[i]));
  ctx.lineTo(X(n - 1), H - pb); ctx.closePath(); ctx.fillStyle = g; ctx.fill();
  ctx.beginPath();
  for (let i = 0; i < n; i++) { if (i) ctx.lineTo(X(i), Y(v[i])); else ctx.moveTo(X(i), Y(v[i])); }
  ctx.strokeStyle = col; ctx.lineWidth = 2; ctx.lineJoin = 'round'; ctx.stroke();
  const vmax = Math.max(...v), vmin = Math.min(...v);
  [[v.indexOf(vmax), 'High', C.up, vmax], [v.indexOf(vmin), 'Low', C.down, vmin]].forEach(([i, t, c, val]) => {
    ctx.fillStyle = c; ctx.beginPath(); ctx.arc(X(i), Y(val), 4, 0, 7); ctx.fill();
    ctx.font = 'bold 11px Segoe UI, sans-serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'alphabetic';
    ctx.fillText(t + ' ' + fmtP(val), Math.min(Math.max(X(i), pl + 60), W - pr - 60), t === 'High' ? Y(val) - 9 : Y(val) + 18);
  });
  if (hoverX !== null) {
    let i = Math.round((hoverX - pl) / (W - pl - pr) * (n - 1)); i = Math.max(0, Math.min(n - 1, i));
    const x = X(i), y = Y(v[i]);
    ctx.setLineDash([5, 4]); ctx.strokeStyle = C.muted; ctx.lineWidth = 1; ctx.beginPath();
    ctx.moveTo(x, pt); ctx.lineTo(x, H - pb); ctx.moveTo(pl, y); ctx.lineTo(W - pr, y); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = col; ctx.beginPath(); ctx.arc(x, y, 5.5, 0, 7); ctx.fill();
    ctx.strokeStyle = C.card; ctx.lineWidth = 2; ctx.stroke();
    const chg = (v[i] / v[0] - 1) * 100, dt = parse(s.d[i]);
    const l1 = isIntra(cur)
      ? dt.toLocaleString('en-GB', {weekday: 'short', day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit'})
      : dt.toLocaleDateString('en-GB', {weekday: 'short', day: '2-digit', month: 'short', year: 'numeric'});
    const l2 = 'Price  ' + fmtP(v[i]), l3 = 'Since start  ' + (chg >= 0 ? '+' : '') + chg.toFixed(2) + '%';
    ctx.font = 'bold 12px Segoe UI, sans-serif';
    const bw = Math.max(ctx.measureText(l1).width, ctx.measureText(l2).width, ctx.measureText(l3).width) + 26, bh = 74;
    let bx = x + 16; if (bx + bw > W - 4) bx = x - 16 - bw;
    const by = Math.max(pt, Math.min(y - bh / 2, H - pb - bh));
    ctx.fillStyle = '#1f3a5f'; ctx.fillRect(bx, by, bw, bh);
    ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
    ctx.fillStyle = '#b8c9de'; ctx.fillText(l1, bx + 13, by + 16);
    ctx.fillStyle = '#ffffff'; ctx.fillText(l2, bx + 13, by + 37);
    ctx.fillStyle = chg >= 0 ? '#7dffa8' : '#ff9a9a'; ctx.fillText(l3, bx + 13, by + 58);
  }
  const chgR = (v[n - 1] / v[0] - 1) * 100;
  $('stats').innerHTML = '<b style="color:' + col + '">' + cur + ': ' + (chgR >= 0 ? '\u25B2 +' : '\u25BC ') + chgR.toFixed(2) + '%</b>' +
    ' &nbsp;|&nbsp; High <b>' + fmtP(vmax) + '</b> &nbsp;|&nbsp; Low <b>' + fmtP(vmin) + '</b> &nbsp;|&nbsp; ' + n.toLocaleString() +
    ' points &nbsp;|&nbsp; ' + s.d[0].slice(0, 16) + ' \u2192 ' + s.d[n - 1].slice(0, 16) +
    ' &nbsp;|&nbsp; <i>move the mouse over the chart for date & price</i>';
}
(function init() {
  $('title').textContent = D.name;
  $('sub').textContent = D.id + '  \u00B7  ' + (D.kind === 'fund' ? 'Mutual fund NAV' : 'Equity');
  const dv = D.daily.v, dd = D.daily.d, last = dv[dv.length - 1], prev = dv.length > 1 ? dv[dv.length - 2] : last;
  $('price').textContent = fmtP(last);
  const ch = last - prev, cp = prev ? ch / prev * 100 : 0;
  $('chg').textContent = (ch >= 0 ? '\u25B2 +' : '\u25BC ') + ch.toFixed(2) + ' (' + cp.toFixed(2) + '%)';
  $('chg').style.color = ch >= 0 ? '#7dffa8' : '#ff9a9a';
  $('asof').textContent = (D.kind === 'fund' ? 'NAV of ' : 'Last close ') + dd[dd.length - 1];
  $('foot').textContent = 'Snapshot generated ' + D.generated + '. Data: Yahoo Finance / AMFI (mfapi.in). Educational use only - not investment advice.';
  RANGES.forEach(([r]) => {
    const b = document.createElement('button'); b.textContent = r;
    if (isIntra(r) && !D.intra[r]) { b.disabled = true; b.title = 'Intraday data not available (market closed, or this is a mutual fund)'; }
    b.onclick = () => { cur = r; draw(); };
    $('bar').appendChild(b);
  });
  if (matchMedia('(prefers-color-scheme: dark)').matches) document.body.classList.add('dark');
  $('theme').onclick = () => { document.body.classList.toggle('dark'); $('theme').textContent = document.body.classList.contains('dark') ? 'Light mode' : 'Dark mode'; draw(); };
  cv.addEventListener('mousemove', e => { hoverX = e.clientX - cv.getBoundingClientRect().left; draw(); });
  cv.addEventListener('mouseleave', () => { hoverX = null; draw(); });
  cv.addEventListener('touchmove', e => { hoverX = e.touches[0].clientX - cv.getBoundingClientRect().left; draw(); }, {passive: true});
  window.addEventListener('resize', draw);
  draw();
})();
</script></body></html>
"""


def build_chart_html(res: dict, intraday: dict = None) -> str:
    """Self-contained interactive chart page (hover shows date + price)."""
    s = res["series"].dropna()
    daily = {"d": [d.strftime("%Y-%m-%d") for d in s.index], "v": [round(float(v), 4) for v in s.values]}
    intra = {}
    for k, ser in (intraday or {}).items():
        if ser is not None and len(ser) > 1:
            intra[k] = {"d": [d.strftime("%Y-%m-%d %H:%M") for d in ser.index],
                        "v": [round(float(v), 4) for v in ser.values]}
    payload = {"name": str(res["name"]), "id": str(res["id"]), "kind": res["kind"], "daily": daily,
               "intra": intra, "generated": datetime.now().strftime("%d %b %Y %H:%M:%S")}
    data = json.dumps(payload).replace("</", "<\\/")
    return CHART_HTML.replace("__TITLE__", html.escape(str(res["name"]))).replace("__DATA__", data)


def write_chart_html(res: dict, intraday: dict = None) -> str:
    """Save the chart page to a temp file and return its path."""
    fd, path = tempfile.mkstemp(suffix=".html", prefix="chart_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(build_chart_html(res, intraday))
    return path
