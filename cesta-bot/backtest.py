"""Backtest semanal da cesta contra Ibovespa e CDI, com custos.

Uso:  python backtest.py [--start 2015-01-01] [--refresh]
Saída: relatório no terminal e output/backtest.csv / output/backtest.png
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from data import load_cdi_daily, load_prices
from strategy import Position, decide


def run(prices: pd.DataFrame, bench: pd.Series, cfg: dict, start: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    c = cfg["costs"]
    cost = c["commission"] + c["b3_fees"] + c["slippage"]
    cash = float(cfg["backtest"]["initial_capital"])
    holdings: dict[str, Position] = {}
    fridays = prices.loc[start:].resample("W-FRI").last().index
    fridays = [f for f in fridays if f in prices.index or prices.index.asof(f) is not pd.NaT]
    rows, trades = [], []

    for f in fridays:
        asof = prices.index.asof(f)
        px_hist = prices.loc[:asof]
        px = px_hist.iloc[-1]
        d = decide(px_hist, bench.loc[:asof], holdings, cfg)

        for t in d.sell:
            pos = holdings.pop(t)
            proceeds = pos.qty * px[t] * (1 - cost)
            cash += proceeds
            trades.append({"date": asof, "ticker": t, "side": "SELL", "price": px[t],
                           "qty": pos.qty, "ret": px[t] / pos.entry_price - 1, "reason": d.reasons.get(t)})

        equity_now = cash + sum(p.qty * px[t] for t, p in holdings.items())
        target_n = cfg["strategy"]["n_positions"]
        per_pos = equity_now * d.exposure / target_n
        for t in d.buy:
            if cash < per_pos * 0.5:
                break
            amount = min(per_pos, cash)
            qty = amount * (1 - cost) / px[t]
            cash -= amount
            holdings[t] = Position(entry_price=px[t], peak=px[t], qty=qty)
            trades.append({"date": asof, "ticker": t, "side": "BUY", "price": px[t],
                           "qty": qty, "ret": np.nan, "reason": d.reasons.get(t)})

        for p in holdings.values():
            p.weeks_held += 1
        equity = cash + sum(p.qty * px[t] for t, p in holdings.items())
        rows.append({"date": asof, "equity": equity, "cash": cash, "n": len(holdings), "exposure": d.exposure})

    curve = pd.DataFrame(rows).set_index("date")
    return curve, pd.DataFrame(trades)


def metrics(series: pd.Series, rf_weekly: pd.Series | None = None) -> dict:
    r = series.pct_change().dropna()
    years = (series.index[-1] - series.index[0]).days / 365.25
    cagr = (series.iloc[-1] / series.iloc[0]) ** (1 / years) - 1
    dd = (series / series.cummax() - 1).min()
    ex = r - (rf_weekly.reindex(r.index).fillna(0) if rf_weekly is not None else 0)
    sharpe = ex.mean() / ex.std() * np.sqrt(52) if ex.std() > 0 else np.nan
    return {"CAGR": cagr, "MaxDD": dd, "Sharpe": sharpe, "Total": series.iloc[-1] / series.iloc[0] - 1}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    start = args.start or cfg["backtest"]["start"]
    warmup = (pd.Timestamp(start) - pd.DateOffset(months=15)).strftime("%Y-%m-%d")

    tickers = cfg["universe"]
    prices = load_prices(tickers + [cfg["benchmark"]], warmup, refresh=args.refresh)
    bench = prices[cfg["benchmark"]].dropna()
    prices = prices[tickers]

    curve, trades = run(prices, bench, cfg, start)
    bench_w = bench.reindex(curve.index, method="ffill")
    bench_w = bench_w / bench_w.iloc[0] * curve["equity"].iloc[0]
    try:
        cdi = load_cdi_daily(start)
        cdi_curve = (1 + cdi).cumprod().reindex(curve.index, method="ffill")
        cdi_curve = cdi_curve / cdi_curve.iloc[0] * curve["equity"].iloc[0]
    except Exception:
        weekly = (1 + cfg["backtest"]["cdi_annual"]) ** (1 / 52) - 1
        cdi_curve = pd.Series((1 + weekly) ** np.arange(len(curve)), index=curve.index) * curve["equity"].iloc[0]
    rf_w = cdi_curve.pct_change()

    out = Path("output"); out.mkdir(exist_ok=True)
    res = pd.DataFrame({"cesta": curve["equity"], "ibov": bench_w, "cdi": cdi_curve})
    res.to_csv(out / "backtest.csv")
    trades.to_csv(out / "trades.csv", index=False)

    print(f"Período: {curve.index[0].date()} a {curve.index[-1].date()}  |  rebalanceamentos: {len(curve)}")
    print(f"{'':8}{'CAGR':>9}{'MaxDD':>9}{'Sharpe':>9}{'Total':>9}")
    for name, s in res.items():
        m = metrics(s, rf_w)
        print(f"{name:8}{m['CAGR']:>9.1%}{m['MaxDD']:>9.1%}{m['Sharpe']:>9.2f}{m['Total']:>9.0%}")
    if len(trades):
        closed = trades[trades.side == "SELL"]
        print(f"\nOperações fechadas: {len(closed)}  |  acerto: {(closed.ret > 0).mean():.0%}  "
              f"|  retorno médio: {closed.ret.mean():+.1%}  |  giro anual: {len(closed) / ((curve.index[-1]-curve.index[0]).days/365.25):.0f} trades")
        print("Motivos de saída:", closed.reason.str.split(" (").str[0].value_counts().to_dict())

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
