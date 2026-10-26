"""Walk-forward dentro de 2020-2026: escolhe a melhor combinação com os anos anteriores e aplica no ano seguinte.

Rode da pasta cesta-bot: python experimentos/walkforward.py
"""
import pickle, sys
from pathlib import Path
from multiprocessing import Pool
sys.path.insert(0, ".")
sys.argv = sys.argv[:1]
exec(open("experimentos/grid4.py").read().split("\nif __name__")[0])

CACHE = Path("output/walkforward_curves.pkl")

def curve_job(o):
    cfg = copy.deepcopy(cfg0); cfg["strategy"].update(o)
    cur, _ = run(CLOSE, OPEN, LOW, BENCH, cfg, "2020-01-01", CDI, VOL)
    return cur["equity"]

def cagr(s):
    yrs = (s.index[-1] - s.index[0]).days / 365.25
    return (s.iloc[-1] / s.iloc[0]) ** (1 / yrs) - 1

if __name__ == "__main__":
    cs = combos()
    if CACHE.exists():
        curves = pickle.loads(CACHE.read_bytes())
    else:
        with Pool(4) as p: curves = p.map(curve_job, cs)
        CACHE.write_bytes(pickle.dumps(curves))
    default = curve_job({})
    eq = pd.DataFrame({i: c for i, c in enumerate(curves)})
    bench = BENCH.reindex(eq.index, method="ffill")
    yr_end = eq.resample("YE").last()
    first = eq.iloc[0]
    yearly = pd.concat([first.to_frame().T, yr_end]).pct_change().iloc[1:]
    yearly.index = yearly.index.year
    def ann(r): return float((1 + r).prod() ** (1 / len(r)) - 1)
    b_y = pd.concat([bench.iloc[:1], bench.resample("YE").last()]).pct_change().iloc[1:]; b_y.index = b_y.index.year
    d_y = pd.concat([default.iloc[:1], default.resample("YE").last()]).pct_change().iloc[1:]; d_y.index = d_y.index.year

    print("== divisão: escolhe em 2020-2022, aplica em 2023-2026")
    is_ = yearly.loc[2020:2022].apply(ann); oos = yearly.loc[2023:].apply(ann)
    pick = is_.idxmax()
    print(f"melhor em 2020-2022: {is_.max():.1%} a.a. -> em 2023-2026: {oos[pick]:.1%} a.a.")
    print(f"top 10 de 2020-2022 em 2023-2026: média {oos[is_.nlargest(10).index].mean():.1%} | mediana de todas: {oos.median():.1%}")
    print(f"correlação de posição (2020-22 x 2023-26): {is_.rank().corr(oos.rank()):.2f}")
    print(f"padrão atual em 2023-2026: {ann(d_y.loc[2023:]):.1%} | Ibovespa: {ann(b_y.loc[2023:]):.1%}")

    for look in (1, 2, 3):
        print(f"\n== walk-forward anual, escolha pelos últimos {look} ano(s)")
        rows = []
        for y in range(2020 + look, 2027):
            past = yearly.loc[y - look:y - 1].apply(ann)
            pick = past.idxmax()
            rows.append({"ano": y, "escolhida": int(pick), "na escolha": past[pick], "no ano": yearly.loc[y, pick],
                         "padrão": d_y.loc[y], "Ibov": b_y.loc[y], "pos. no ano": int(yearly.loc[y].rank(ascending=False)[pick])})
        t = pd.DataFrame(rows).set_index("ano")
        print((t[["na escolha", "no ano", "padrão", "Ibov"]] * 100).round(1).assign(**{"posição (de 256)": t["pos. no ano"], "combo": t["escolhida"]}).to_string())
        print(f"encadeado: walk-forward {ann(t['no ano']):.1%} a.a. | padrão {ann(t['padrão']):.1%} | Ibovespa {ann(t['Ibov']):.1%}")
