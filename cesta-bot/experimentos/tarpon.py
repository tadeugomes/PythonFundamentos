"""Tarpon GT: cota real (FIC) e réplica da carteira publicada na CVM com atraso.

Uso: python experimentos/cvm_extract.py PASTA  (baixa ~1 GB da CVM, guarda só a Tarpon)
     python experimentos/tarpon.py PASTA
"""
import sys
import numpy as np, pandas as pd, yfinance as yf
D = sys.argv[1]
MASTER, FIC = "27.389.566/0001-03", "22.232.927/0001-90"
COST = 0.0013

h = pd.read_csv(f"{D}/tarpon_cda.csv", dtype=str)
h["cnpj"] = h["CNPJ_FUNDO_CLASSE"].fillna(h["CNPJ_FUNDO"]) if "CNPJ_FUNDO" in h else h["CNPJ_FUNDO_CLASSE"]
m = h[(h.cnpj == MASTER) & (h.origem == "acoes")].copy()
m["v"] = m.VL_MERC_POS_FINAL.astype(float); m["q"] = m.QT_POS_FINAL.astype(float)
m["date"] = pd.to_datetime(m.DT_COMPTC) + pd.offsets.MonthEnd(0)
val = m.groupby(["date", "CD_ATIVO"]).v.sum().unstack().fillna(0)
qty = m.groupby(["date", "CD_ATIVO"]).q.sum().unstack()
cvm_px = (val / qty).replace([np.inf], np.nan)          # preço implícito no fim do mês
w = val.div(val.sum(axis=1), axis=0)                     # 100% em ações
w = w[w.sum(axis=1) > 0]

tickers = list(w.columns)
yp = yf.download([t + ".SA" for t in tickers] + ["^BVSP"], start="2020-12-01", auto_adjust=True, progress=False)["Close"]
yp.columns = [c.replace(".SA", "") for c in yp.columns]
me = pd.date_range("2021-01-31", "2026-10-31", freq="ME")
px_m = yp.resample("ME").last().reindex(me)
# sem Yahoo (PORT3, RDNI3, SRNA3): preço da CVM; sem nenhum preço no mês, fica parado (saída em dinheiro)
for t in tickers:
    if t not in px_m or px_m[t].isna().all():
        px_m[t] = cvm_px[t].reindex(me) if t in cvm_px else np.nan
ret = px_m[tickers].pct_change(fill_method=None).fillna(0)
ibov = px_m["^BVSP"].pct_change()

q = pd.read_csv(f"{D}/tarpon_quota.csv", dtype=str)
q = q[q.CNPJ == FIC].assign(d=lambda x: pd.to_datetime(x.DT_COMPTC), v=lambda x: x.VL_QUOTA.astype(float))
fic = q.drop_duplicates("d").set_index("d").v.sort_index().resample("ME").last().reindex(me).pct_change()

def replica(lag):
    cur = pd.Series(0.0, index=tickers); out = {}
    for i in range(1, len(me)):
        d0, d1 = me[i - 1], me[i]
        src = d0 - pd.offsets.MonthEnd(lag)           # carteira conhecida no fim de d0
        known = w.loc[:src]
        if len(known):
            tgt = known.iloc[-1].reindex(tickers).fillna(0)
            turnover = (tgt - cur).abs().sum() / 2
            cur = tgt
        else:
            turnover = 0
        if cur.sum() == 0: continue
        r = float((cur * ret.loc[d1]).sum()) - turnover * COST * 2
        out[d1] = r
        cur = cur * (1 + ret.loc[d1]); cur = cur / cur.sum()   # deriva até o próximo rebalanceamento
    return pd.Series(out)

def stats(r):
    r = r.dropna(); v = (1 + r).cumprod()
    yrs = len(r) / 12
    return f"{v.iloc[-1] ** (1 / yrs) - 1:6.1%} a.a. | DD {(v / v.cummax() - 1).min():6.1%} | total {v.iloc[-1] - 1:6.0%}"

for start in ["2021-11-30", "2022-02-28"]:
    win = lambda s: s.loc[start:"2026-09-30"]
    print(f"\n=== {start} a 2026-09-30 (mensal)")
    print("Tarpon GT FIC (cota real, líquida de taxas)", stats(win(fic)))
    print("réplica, atraso 1 mês                     ", stats(win(replica(1))))
    print("réplica, atraso 3 meses                   ", stats(win(replica(3))))
    print("Ibovespa                                  ", stats(win(ibov)))
y = pd.DataFrame({"FIC": fic, "rep1": replica(1), "rep3": replica(3), "Ibov": ibov}).loc["2021-02-28":]
yy = (1 + y).groupby(y.index.year).prod() - 1
print("\nano a ano (%):"); print((yy * 100).round(1).to_string())
