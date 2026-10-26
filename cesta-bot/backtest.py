"""Backtest semanal da cesta contra Ibovespa e CDI, com custos.

Uso:  python backtest.py [--start 5y|2015-01-01] [--refresh] [--execution close|next_open]
                         [--no-cash-yield] [--no-resize] [--check weekly|daily]
                         [--refill weekly|daily] [--stop-type close|intraday] [--trailing-stop 0.12]
Saída: relatório no terminal e output/backtest.csv / output/backtest.png

O sinal usa o fechamento de sexta. Por padrão as ordens são executadas na abertura
do pregão seguinte, como acontece ao vivo. O caixa parado rende CDI.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from data import load_cdi_daily, load_prices
from strategy import Position, decide


def run(
    close: pd.DataFrame,
    open_: pd.DataFrame,
    low: pd.DataFrame,
    bench: pd.Series,
    cfg: dict,
    start: str,
    cdi: pd.Series | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Simula dia a dia. Sexta: decisão completa. Outros dias: conforme `strategy.check`,
    `strategy.refill` e `strategy.stop_type`. Decisões no fechamento, execução na abertura seguinte."""
    c = cfg["costs"]
    bt = cfg["backtest"]
    st = cfg["strategy"]
    cost = c["commission"] + c["b3_fees"] + c["slippage"]
    next_open = bt.get("execution", "next_open") == "next_open"
    resize = st.get("resize_on_regime", True)
    daily_check = st.get("check", "weekly") == "daily"
    daily_refill = st.get("refill", "weekly") == "daily"
    intraday = st.get("stop_type", "close") == "intraday"
    target_n = st["n_positions"]
    stop = st["trailing_stop"]
    cash = float(bt["initial_capital"])
    holdings: dict[str, Position] = {}
    days = close.index
    close_ff = close.ffill()  # marcação a mercado de ativos sem negócio no dia
    fridays = close.loc[start:].resample("W-FRI").last().index
    signal_days = sorted({days.asof(f) for f in fridays})
    weekly_set = set(signal_days)
    sim_days = days[days >= signal_days[0]]
    rows, trades = [], []
    state = {"cash": cash, "prev_exposure": None}
    exposure = 1.0
    pending = None  # (decisão, é semanal) a executar no próximo pregão
    prev_day = None

    def trade(day, t, side, qty, price, reason, ret=np.nan):
        trades.append({"date": day, "ticker": t, "side": side, "price": price, "qty": qty,
                       "ret": ret, "reason": reason})

    def execute(d, px, day, weekly):
        cash = state["cash"]
        for t in d.sell:
            if t not in holdings:
                continue  # já saiu por stop no pregão
            if pd.isna(px.get(t)):  # sem negócio no dia da execução: tenta de novo depois
                continue
            pos = holdings.pop(t)
            cash += pos.qty * px[t] * (1 - cost)
            trade(day, t, "SELL", pos.qty, px[t], d.reasons.get(t), px[t] / pos.entry_price - 1)

        equity_now = cash + sum(p.qty * px[t] for t, p in holdings.items() if not pd.isna(px[t]))
        per_pos = equity_now * d.exposure / target_n

        # mudança de regime: leva as posições mantidas ao novo tamanho alvo
        if weekly and resize and state["prev_exposure"] is not None and d.exposure != state["prev_exposure"]:
            for t, pos in holdings.items():
                if pd.isna(px[t]):
                    continue
                delta = per_pos / px[t] - pos.qty
                if delta < 0:
                    cash += -delta * px[t] * (1 - cost)
                    pos.qty += delta
                    trade(day, t, "TRIM", -delta, px[t], f"regime {d.exposure:.0%}")
                elif delta > 0 and cash > 0:
                    amount = min(delta * px[t], cash)
                    pos.qty += amount * (1 - cost) / px[t]
                    cash -= amount
                    trade(day, t, "ADD", amount / px[t], px[t], f"regime {d.exposure:.0%}")
        if weekly:
            state["prev_exposure"] = d.exposure

        for t in d.buy:
            if len(holdings) >= target_n or cash < per_pos * 0.5:
                break
            if t in holdings or pd.isna(px.get(t)):
                continue
            amount = min(per_pos, cash)
            qty = amount * (1 - cost) / px[t]
            cash -= amount
            holdings[t] = Position(entry_price=px[t], peak=px[t], qty=qty)
            trade(day, t, "BUY", qty, px[t], d.reasons.get(t))

        if weekly:
            for p in holdings.values():
                p.weeks_held += 1
        state["cash"] = cash

    for s in sim_days:
        if cdi is not None and prev_day is not None:
            state["cash"] *= float((1 + cdi.loc[prev_day + pd.Timedelta(days=1):s]).prod())
        prev_day = s

        if pending is not None:
            d, weekly = pending
            pending = None
            execute(d, open_.loc[s].fillna(close.loc[s]), s, weekly)

        if intraday:
            # stop executado no pregão: na mínima que cruza o nível, ou na abertura se abrir abaixo
            for t, pos in list(holdings.items()):
                level = pos.peak * (1 - stop)
                lo, op = low.at[s, t], open_.at[s, t]
                if not pd.isna(lo) and lo <= level:
                    fill = min(op, level) if not pd.isna(op) else level
                    holdings.pop(t)
                    state["cash"] += pos.qty * fill * (1 - cost)
                    trade(s, t, "SELL", pos.qty, fill, "stop no pregão", fill / pos.entry_price - 1)
            for t, pos in holdings.items():
                if not pd.isna(close.at[s, t]):
                    pos.peak = max(pos.peak, close.at[s, t])

        if s in weekly_set:
            equity = state["cash"] + sum(p.qty * close_ff.at[s, t] for t, p in holdings.items())
            d = decide(close.loc[:s], bench.loc[:s], holdings, cfg)
            exposure = d.exposure
            rows.append({"date": s, "equity": equity, "cash": state["cash"], "n": len(holdings),
                         "exposure": d.exposure})
            pending = (d, True)
        elif daily_check or daily_refill:
            d = decide(close.loc[:s], bench.loc[:s], holdings, cfg, exits=daily_check, rank_exit=False)
            d.exposure = exposure  # regime só muda na sexta
            if not daily_refill:
                d.buy = []
            if d.sell or d.buy:
                pending = (d, False)

        if pending is not None and not next_open:
            d, weekly = pending
            pending = None
            execute(d, close.loc[s], s, weekly)

    curve = pd.DataFrame(rows).set_index("date")
    return curve, pd.DataFrame(trades)


