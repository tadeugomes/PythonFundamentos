"""Baixa CDA (carteira) e informe diário (cota) da CVM e guarda só as linhas da Tarpon."""
import sys, zipfile, subprocess, os, re
import pandas as pd
D = sys.argv[1]
MASTER, FIC = "27.389.566/0001-03", "22.232.927/0001-90"
BASE = "https://dados.cvm.gov.br/dados/FI/DOC"

def get(url, path):
    for i in range(4):
        r = subprocess.run(["curl", "-sS", "-f", "-m", "600", "-o", path, url])
        if r.returncode == 0:
            return True
    print("falhou", url, flush=True); return False

def cnpj_col(df):
    return "CNPJ_FUNDO_CLASSE" if "CNPJ_FUNDO_CLASSE" in df.columns else "CNPJ_FUNDO"

hold, pls, quotas = [], [], []
cda_files = [f"HIST/cda_fi_{y}.zip" for y in (2021, 2022)] + \
            [f"cda_fi_{y}{m:02d}.zip" for y in (2023, 2024, 2025, 2026) for m in range(1, 13) if (y, m) <= (2026, 9)]
for f in cda_files:
    path = f"{D}/tmp.zip"
    if not get(f"{BASE}/CDA/DADOS/{f}", path): continue
    z = zipfile.ZipFile(path)
    for n in z.namelist():
        kind = "acoes" if re.match(r"cda_fi_BLC_4_\d+\.csv", n) else "conf" if re.match(r"cda_fi_CONFID_\d+\.csv", n) else "pl" if re.match(r"cda_fi_PL_\d+\.csv", n) else None
        if not kind: continue
        df = pd.read_csv(z.open(n), sep=";", encoding="latin1", dtype=str, on_bad_lines="skip")
        df = df[df[cnpj_col(df)].isin([MASTER, FIC])]
        if kind == "pl": pls.append(df)
        else:
            df = df.assign(origem=kind); hold.append(df)
    os.remove(path); print("cda", f, flush=True)
pd.concat(hold).to_csv(f"{D}/tarpon_cda.csv", index=False)
pd.concat(pls).to_csv(f"{D}/tarpon_pl.csv", index=False)

inf = [f"inf_diario_fi_{y}{m:02d}.zip" for y in range(2021, 2027) for m in range(1, 13) if (y, m) <= (2026, 10)]
for f in inf:
    path = f"{D}/tmp.zip"
    if not get(f"{BASE}/INF_DIARIO/DADOS/{f}", path): continue
    z = zipfile.ZipFile(path)
    for n in z.namelist():
        df = pd.read_csv(z.open(n), sep=";", encoding="latin1", dtype=str, on_bad_lines="skip")
        df = df[df[cnpj_col(df)].isin([MASTER, FIC])]
        quotas.append(df.rename(columns={cnpj_col(df): "CNPJ"})[["CNPJ", "DT_COMPTC", "VL_QUOTA", "VL_PATRIM_LIQ"]])
    os.remove(path); print("inf", f, flush=True)
pd.concat(quotas).to_csv(f"{D}/tarpon_quota.csv", index=False)
print("FIM", flush=True)
