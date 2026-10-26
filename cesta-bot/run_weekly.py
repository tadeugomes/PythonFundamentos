"""Rodada semanal: baixa dados, decide, executa (paper/telegram/mt5) e notifica.

Com strategy.check ou strategy.refill em "daily", agende todo dia útil: a sexta faz a
decisão completa e os outros dias só checam saídas e/ou preenchem vagas abertas.

Sinais (paper/telegram): agende para sexta-feira após 18h.
Ordens (mt5): agende para segunda entre 10:05 e 16:50; o sinal usa o fechamento de sexta.
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


def filled(orders: list) -> tuple[float, float | None]:
    """Quantidade executada e preço médio. Em dry-run conta a quantidade simulada."""
    done = [o for o in orders if o.sent or o.comment == "dry-run"]
    qty = sum(o.qty for o in done)
    priced = [o for o in done if o.price > 0]
    avg = sum(o.qty * o.price for o in priced) / sum(o.qty for o in priced) if priced else None
    return qty, avg


def fmt_message(d, px, holdings, cfg, mode: str, executed: list, resized: dict, not_filled: list,
                weekly: bool = True) -> str:
    title = "Cesta semanal" if weekly else "Cesta · checagem diária"
    lines = [f"<b>{title}</b> · {date.today():%d/%m/%Y} · modo {mode} · dados de {px.name:%d/%m}"]
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
    if resized:
        lines.append(f"\n<b>AJUSTAR</b> (regime mudou para {d.exposure:.0%})")
        for t, dq in resized.items():
            lines.append(f"• {t} {'+' if dq > 0 else ''}{dq:.2f} ações")
    if d.hold:
        lines.append("\n<b>MANTER</b> " + ", ".join(
            f"{t} ({px[t]/holdings[t].entry_price-1:+.1%})" for t in d.hold))
    if executed:
        lines.append("\n<b>Ordens</b>")
        for o in executed:
            lines.append(f"• {o.side} {o.qty} {o.symbol} @ {o.price:.2f} {'enviada' if o.sent else '(não enviada)'}")
    if not_filled:
        lines.append("\n<b>Não executadas</b> (carteira registrada não mudou): " + ", ".join(not_filled))
    if d.scores is not None and len(d.scores):
        top = d.scores.head(10)
        lines.append("\nTop momentum: " + ", ".join(f"{t} {v:+.0%}" for t, v in top.items()))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["paper", "telegram", "mt5"])
    ap.add_argument("--no-fundamentals", action="store_true")
    ap.add_argument("--force", action="store_true", help="roda mesmo se a carteira já usou estes dados")
    args = ap.parse_args()
    cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    ex = cfg["execution"]
    mode = args.mode or ex["mode"]
    dry_run, frac = ex["dry_run"], ex["use_fractional"]
    state_path = Path(ex["state_file"])
    state = load_state(state_path)
    holdings = to_positions(state["holdings"])
    prev_exposure = state.get("exposure")

    if mode == "mt5" and not dry_run:
        import broker_mt5 as br
        if not br.market_is_open():
            raise SystemExit(f"pregão fechado: ordens a mercado só entre {br.SESSION_START:%H:%M} e "
                             f"{br.SESSION_END:%H:%M} em dia útil. Nada foi enviado nem registrado.")

    start = (pd.Timestamp.today() - pd.DateOffset(months=18)).strftime("%Y-%m-%d")
    tickers = cfg["universe"]
    # sem cache: a janela de 18 meses não pode sobrescrever o histórico longo do backtest
    prices, volume = load_prices(tickers + [cfg["benchmark"]], start, refresh=True, cache=False,
                                 extra=("Volume",))
    bench = prices[cfg["benchmark"]].dropna()
    prices, volume = prices[tickers], volume[tickers]
    # tamanho: volume financeiro médio de 252 pregões (size_top e weighting: liquidity)
    size = (prices * volume).rolling(252, min_periods=126).mean().iloc[-1]
    asof = str(prices.index[-1].date())
    if state.get("prices_asof") == asof and not args.force:
        # repetir a rodada contaria mais uma semana de posse e duplicaria o histórico
        raise SystemExit(f"carteira já processada com dados de {asof}. Use --force para rodar de novo.")
    fund = None if args.no_fundamentals else load_fundamentals(tickers)

    st = cfg["strategy"]
    daily_check = st.get("check", "weekly") == "daily"
    daily_refill = st.get("refill", "weekly") == "daily"
    asof_ts = prices.index[-1]
    last_weekly = state.get("last_weekly")
    # com checagem diária, a decisão completa (ranking, regime, ajuste) só roda na sexta;
    # se a sexta foi feriado, roda no primeiro pregão depois de 7 dias sem decisão completa
    weekly = (not (daily_check or daily_refill) or asof_ts.weekday() == 4 or last_weekly is None
              or (asof_ts - pd.Timestamp(last_weekly)).days >= 7)
    if weekly:
        d = decide(prices, bench, holdings, cfg, fund, size=size)
    else:
        d = decide(prices, bench, holdings, cfg, fund, exits=daily_check, rank_exit=False, size=size)
        if prev_exposure is not None:
            d.exposure = prev_exposure  # regime só muda na decisão semanal
        if not daily_refill:
            d.buy = []
    px = prices.iloc[-1]
    # capital_per_position vale para pesos iguais (1/n); com pesos por liquidez, escala pelo peso
    n_pos = st["n_positions"]

    def cap(t):
        return ex["capital_per_position"] * n_pos * d.weights.get(t, 1 / n_pos) * d.exposure
    resize = (weekly and st.get("resize_on_regime", True)
              and prev_exposure is not None and d.exposure != prev_exposure)

    # quantidades executadas por ticker; paper usa frações, mt5 usa o que a corretora confirmou
    sold: dict[str, float] = {}
    resized: dict[str, float] = {}
    bought: dict[str, tuple[float, float]] = {}
    executed = []

    if mode == "mt5":
        import broker_mt5 as br
        br.connect(cfg)
        try:
            for t in d.sell:
                orders = br.sell(t, None, frac, dry_run, fallback_qty=int(holdings[t].qty))
                executed += orders
                sold[t] = min(filled(orders)[0], holdings[t].qty)
            if resize:
                for t in d.hold:
                    price = br.last_price(t) or px[t]
                    delta = br.quantity_for(cap(t), price) - int(holdings[t].qty)
                    if delta < 0:
                        orders = br.sell(t, -delta, frac, dry_run, fallback_qty=int(holdings[t].qty))
                        resized[t] = -filled(orders)[0]
                    elif delta > 0:
                        orders = br.buy(t, delta, frac, dry_run)
                        resized[t] = filled(orders)[0]
                    else:
                        continue
                    executed += orders
            for t in d.buy:
                price = br.last_price(t) or px[t]
                orders = br.buy(t, br.quantity_for(cap(t), price), frac, dry_run)
                executed += orders
                qty, avg = filled(orders)
                bought[t] = (qty, avg or float(px[t]))
        finally:
            br.shutdown()
    else:
        sold = {t: holdings[t].qty for t in d.sell}
        if resize:
            resized = {t: cap(t) / px[t] - holdings[t].qty for t in d.hold}
        bought = {t: (cap(t) / px[t], float(px[t])) for t in d.buy}

    holdings_before = {t: Position(**h.__dict__) for t, h in holdings.items()}
    not_filled = []
    # atualiza carteira registrada (paper e real usam o mesmo estado)
    for t in d.sell:
        h = holdings[t]
        q = sold.get(t, 0)
        if q <= 0:
            not_filled.append(f"venda {t}")
            continue
        h.qty -= q
        state["history"].append({"date": str(date.today()), "ticker": t, "side": "SELL", "qty": float(q),
                                 "price": float(px[t]), "ret": float(px[t] / h.entry_price - 1),
                                 "reason": d.reasons.get(t)})
        if h.qty < 1e-9:
            holdings.pop(t)
        else:
            not_filled.append(f"venda parcial {t} (restam {h.qty:g})")
    for t, dq in resized.items():
        holdings[t].qty += dq
        state["history"].append({"date": str(date.today()), "ticker": t, "side": "ADD" if dq > 0 else "TRIM",
                                 "qty": float(abs(dq)), "price": float(px[t]),
                                 "reason": f"regime {d.exposure:.0%}"})
    for t in d.buy:
        qty, price = bought.get(t, (0, None))
        if qty <= 0:
            not_filled.append(f"compra {t}")
            continue
        holdings[t] = Position(entry_price=float(price), peak=float(price), qty=float(qty))
        state["history"].append({"date": str(date.today()), "ticker": t, "side": "BUY", "qty": float(qty),
                                 "price": float(price), "reason": d.reasons.get(t)})
    if weekly:
        for h in holdings.values():
            h.weeks_held += 1
        state["last_weekly"] = asof
    state["holdings"] = {t: h.__dict__ for t, h in holdings.items()}
    state["exposure"] = d.exposure
    state["last_run"] = str(date.today())
    state["prices_asof"] = asof
    state["equity_paper"] = float(sum(h.qty * px[t] for t, h in holdings.items()))
    save_state(state_path, state)

    msg = fmt_message(d, px, holdings_before, cfg, mode, executed, resized, not_filled, weekly)
    print(msg.replace("<b>", "").replace("</b>", ""))
    if mode in ("telegram", "mt5"):
        send_telegram(msg, cfg)


if __name__ == "__main__":
    main()