def metrics(series: pd.Series, rf_weekly: pd.Series | None = None) -> dict:
    r = series.pct_change().dropna()
    years = (series.index[-1] - series.index[0]).days / 365.25
    cagr = (series.iloc[-1] / series.iloc[0]) ** (1 / years) - 1
    dd = (series / series.cummax() - 1).min()
    ex = r - (rf_weekly.reindex(r.index).fillna(0) if rf_weekly is not None else 0)
    sharpe = ex.mean() / ex.std() * np.sqrt(52) if ex.std() > 1e-12 else np.nan
    return {"CAGR": cagr, "MaxDD": dd, "Sharpe": sharpe, "Total": series.iloc[-1] / series.iloc[0] - 1}


def parse_start(value: str) -> str:
    """Aceita data (2015-01-01) ou janela relativa a hoje (5y = últimos 5 anos)."""
    value = str(value)
    if value.endswith("y") and value[:-1].isdigit():
        return (pd.Timestamp.today().normalize() - pd.DateOffset(years=int(value[:-1]))).strftime("%Y-%m-%d")
    return value


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--execution", choices=["close", "next_open"], default=None)
    ap.add_argument("--no-cash-yield", action="store_true", help="caixa parado não rende CDI")
    ap.add_argument("--no-resize", action="store_true", help="regime só afeta compras novas")
    ap.add_argument("--check", choices=["weekly", "daily"], default=None, help="frequência das saídas")
    ap.add_argument("--refill", choices=["weekly", "daily"], default=None, help="frequência de reposição de vagas")
    ap.add_argument("--stop-type", choices=["close", "intraday"], default=None)
    ap.add_argument("--trailing-stop", type=float, default=None)
    ap.add_argument("--out", default="output")
    args = ap.parse_args()
    cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    if args.execution:
        cfg["backtest"]["execution"] = args.execution
    if args.no_resize:
        cfg["strategy"]["resize_on_regime"] = False
    for key, val in [("check", args.check), ("refill", args.refill), ("stop_type", args.stop_type),
                     ("trailing_stop", args.trailing_stop)]:
        if val is not None:
            cfg["strategy"][key] = val
    start = parse_start(args.start or cfg["backtest"]["start"])
    warmup = (pd.Timestamp(start) - pd.DateOffset(months=15)).strftime("%Y-%m-%d")

    tickers = cfg["universe"]
    close, open_, low = load_prices(tickers + [cfg["benchmark"]], warmup, refresh=args.refresh,
                                    extra=("Open", "Low"))
    bench = close[cfg["benchmark"]].dropna()
    close, open_, low = close[tickers], open_[tickers], low[tickers]

    try:
        cdi = load_cdi_daily(warmup)
    except Exception as e:
        print(f"[cdi] série do BCB indisponível ({e}); usando {cfg['backtest']['cdi_annual']:.0%} a.a. fixo")
        cdi = None
    use_yield = cfg["backtest"].get("cash_yield", True) and not args.no_cash_yield

    curve, trades = run(close, open_, low, bench, cfg, start, cdi if use_yield else None)
    bench_w = bench.reindex(curve.index, method="ffill")
    bench_w = bench_w / bench_w.iloc[0] * curve["equity"].iloc[0]
    if cdi is not None:
        cdi_curve = (1 + cdi).cumprod().reindex(curve.index, method="ffill")
        cdi_curve = cdi_curve / cdi_curve.iloc[0] * curve["equity"].iloc[0]
    else:
        weekly = (1 + cfg["backtest"]["cdi_annual"]) ** (1 / 52) - 1
        cdi_curve = pd.Series((1 + weekly) ** np.arange(len(curve)), index=curve.index) * curve["equity"].iloc[0]
    rf_w = cdi_curve.pct_change()

    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    res = pd.DataFrame({"cesta": curve["equity"], "ibov": bench_w, "cdi": cdi_curve})
    res.to_csv(out / "backtest.csv")
    trades.to_csv(out / "trades.csv", index=False)

    first = close.loc[start:].iloc[0]
    print(f"Universo: {int(first.notna().sum())} de {len(tickers)} ativos com cotação em {first.name.date()} "
          "(lista atual do índice: há viés de sobrevivência)")
    st = cfg["strategy"]
    print(f"Saídas: {st.get('check', 'weekly')}  |  reposição: {st.get('refill', 'weekly')}  |  stop: "
          f"{st.get('stop_type', 'close')} {st['trailing_stop']:.0%}")
    print(f"Execução: {cfg['backtest'].get('execution', 'next_open')}  |  caixa rende CDI: "
          f"{'sim' if use_yield and cdi is not None else 'não'}  |  ajuste no regime: "
          f"{'sim' if cfg['strategy'].get('resize_on_regime', True) else 'não'}")
    print(f"Período: {curve.index[0].date()} a {curve.index[-1].date()}  |  rebalanceamentos: {len(curve)}  "
          f"|  semanas em regime 50%: {(curve.exposure < 1).mean():.0%}  |  caixa médio: "
          f"{(curve.cash / curve.equity).mean():.0%}")
    print(f"{'':8}{'CAGR':>9}{'MaxDD':>9}{'Sharpe':>9}{'Total':>9}")
    for name, s in res.items():
        m = metrics(s, rf_w)
        print(f"{name:8}{m['CAGR']:>9.1%}{m['MaxDD']:>9.1%}{m['Sharpe']:>9.2f}{m['Total']:>9.0%}")
    if len(trades):
        closed = trades[trades.side == "SELL"]
        years = (curve.index[-1] - curve.index[0]).days / 365.25
        print(f"\nOperações fechadas: {len(closed)}  |  acerto: {(closed.ret > 0).mean():.0%}  "
              f"|  retorno médio: {closed.ret.mean():+.1%}  |  giro anual: {len(closed) / years:.0f} trades")
        print("Motivos de saída:", closed.reason.str.split(" (", regex=False).str[0].value_counts().to_dict())

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        ax = res.plot(figsize=(10, 5), logy=True, title="Cesta semanal vs Ibovespa vs CDI")
        ax.set_ylabel("R$ (log)")
        plt.tight_layout(); plt.savefig(out / "backtest.png", dpi=120)
    except Exception as e:
        print("gráfico não gerado:", e)


if __name__ == "__main__":
    main()
