"""Regras de seleção e saída da cesta.

Fatores de preço (usados no backtest e ao vivo):
  - momentum 12-1: retorno dos últimos 12 meses excluindo o último mês
  - tendência: preço acima da SMA de 50 dias
  - volatilidade: exclui ativos com vol anualizada acima do limite
Saída por tendência: fechamento abaixo da SMA de saída (`exit_sma`, padrão = `trend_sma`)
por `exit_confirm_days` pregões seguidos.
Regime: IBOV abaixo da SMA200 reduz a exposição total. Com `regime_confirm_weeks` > 1,
o regime só troca depois de N leituras semanais seguidas do outro lado da média.
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
    weights: dict[str, float] = field(default_factory=dict)  # fração da carteira por ativo (sem regime)


def momentum_12_1(prices: pd.DataFrame, lookback: int, skip: int) -> pd.Series:
    past = prices.shift(skip)
    return past.iloc[-1] / past.iloc[-lookback] - 1


def annual_vol(prices: pd.DataFrame, lookback: int) -> pd.Series:
    return prices.pct_change().iloc[-lookback:].std() * np.sqrt(252)


def regime_exposure(bench: pd.Series, sma: int, low_exposure: float, confirm_weeks: int = 1) -> float:
    if len(bench) < sma:
        return 1.0
    if confirm_weeks <= 1:
        return 1.0 if bench.iloc[-1] >= bench.iloc[-sma:].mean() else low_exposure
    # leituras semanais (último pregão de cada semana, mais o dia atual) e troca só com N seguidas
    above = (bench >= bench.rolling(sma).mean()).where(bench.rolling(sma).count() >= sma)
    weekly = above.resample("W-FRI").last().dropna().astype(bool)
    state, run_len, last = True, 0, None
    for v in weekly:
        run_len = run_len + 1 if v == last else 1
        last = v
        if v != state and run_len >= confirm_weeks:
            state = v
    return 1.0 if state else low_exposure


def below_exit_sma(prices: pd.DataFrame, sma: int, days: int) -> pd.Series:
    """True para ativos que fecharam abaixo da SMA de saída nos últimos `days` pregões."""
    window = prices.iloc[-(sma + days):]
    avg = window.rolling(sma).mean().iloc[-days:]
    return (window.iloc[-days:] < avg).all()


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
    size: pd.Series | None = None,
) -> Decision:
    """Decisão com base nos dados até a última linha de `prices`.

    Na sexta roda completa. Nos outros dias (checagem diária) usa `rank_exit=False`,
    porque sair do top momentum é critério de reavaliação semanal, ou `exits=False`
    para só preencher vagas abertas.
    `size`: volume financeiro médio por ativo, usado por `size_top` e `weighting: liquidity`.
    """
    s = cfg["strategy"]
    n = s["n_positions"]
    exit_sma = s.get("exit_sma", s["trend_sma"])
    exit_days = s.get("exit_confirm_days", 1)
    need = max(s["momentum_lookback"] + s["momentum_skip"], s["trend_sma"], s["vol_lookback"],
               exit_sma + exit_days) + 5
    if len(prices) < need:
        return Decision(hold=list(holdings), exposure=1.0, reasons={"_": "histórico insuficiente"})

    last = prices.iloc[-1]
    sma = prices.iloc[-s["trend_sma"]:].mean()
    mom = momentum_12_1(prices, s["momentum_lookback"], s["momentum_skip"])
    vol = annual_vol(prices, s["vol_lookback"])
    exit_trend = below_exit_sma(prices, exit_sma, exit_days)

    eligible = (last > sma) & (vol <= s["max_vol_annual"]) & mom.notna() & last.notna()
    eligible &= pd.Series({t: fundamental_ok(fund, t) for t in prices.columns})
    if s.get("size_top", 0) and size is not None:
        biggest = size.dropna().sort_values(ascending=False).index[: s["size_top"]]
        eligible &= eligible.index.isin(biggest)
    ranked = mom[eligible].sort_values(ascending=False)
    pool = set(ranked.index[: 2 * n])  # zona de permanência: top 2n

    exposure = regime_exposure(bench, s["regime_sma"], s["regime_exposure"], s.get("regime_confirm_weeks", 1))
    d = Decision(exposure=exposure, scores=ranked)

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
        elif pos.weeks_held >= s["min_hold_weeks"] and exit_trend.get(t, False):
            d.sell.append(t); d.reasons[t] = f"abaixo da SMA{exit_sma}"
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
    d.weights = target_weights(d.hold + d.buy, n, s, size)
    return d


def target_weights(names: list[str], n: int, s: dict, size: pd.Series | None) -> dict[str, float]:
    """Pesos alvo. Pesos iguais: 1/n cada. Liquidez: as k posições somam k/n,
    divididas pelo volume financeiro, com teto `max_weight` por ativo."""
    if not names:
        return {}
    if s.get("weighting", "equal") != "liquidity" or size is None:
        return {t: 1 / n for t in names}
    total = len(names) / n
    w = size.reindex(names).fillna(0).clip(lower=0)
    if w.sum() <= 0:
        return {t: 1 / n for t in names}
    w = w / w.sum() * total
    cap = s.get("max_weight", 0.25)
    for _ in range(20):  # redistribui o excesso acima do teto
        over = w > cap
        if not over.any() or over.all():
            break
        excess = (w[over] - cap).sum()
        w[over] = cap
        w[~over] += excess * w[~over] / w[~over].sum()
    return w.clip(upper=cap).to_dict()
