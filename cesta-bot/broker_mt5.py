"""Execução de ordens no MetaTrader 5 (XP). Só funciona em Windows com o terminal instalado.

Com dry_run=True nada é enviado; as ordens são apenas impressas e devolvidas.
Uma quantidade é dividida em lote padrão (múltiplos de 100, no ticker) e
fracionário (resto de 1 a 99, no ticker + F). Sem fracionário, o resto é descartado.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

try:
    import MetaTrader5 as mt5
except ImportError:  # fora do Windows
    mt5 = None

# janela segura do pregão regular da B3 (horário de Brasília), longe da abertura e do leilão
SESSION_START = time(10, 5)
SESSION_END = time(16, 50)


@dataclass
class Order:
    symbol: str
    side: str
    qty: int
    price: float
    sent: bool
    comment: str = ""


def connect(cfg: dict) -> bool:
    if mt5 is None:
        raise RuntimeError("pacote MetaTrader5 indisponível (instale no Windows: pip install MetaTrader5)")
    m = cfg["mt5"]
    ok = mt5.initialize(
        path=m.get("terminal_path"),
        login=int(os.environ[m["login_env"]]),
        password=os.environ[m["password_env"]],
        server=m["server"],
    )
    if not ok:
        raise RuntimeError(f"mt5.initialize falhou: {mt5.last_error()}")
    return True


def shutdown():
    if mt5 is not None:
        mt5.shutdown()


def market_is_open(now: datetime | None = None) -> bool:
    now = now or datetime.now(ZoneInfo("America/Sao_Paulo"))
    return now.weekday() < 5 and SESSION_START <= now.time() <= SESSION_END


def split_qty(ticker: str, qty: int, use_fractional: bool) -> list[tuple[str, int]]:
    """Divide em [(ticker, lote múltiplo de 100), (tickerF, resto)]."""
    lots = (qty // 100) * 100
    rest = qty - lots
    legs = []
    if lots > 0:
        legs.append((ticker, lots))
    if rest > 0 and use_fractional:
        legs.append((f"{ticker}F", rest))
    return legs


def quantity_for(amount: float, price: float) -> int:
    return math.floor(amount / price) if price > 0 else 0


def last_price(symbol: str) -> float | None:
    if mt5 is None:
        return None
    mt5.symbol_select(symbol, True)
    tick = mt5.symbol_info_tick(symbol)
    return tick.last if tick and tick.last > 0 else None


def market_order(symbol: str, side: str, qty: int, dry_run: bool = True, comment: str = "cesta-bot") -> Order:
    price = last_price(symbol) or 0.0
    if dry_run or mt5 is None or qty <= 0:
        print(f"[dry-run] {side} {qty} {symbol} @ {price}")
        return Order(symbol, side, qty, price, sent=False, comment="dry-run")
    mt5.symbol_select(symbol, True)
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(qty),
        "type": mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL,
        "price": price,
        "type_time": mt5.ORDER_TIME_DAY,
        "type_filling": mt5.ORDER_FILLING_RETURN,  # padrão na B3
        "comment": comment,
    }
    res = mt5.order_send(req)
    ok = res is not None and res.retcode == mt5.TRADE_RETCODE_DONE
    if not ok:
        print(f"[mt5] ordem rejeitada {symbol}: {res.retcode if res else None} {res.comment if res else mt5.last_error()}")
        return Order(symbol, side, qty, price, sent=False, comment=str(res.comment if res else mt5.last_error()))
    return Order(symbol, side, int(res.volume), res.price, sent=True, comment=str(res.comment))


def positions() -> dict[str, float]:
    if mt5 is None:
        return {}
    out: dict[str, float] = {}
    for p in mt5.positions_get() or []:
        out[p.symbol] = out.get(p.symbol, 0) + p.volume
    return out


def position_legs(ticker: str) -> list[tuple[str, int]]:
    """Posição real no MT5 por símbolo: [(ticker, qtd), (tickerF, qtd)]."""
    pos = positions()
    return [(s, int(pos[s])) for s in (ticker, f"{ticker}F") if pos.get(s, 0) > 0]


def buy(ticker: str, qty: int, use_fractional: bool, dry_run: bool) -> list[Order]:
    return [market_order(s, "BUY", q, dry_run) for s, q in split_qty(ticker, qty, use_fractional)]


def sell(ticker: str, qty: int | None, use_fractional: bool, dry_run: bool, fallback_qty: int = 0) -> list[Order]:
    """Vende `qty` ações (None = posição inteira) usando a posição real de cada símbolo.

    Em parcial, consome primeiro o fracionário e depois lotes de 100.
    Sem posição no MT5 (dry-run fora do Windows), usa `fallback_qty` da carteira registrada.
    """
    legs = position_legs(ticker) if mt5 is not None else []
    if not legs:
        total = fallback_qty if qty is None else min(qty, fallback_qty)
        return [market_order(s, "SELL", q, dry_run) for s, q in split_qty(ticker, total, use_fractional)]
    if qty is None:
        return [market_order(s, "SELL", q, dry_run) for s, q in legs]
    orders, left = [], qty
    for s, have in sorted(legs, key=lambda x: not x[0].endswith("F")):
        q = min(have, left) if s.endswith("F") else (min(have, left) // 100) * 100
        if q > 0:
            orders.append(market_order(s, "SELL", q, dry_run))
            left -= q
    return orders
