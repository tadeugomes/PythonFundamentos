"""Backtest semanal da cesta contra Ibovespa e CDI, com custos.

Uso:  python backtest.py [--start 2015-01-01] [--refresh] [--execution close|next_open]
                         [--no-cash-yield] [--no-resize]
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
    bench: pd.Series,
    cfg: dict,
    start: str,
    cdi: pd.Series | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    c = cfg["costs"]
    bt = cfg["backtest"]
    cost = c["commission"] + c["b3_fees"] + c["slippage"]
    next_open = bt.get("execution", "next_open") == "next_open"
    resize = cfg["strategy"].get("resize_on_regime", True)
    target_n = cfg["strategy"]["n_positions"]
    cash = float(bt["initial_capital"])
    holdings: dict[str, Position] = {}
    days = close.index
    close_ff = close.ffill()  # marcação a mercado de ativos sem negócio no dia
    fridays = close.loc[start:].resample("W-FRI").last().index
    signal_days = sorted({days.asof(f) for f in fridays})
    rows, trades = [], []
    prev_day, prev_exposure = None, None

    for s in signal_days:
        if cdi is not None and prev_day is not None:
            cash *= float((1 + cdi.loc[prev_day + pd.Timedelta(days=1):s]).prod())
        prev_day = s

        px_close = close.loc[s]
        equity_close = cash + sum(p.qty * close_ff.at[s, t] for t, p in holdings.items())
        d = decide(close.loc[:s], bench.loc[:s], holdings, cfg)
        rows.append({"date": s, "equity": equity_close, "cash": cash, "n": len(holdings), "exposure": d.exposure})

        i = days.get_loc(s)
        if next_open:
            if i + 1 >= len(days):
                break  # sem pregão seguinte para executar
            e = days[i + 1]
            px = open_.loc[e].fillna(close.loc[e])
        else:
            e, px = s, px_close

        def trade(t, side, qty, reason, ret=np.nan):
            trades.append({"date": e, "ticker": t, "side": side, "price": px[t], "qty": qty,
                           "ret": ret, "reason": reason})

        for t in d.sell:
            if pd.isna(px.get(t)):  # sem negócio no dia da execução: vende na próxima semana
                d.hold.append(t)
                continue
            pos = holdings.pop(t)
            cash += pos.qty * px[t] * (1 - cost)
            trade(t, "SELL", pos.qty, d.reasons.get(t), px[t] / pos.entry_price - 1)

        equity_now = cash + sum(p.qty * px[t] for t, p in holdings.items() if not pd.isna(px[t]))
        per_pos = equity_now * d.exposure / target_n

        # mudança de regime: leva as posições mantidas ao novo tamanho alvo
        if resize and prev_exposure is not None and d.exposure != prev_exposure:
            for t, pos in holdings.items():
                if pd.isna(px[t]):
                    continue
                delta = per_pos / px[t] - pos.qty
                if delta < 0:
                    cash += -delta * px[t] * (1 - cost)
                    pos.qty += delta
                    trade(t, "TRIM", -delta, f"regime {d.exposure:.0%}")
                elif delta > 0 and cash > 0:
                    amount = min(delta * px[t], cash)
                    pos.qty += amount * (1 - cost) / px[t]
                    cash -= amount
                    trade(t, "ADD", amount / px[t], f"regime {d.exposure:.0%}")
        prev_exposure = d.exposure

        for t in d.buy:
            if cash < per_pos * 0.5:
                break
            if pd.isna(px.get(t)):
                continue
            amount = min(per_pos, cash)
            qty = amount * (1 - cost) / px[t]
            cash -= amount
            holdings[t] = Position(entry_price=px[t], peak=px[t], qty=qty)
            trade(t, "BUY", qty, d.reasons.get(t))

        for p in holdings.values():
            p.weeks_held += 1

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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--execution", choices=["close", "next_open"], default=None)
    ap.add_argument("--no-cash-yield", action="store_true", help="caixa parado não rende CDI")
    ap.add_argument("--no-resize", action="store_true", help="regime só afeta compras novas")
    ap.add_argument("--out", default="output")
    args = ap.parse_args()
    cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    if args.execution:
        cfg["backtest"]["execution"] = args.execution
    if args.no_resize:
        cfg["strategy"]["resize_on_regime"] = False
    start = args.start or cfg["backtest"]["start"]
    warmup = (pd.Timestamp(start) - pd.DateOffset(months=15)).strftime("%Y-%m-%d")

    tickers = cfg["universe"]
    close, open_ = load_prices(tickers + [cfg["benchmark"]], warmup, refresh=args.refresh, with_open=True)
    bench = close[cfg["benchmark"]].dropna()
    close, open_ = close[tickers], open_[tickers]

    try:
        cdi = load_cdi_daily(warmup)
    except Exception as e:
        print(f"[cdi] série do BCB indisponível ({e}); usando {cfg['backtest']['cdi_annual']:.0%} a.a. fixo")
        cdi = None
    use_yield = cfg["backtest"].get("cash_yield", True) and not args.no_cash_yield

    curve, trades = run(close, open_, bench, cfg, start, cdi if use_yield else None)
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
