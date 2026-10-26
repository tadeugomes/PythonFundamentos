"""Opção 1: ranking só entre as maiores (liquidez) e/ou peso por liquidez.

Rode da pasta cesta-bot: python experimentos/grid3.py
"""
import copy, itertools, sys
from multiprocessing import Pool
sys.path.insert(0, ".")
import pandas as pd, yaml
from backtest import run, metrics
from data import load_prices, load_cdi_daily

cfg0 = yaml.safe_load(open("config.yaml", encoding="utf-8"))
tk = cfg0["universe"]
CLOSE, OPEN, LOW, VOL = load_prices(tk + [cfg0["benchmark"]], "2013-10-01", extra=("Open", "Low", "Volume"))
BENCH = CLOSE[cfg0["benchmark"]].dropna(); CLOSE, OPEN, LOW, VOL = CLOSE[tk], OPEN[tk], LOW[tk], VOL[tk]
CDI = load_cdi_daily("2013-10-01")
WINDOWS = {"IS": ("2015-01-01", "2021-10-22"), "OOS": ("2021-10-26", None)}

def evaluate(over, window, return_curve=False):
    start, end = WINDOWS[window]; sl = slice(None, end)
    cfg = copy.deepcopy(cfg0); cfg["strategy"].update({k: v for k, v in over.items() if not k.startswith("_")})
    curve, trades = run(CLOSE.loc[sl], OPEN.loc[sl], LOW.loc[sl], BENCH.loc[sl], cfg, start, CDI.loc[sl], VOL.loc[sl])
    if return_curve: return curve
    eq = curve["equity"]; rf = (1 + CDI).cumprod().reindex(eq.index, method="ffill").pct_change()
    m = metrics(eq, rf); b = metrics(BENCH.reindex(eq.index, method="ffill"), rf)
    return {**over, "window": window, "cagr": m["CAGR"], "dd": m["MaxDD"], "sharpe": m["Sharpe"],
            "excess": m["CAGR"] - b["CAGR"], "ibov": b["CAGR"]}

def job(a): return evaluate(*a)

def combos():
    out = []
    for base, top, wt, reg in itertools.product(["weekly", "daily"], [0, 10, 15, 20], ["equal", "liquidity"], ["on", "off"]):
        o = {"_base": base, "size_top": top, "weighting": wt, "_regime": reg}
        if base == "daily": o.update(check="daily", refill="daily")
        if reg == "off": o["regime_exposure"] = 1.0
        out.append(o)
    return out

if __name__ == "__main__":
    tasks = [(o, w) for o in combos() for w in WINDOWS]
    with Pool(4) as p: rows = p.map(job, tasks)
    df = pd.DataFrame(rows)
    df.to_csv("output/grid3.csv", index=False)
    w = df.pivot_table(index=["_base", "size_top", "weighting", "_regime"], columns="window", values=["cagr", "excess", "dd"])
    w.columns = [f"{a}_{b}" for a, b in w.columns]
    print(f"Ibov IS {df[df.window=='IS'].ibov.iloc[0]:.1%}  OOS {df[df.window=='OOS'].ibov.iloc[0]:.1%}")
    print("excesso > 0 fora da amostra:", int((w.excess_OOS > 0).sum()), "de", len(w), "| correlação IS x OOS:", round(w.excess_IS.corr(w.excess_OOS), 2))
    print((w.sort_values("excess_IS", ascending=False)[["cagr_IS", "excess_IS", "dd_IS", "cagr_OOS", "excess_OOS", "dd_OOS"]] * 100).round(1).to_string())
