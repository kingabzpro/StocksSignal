"""Validate stock symbols and retrieve real Yahoo market data off the UI thread."""

import asyncio
import logging
import math
import re
from datetime import datetime, timezone

import yfinance as yf

logging.getLogger("yfinance").setLevel(logging.CRITICAL)


def parse_tickers(text: str, existing: tuple[str, ...] | list[str] = ()) -> list[str]:
    tickers = [ticker for ticker in re.split(r"[\s,]+", text.strip().upper()) if ticker]
    if not tickers or any(not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,14}", t) for t in tickers):
        raise ValueError("Enter stock symbols separated by commas or spaces (for example AAPL, BRK-B).")
    if len(set(tickers)) != len(tickers) or set(tickers).intersection(existing):
        raise ValueError("Duplicate ticker. Each stock can appear only once.")
    if len(existing) + len(tickers) > 5:
        raise ValueError("StockSignal supports a maximum of 5 stocks.")
    return tickers


def number(value, *, positive=False) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) and (not positive or result > 0) else None


def _fetch_market(ticker: str) -> dict:
    try:
        stock = yf.Ticker(ticker)
        history = stock.history(period="1y", auto_adjust=False, timeout=15, raise_errors=True)
        info = stock.get_info()
    except Exception:
        raise ValueError(f"{ticker}: Yahoo market data unavailable. Check the symbol or retry.") from None
    if history.empty or info.get("quoteType") != "EQUITY":
        raise ValueError(f"{ticker}: no verified stock data. Use a listed equity symbol.")
    closes = history["Close"].dropna()
    if closes.empty:
        raise ValueError(f"{ticker}: no valid closing prices available.")
    last_close = number(closes.iloc[-1], positive=True)
    if last_close is None:
        raise ValueError(f"{ticker}: invalid market price returned by Yahoo.")
    quote_time = number(info.get("regularMarketTime"), positive=True)
    quote_price = number(info.get("regularMarketPrice"), positive=True)
    use_quote = quote_price is not None and quote_time is not None
    price = quote_price if use_quote else last_close
    previous = (number(info.get("regularMarketPreviousClose"), positive=True)
                or number(info.get("previousClose"), positive=True)) if use_quote else (
                    number(closes.iloc[-2], positive=True) if len(closes) > 1 else None)
    adjusted = history.get("Adj Close", closes).dropna()

    def performance(sessions):
        if len(adjusted) <= sessions:
            return None
        start = number(adjusted.iloc[-sessions - 1], positive=True)
        end = number(adjusted.iloc[-1], positive=True)
        return (end / start - 1) * 100 if start and end else None

    return {
        "ticker": ticker,
        "company": info.get("longName") or info.get("shortName") or ticker,
        "currency": info.get("currency"),
        "price": price,
        "price_basis": "Latest regular-session quote" if use_quote else "Latest daily close",
        "price_as_of": datetime.fromtimestamp(quote_time, timezone.utc).isoformat() if use_quote else str(closes.index[-1].date()),
        "change_percent": (price / previous - 1) * 100 if previous else None,
        "market_cap": number(info.get("marketCap"), positive=True),
        "high_52w": number(info.get("fiftyTwoWeekHigh"), positive=True),
        "low_52w": number(info.get("fiftyTwoWeekLow"), positive=True),
        "return_5_sessions_percent": performance(5),
        "return_20_sessions_percent": performance(20),
        "history_latest_session": str(closes.index[-1].date()),
        "recent_history": [
            {"date": str(index.date()), "close": number(row["Close"], positive=True),
             "adjusted_close": number(row.get("Adj Close"), positive=True)}
            for index, row in history.tail(21).iterrows()
        ],
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }


async def fetch_market(ticker: str) -> dict:
    return await asyncio.to_thread(_fetch_market, ticker)
