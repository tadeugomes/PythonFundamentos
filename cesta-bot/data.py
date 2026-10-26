"""Coleta e cache de preços (Yahoo Finance) e séries macro (Banco Central)."""
from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

B3_TZ = "America/Sao_Paulo"
B3_DATA_READY_HOUR = 18  # depois do leilão de fechamento
FIELDS = ("Close", "Open", "Low")


def _yf_symbol(ticker: str) -> str:
    return ticker if ticker.startswith("^") or ticker.endswith(".SA") else f"{ticker}.SA"


def drop_partial_bar(df: pd.DataFrame) -> pd.DataFrame:
    """Remove a barra de hoje se o pregão ainda não fechou (rodada de manhã ou durante o dia)."""
    now = pd.Timestamp.now(tz=B3_TZ)
    if len(df) and df.index[-1].date() == now.date() and now.hour < B3_DATA_READY_HOUR:
        return df.iloc[:-1]
    return df


def _download(tickers: list[str], start: str) -> dict[str, pd.DataFrame]:
    symbols = [_yf_symbol(t) for t in tickers]
    raw = yf.download(symbols, start=start, auto_adjust=True, progress=False, group_by="column")
    out = {}
    for field in FIELDS:
        df = raw[field].copy()
        df.columns = [c.replace(".SA", "") for c in df.columns]
        out[field] = df.sort_index().reindex(columns=tickers)
    out["Close"] = out["Close"].dropna(how="all")
    for field in FIELDS[1:]:
        out[field] = out[field].reindex(out["Close"].index)
    return out


def _cache_ok(df: pd.DataFrame, tickers: list[str], start: str) -> bool:
    missing = [t for t in tickers if t not in df.columns]
    stale = (pd.Timestamp.today() - df.index.max()).days > 3  # tolera fim de semana
    # o cache precisa cobrir o início pedido (uma rodada ao vivo grava só 18 meses)
    covers = df.index.min() <= pd.Timestamp(start) + pd.Timedelta(days=7)
    return not missing and not stale and covers


def load_prices(
    tickers: list[str],
    start: str,
    refresh: bool = False,
    cache: bool = True,
    extra: tuple[str, ...] = (),
) -> pd.DataFrame | tuple[pd.DataFrame, ...]:
    """Preços ajustados, uma coluna por ticker (sem sufixo .SA).

    Devolve o fechamento, ou (fechamento, *extra) com `extra=("Open", "Low")`.
    `cache=False` baixa sem ler nem gravar o cache (uso ao vivo).
    """
    paths = {f: CACHE_DIR / f"prices_{f.lower()}.parquet" for f in FIELDS}
    data = None
    if cache and not refresh and all(p.exists() for p in paths.values()):
        cached = {f: pd.read_parquet(p) for f, p in paths.items()}
        if _cache_ok(cached["Close"], tickers, start):
            data = {f: df.loc[pd.Timestamp(start):, tickers] for f, df in cached.items()}
    if data is None:
        data = _download(tickers, start)
        if cache:
            for f, p in paths.items():
                data[f].to_parquet(p)

    close = drop_partial_bar(data["Close"])
    empty = [t for t in tickers if close[t].isna().all()]
    if empty:
        print(f"[dados] sem cotação no Yahoo (ticker mudou ou saiu da bolsa?): {', '.join(empty)}")
    if extra:
        return (close, *(data[f].reindex(close.index) for f in extra))
    return close


def _sgs_get(series: int, start: pd.Timestamp, end: pd.Timestamp, tries: int = 5) -> list[dict]:
    url = (
        f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{series}/dados"
        f"?formato=json&dataInicial={start:%d/%m/%Y}&dataFinal={end:%d/%m/%Y}"
    )
    for i in range(tries):
        try:
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError):
            if i == tries - 1:
                raise
            time.sleep(2 ** (i + 1))
    return []


def load_cdi_daily(start: str) -> pd.Series:
    """CDI diário (série 12 do SGS), em fração (0.0004 = 0,04% ao dia).

    O SGS aceita no máximo 10 anos por consulta em séries diárias; a coleta vai em janelas de 5 anos.
    """
    cache = CACHE_DIR / "cdi.parquet"
    if cache.exists():
        s = pd.read_parquet(cache)["cdi"]
        fresh = (pd.Timestamp.today() - s.index.max()).days <= 7
        if fresh and s.index.min() <= pd.Timestamp(start) + pd.Timedelta(days=7):
            return s
    rows = []
    t0, today = pd.Timestamp(start), pd.Timestamp.today().normalize()
    while t0 <= today:
        t1 = min(t0 + pd.DateOffset(years=5) - pd.Timedelta(days=1), today)
        rows += _sgs_get(12, t0, t1)
        t0 = t1 + pd.Timedelta(days=1)
    df = pd.DataFrame(rows)
    df["data"] = pd.to_datetime(df["data"], dayfirst=True)
    s = (df.set_index("data")["valor"].astype(float) / 100).rename("cdi")
    s = s[~s.index.duplicated()].sort_index()
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
        # a data fica numa coluna: df.attrs não é garantido no parquet
        if "asof" in df and (pd.Timestamp.today() - pd.Timestamp(df["asof"].iloc[0])).days <= 30:
            return df.drop(columns="asof")
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
    df.assign(asof=str(pd.Timestamp.today().date())).to_parquet(cache)
    return df
