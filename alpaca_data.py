"""
alpaca_data.py — Real-time US stock quotes via Alpaca Markets (IEX feed).
Free paper-trading account; no SIP/real-time subscription needed.
Credentials loaded from .env (never hardcoded).
"""
import os
from dotenv import load_dotenv

load_dotenv()

_client = None   # lazy singleton

def _get_client():
    global _client
    if _client is None:
        from alpaca.data import StockHistoricalDataClient
        key    = os.getenv("ALPACA_KEY")
        secret = os.getenv("ALPACA_SECRET")
        if not key or not secret:
            raise RuntimeError("Alpaca credentials missing — check .env")
        _client = StockHistoricalDataClient(key, secret)
    return _client


def get_us_snapshots(symbols: list) -> dict:
    """
    Batch real-time snapshot for US stocks via Alpaca IEX feed.

    Returns:
        { symbol: {
            price, prev_close, change, change_pct,
            volume, day_high, day_low,
            bid, ask, timestamp
          }, ... }

    Falls back gracefully to empty dict on any error so callers can
    fall back to yfinance fast_info.
    """
    if not symbols:
        return {}
    try:
        from alpaca.data.requests import StockSnapshotRequest
        snaps = _get_client().get_stock_snapshot(
            StockSnapshotRequest(
                symbol_or_symbols=[s.upper() for s in symbols],
                feed="iex",          # free IEX feed (paper account)
            )
        )
        result = {}
        for sym, s in snaps.items():
            try:
                # Latest trade price (most current)
                price = None
                if s.latest_trade:
                    price = float(s.latest_trade.price)
                elif s.minute_bar:
                    price = float(s.minute_bar.close)

                prev  = float(s.previous_daily_bar.close) if s.previous_daily_bar else None
                vol   = int(s.daily_bar.volume)            if s.daily_bar          else None
                hi    = float(s.daily_bar.high)            if s.daily_bar          else None
                lo    = float(s.daily_bar.low)             if s.daily_bar          else None
                bid   = float(s.latest_quote.bid_price)    if s.latest_quote       else None
                ask   = float(s.latest_quote.ask_price)    if s.latest_quote       else None
                ts    = s.latest_trade.timestamp.strftime("%H:%M:%S") if s.latest_trade else None

                change     = round(price - prev, 2)          if price and prev else None
                change_pct = round((price - prev)/prev*100, 2) if price and prev else None

                result[sym] = {
                    "price":      price,
                    "prev_close": prev,
                    "change":     change,
                    "change_pct": change_pct,
                    "volume":     vol,
                    "day_high":   hi,
                    "day_low":    lo,
                    "bid":        bid,
                    "ask":        ask,
                    "timestamp":  ts,
                    "source":     "Alpaca/IEX",
                }
            except Exception:
                continue
        return result

    except Exception:
        return {}   # caller falls back to yfinance


def get_us_snapshot_single(symbol: str) -> dict:
    """Convenience wrapper for one symbol."""
    result = get_us_snapshots([symbol])
    return result.get(symbol.upper(), {})
