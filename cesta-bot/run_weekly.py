"""Rodada semanal: baixa dados, decide, executa (paper/telegram/mt5) e notifica.

Agende para sexta-feira após 18h ou segunda antes da abertura.
  python run_weekly.py               # usa execution.mode do config
  python run_weekly.py --mode paper  # força modo
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

import pandas as pd
import yaml

from data import load_fundamentals, load_prices
from notify import send_telegram
from strategy import Position, decide


def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"holdings": {}, "history": []}


def save_state(path: Path, state: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def to_positions(raw: dict) -> dict[str, Position]:
    return {t: Position(**{k: v for k, v in h.items() if k in Position.__dataclass_fields__}) for t, h in raw.items()}


def fmt_message(d, px, holdings, cfg, mode: str, executed: list) -> str:
    lines = [f"<b>Cesta semanal</b> · {date.today():%d/%m/%Y} · modo {mode}"]
    lines.append(f"Exposição alvo: {d.exposure:.0%}" + ("  (regime desfavorável)" if d.exposure < 1 else ""))
    if d.sell:
        lines.append("\n<b>VENDER</b>")
        for t in d.sell:
            h = holdings[t]
            lines.append(f"• {t} @ R$ {px[t]:.2f}  ({px[t]/h.entry_price-1:+.1%})  {d.reasons.get(t,'')}")
    if d.buy:
        lines.append("\n<b>COMPRAR</b>")
        for t in d.buy:
            lines.append(f"• {t} @ R$ {px[t]:.2f}  {d.reasons.get(t,'')}")
    if d.hold:
        lines.append("\n<b>MANTER</b> " + ", ".join(
            f"{t} ({px[t]/holdings[t].entry_price-1:+.1%})" for t in d.hold))
    if executed:
        lines.append("\n<b>Ordens</b>")
        for o in executed:
            lines.append(f"• {o.side} {o.qty} {o.symbol} @ {o.price:.2f} {'enviada' if o.sent else '(não enviada)'}")
    if d.scores is not None and len(d.scores):
        top = d.scores.head(10)
        lines.append("\nTop momentum: " + ", ".join(f"{t} {v:+.0%}" for t, v in top.items()))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["paper", "telegram", "mt5"])
    ap.add_argument("--no-fundamentals", action="store_true")
    args = ap.parse_args()
    cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    ex = cfg["execution"]
    mode = args.mode or ex["mode"]
    state_path = Path(ex["state_file"])
    state = load_state(state_path)
    holdings = to_positions(state["holdings"])

    start = (pd.Timestamp.today() - pd.DateOffset(months=18)).strftime("%Y-%m-%d")
    tickers = cfg["universe"]
    prices = load_prices(tickers + [cfg["benchmark"]], start, refresh=True)
    bench = prices[cfg["benchmark"]].dropna()
    prices = prices[tickers]
    fund = None if args.no_fundamentals else load_fundamentals(tickers)

    d = decide(prices, bench, holdings, cfg, fund)
    px = prices.iloc[-1]
    executed = []

    if mode == "mt5":
        import broker_mt5 as br
        br.connect(cfg)
        try:
            for t in d.sell:
                qty = int(holdings[t].qty)
                sym, q = br._symbol_for(t, qty, ex["use_fractional"])
                executed.append(br.market_order(sym, "SELL", q, dry_run=ex["dry_run"]))
            for t in d.buy:
                price = br.last_price(t) or px[t]
                sym, q = br.quantity_for(t, ex["capital_per_position"] * d.exposure, price, ex["use_fractional"])
                executed.append(br.market_order(sym, "BUY", q, dry_run=ex["dry_run"]))
        finally:
            br.shutdown()

    holdings_before = dict(holdings)
    # atualiza carteira registrada (paper e real usam o mesmo estado)
    for t in d.sell:
        h = holdings.pop(t)
        state["history"].append({"date": str(date.today()), "ticker": t, "side": "SELL",
                                 "price": float(px[t]), "ret": float(px[t] / h.entry_price - 1),
                                 "reason": d.reasons.get(t)})
    for t in d.buy:
        qty = (ex["capital_per_position"] * d.exposure) / px[t]
        holdings[t] = Position(entry_price=float(px[t]), peak=float(px[t]), qty=float(qty))
        state["history"].append({"date": str(date.today()), "ticker": t, "side": "BUY",
                                 "price": float(px[t]), "reason": d.reasons.get(t)})
    for h in holdings.values():
        h.weeks_held += 1
    state["holdings"] = {t: h.__dict__ for t, h in holdings.items()}
    state["last_run"] = str(date.today())
    state["equity_paper"] = float(sum(h.qty * px[t] for t, h in holdings.items()))
    save_state(state_path, state)

    msg = fmt_message(d, px, holdings_before, cfg, mode, executed)
    print(msg.replace("<b>", "").replace("</b>", ""))
    if mode in ("telegram", "mt5"):
        send_telegram(msg, cfg)


if __name__ == "__main__":
    main()
