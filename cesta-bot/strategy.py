"""Regras de seleção e saída da cesta.

Fatores de preço (usados no backtest e ao vivo):
  - momentum 12-1: retorno dos últimos 12 meses excluindo o último mês
  - tendência: preço acima da SMA de 50 dias
  - volatilidade: exclui ativos com vol anualizada acima do limite
Regime: IBOV abaixo da SMA200 reduz a exposição total.
Fundamentos (só ao vivo): exclui ROE negativo e alavancagem alta.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class Position:
    entry_price: float
    peak: float
    weeks_held: int = 0
    qty: float = 0.0


@dataclass
class Decision:
    buy: list[str] = field(default_factory=list)
    sell: list[str] = field(default_factory=list)
    hold: list[str] = field(default_factory=list)
    exposure: float = 1.0
    reasons: dict[str, str] = field(default_factory=dict)
    scores: pd.Series | None = None


def momentum_12_1(prices: pd.DataFrame, lookback: int, skip: int) -> pd.Series:
    past = prices.shift(skip)
    return past.iloc[-1] / past.iloc[-lookback] - 1


def annual_vol(prices: pd.DataFrame, lookback: int) -> pd.Series:
    return prices.pct_change().iloc[-lookback:].std() * np.sqrt(252)


def regime_exposure(bench: pd.Series, sma: int, low_exposure: float) -> float:
    if len(bench) < sma:
        return 1.0
    return 1.0 if bench.iloc[-1] >= bench.iloc[-sma:].mean() else low_exposure


def fundamental_ok(fund: pd.DataFrame | None, ticker: str) -> bool:
    if fund is None or ticker not in fund.index:
        return True
    row = fund.loc[ticker]
    roe = row.get("roe")
    de = row.get("debt_to_equity")
    if roe is not None and not pd.isna(roe) and roe < 0:
        return False
    if de is not None and not pd.isna(de) and de > 300:  # yfinance reporta em %
        return False
    return True


def decide(
    prices: pd.DataFrame,
    bench: pd.Series,
    holdings: dict[str, Position],
    cfg: dict,
    fund: pd.DataFrame | None = None,
    exits: bool = True,
    rank_exit: bool = True,
) -> Decision:
    """Decisão com base nos dados até a última linha de `prices`.

    Na sexta roda completa. Nos outros dias (checagem diária) usa `rank_exit=False`,
    porque sair do top momentum é critério de reavaliação semanal, ou `exits=False`
    para só preencher vagas abertas.
    """
    s = cfg["strategy"]
    n = s["n_positions"]
    need = max(s["momentum_lookback"] + s["momentum_skip"], s["trend_sma"], s["vol_lookback"]) + 5
    if len(prices) < need:
        return Decision(hold=list(holdings), exposure=1.0, reasons={"_": "histórico insuficiente"})

    last = prices.iloc[-1]
    sma = prices.iloc[-s["trend_sma"]:].mean()
    mom = momentum_12_1(prices, s["momentum_lookback"], s["momentum_skip"])
    vol = annual_vol(prices, s["vol_lookback"])

    eligible = (last > sma) & (vol <= s["max_vol_annual"]) & mom.notna() & last.notna()
    eligible &= pd.Series({t: fundamental_ok(fund, t) for t in prices.columns})
    ranked = mom[eligible].sort_values(ascending=False)
    pool = set(ranked.index[: 2 * n])  # zona de permanência: top 2n

    d = Decision(exposure=regime_exposure(bench, s["regime_sma"], s["regime_exposure"]), scores=ranked)

    for t, pos in holdings.items():
        if not exits:
            d.hold.append(t)
            continue
        price = last.get(t)
        if price is None or pd.isna(price):
            d.sell.append(t); d.reasons[t] = "sem cotação"; continue
        pos.peak = max(pos.peak, price)
        if price < pos.peak * (1 - s["trailing_stop"]):
            d.sell.append(t); d.reasons[t] = f"stop móvel ({price / pos.peak - 1:+.1%} do pico)"
        elif pos.weeks_held >= s["min_hold_weeks"] and price < sma[t]:
            d.sell.append(t); d.reasons[t] = f"abaixo da SMA{s['trend_sma']}"
        elif rank_exit and pos.weeks_held >= s["min_hold_weeks"] and t not in pool:
            d.sell.append(t); d.reasons[t] = "saiu do top momentum"
        else:
            d.hold.append(t)

    slots = n - len(d.hold)
    for t in ranked.index:
        if slots <= 0:
            break
        if t in holdings or t in d.sell:
            continue
        d.buy.append(t); d.reasons[t] = f"momentum 12-1 {ranked[t]:+.1%}"
        slots -= 1
    return d
