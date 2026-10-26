"""Máximo retorno com 10 posições a partir de 2020 (otimizado no próprio período) e checagem em 2015-2019.

Rode da pasta cesta-bot: python experimentos/grid4.py
"""
import copy, itertools, sys
from multiprocessing import Pool
sys.path.insert(0, ".")
sys.argv = sys.argv[:1]
exec(open("experimentos/grid3.py").read().split('if __name__')[0])

WINDOWS.update({"2020+": ("2020-01-01", None), "2015-19": ("2015-01-01", "2019-12-31")})

def combos():
    out = []
    for top, wt, lb, sma, ts, vol, chk in itertools.product(
            [0, 10, 15, 20], ["equal", "liquidity"], [126, 252], [20, 50], [0.12, 0.25],
            [0.60, 9.0], ["weekly", "daily"]):
        o = {"n_positions": 10, "regime_exposure": 1.0, "size_top": top, "weighting": wt,
             "momentum_lookback": lb, "trend_sma": sma, "exit_sma": sma, "trailing_stop": ts, "max_vol_annual": vol}
        if chk == "daily": o.update(check="daily", refill="daily")
        out.append(o)
    return out

if __name__ == "__main__":
    cs = combos()
    with Pool(4) as p: rows = p.map(job, [(o, "2020+") for o in cs])
    df = pd.DataFrame(rows).sort_values("cagr", ascending=False)
    df.to_csv("output/grid4.csv", index=False)
    cols = ["size_top", "weighting", "momentum_lookback", "trend_sma", "trailing_stop", "max_vol_annual", "check", "cagr", "dd", "sharpe"]
    df["check"] = df["check"].fillna("weekly")
    print(f"Ibovespa {df.ibov.iloc[0]:.1%} a.a. | combinações acima do Ibovespa: {(df.excess > 0).sum()} de {len(df)} | mediana {df.cagr.median():.1%}")
    print(df[cols].head(10).to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    best = [{k: v for k, v in r.items() if k in cs[0] or k in ("check", "refill")} for r in df.head(5).to_dict("records")]
    for o in best:
        if o.get("check") == "weekly": o.pop("check", None); o.pop("refill", None)
    with Pool(4) as p: val = p.map(job, [(o, "2015-19") for o in best])
    print("\nas 5 melhores rodadas em 2015-2019 (fora da escolha):")
    for r in val: print(f"  {r['cagr']:.1%} a.a. | DD {r['dd']:.1%} | Ibovespa {r['ibov']:.1%}")
