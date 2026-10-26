"""Coleta e cache de preços (Yahoo Finance) e séries macro (Banco Central)."""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)


def _yf_symbol(ticker: str) -> str:
    return ticker if ticker.startswith("^") or ticker.endswith(".SA") else f"{ticker}.SA"


def load_prices(tickers: list[str], start: str, refresh: bool = False) -> pd.DataFrame:
    """Preços de fechamento ajustados, uma coluna por ticker (sem sufixo .SA)."""
    cache = CACHE_DIR / "prices.parquet"
    if cache.exists() and not refresh:
        df = pd.read_parquet(cache)
        missing = [t for t in tickers if t not in df.columns]
        stale = (pd.Timestamp.today() - df.index.max()).days > 1
        if not missing and not stale:
            return df[tickers]

    symbols = [_yf_symbol(t) for t in tickers]
    raw = yf.download(symbols, start=start, auto_adjust=True, progress=False, group_by="column")
    close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw[["Close"]]
    close.columns = [c.replace(".SA", "") for c in close.columns]
    close = close.sort_index().dropna(how="all")
    close.to_parquet(cache)
    return close[tickers]


def load_selic(start: str) -> pd.Series:
    """Meta Selic (série 432 do SGS/BCB), diária, em % a.a."""
    cache = CACHE_DIR / "selic.parquet"
    if cache.exists():
        s = pd.read_parquet(cache)["selic"]
        if (pd.Timestamp.today() - s.index.max()).days <= 7:
            return s
    url = (
        "https://api.bcb.gov.br/dados/serie/bcdata.sgs.432/dados"
        f"?formato=json&dataInicial={pd.Timestamp(start):%d/%m/%Y}"
    )
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    df = pd.DataFrame(r.json())
    df["data"] = pd.to_datetime(df["data"], dayfirst=True)
    s = df.set_index("data")["valor"].astype(float).rename("selic")
    s.to_frame().to_parquet(cache)
    return s


def load_cdi_daily(start: str) -> pd.Series:
    """CDI diário (série 12 do SGS), em fração (0.0004 = 0,04% ao dia)."""
    cache = CACHE_DIR / "cdi.parquet"
    if cache.exists():
        s = pd.read_parquet(cache)["cdi"]
        if (pd.Timestamp.today() - s.index.max()).days <= 7:
            return s
    url = (
        "https://api.bcb.gov.br/dados/serie/bcdata.sgs.12/dados"
        f"?formato=json&dataInicial={pd.Timestamp(start):%d/%m/%Y}"
    )
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    df = pd.DataFrame(r.json())
    df["data"] = pd.to_datetime(df["data"], dayfirst=True)
    s = (df.set_index("data")["valor"].astype(float) / 100).rename("cdi")
    s.to_frame().to_parquet(cache)
    return s


def load_fundamentals(tickers: list[str], refresh: bool = False) -> pd.DataFrame:
    """Snapshot de fundamentos via yfinance .info (lento; use cache).

    Serve ao filtro de qualidade do mês corrente. Não há histórico gratuito
    confiável para usar isso no backtest; o backtest usa só fatores de preço.
    """
    cache = CACHE_DIR / "fundamentals.parquet"
    if cache.exists() and not refresh:
        df = pd.read_parquet(cache)
        if (pd.Timestamp.today() - pd.Timestamp(df.attrs.get("asof", "2000-01-01"))).days <= 30:
            return df
    rows = []
    for t in tickers:
        try:
            info = yf.Ticker(_yf_symbol(t)).info
        except Exception:
            info = {}
        rows.append(
            {
                "ticker": t,
                "roe": info.get("returnOnEquity"),
                "debt_to_equity": info.get("debtToEquity"),
                "revenue_growth": info.get("revenueGrowth"),
                "ev_ebitda": info.get("enterpriseToEbitda"),
                "pe": info.get("trailingPE"),
                "sector": info.get("sector"),
            }
        )
    df = pd.DataFrame(rows).set_index("ticker")
    df.attrs["asof"] = str(pd.Timestamp.today().date())
    df.to_parquet(cache)
    return df
