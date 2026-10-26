"""Execução de ordens no MetaTrader 5 (XP). Só funciona em Windows com o terminal instalado.

Com dry_run=True nada é enviado; as ordens são apenas impressas e devolvidas.
Para valores pequenos usa o mercado fracionário (ticker + F, 1 a 99 ações);
acima de 100 ações, usa o lote padrão em múltiplos de 100.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass

try:
    import MetaTrader5 as mt5
except ImportError:  # fora do Windows
    mt5 = None


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


def _symbol_for(ticker: str, qty: int, use_fractional: bool) -> tuple[str, int]:
    if use_fractional and qty < 100:
        return f"{ticker}F", qty
    return ticker, (qty // 100) * 100


def quantity_for(ticker: str, amount: float, price: float, use_fractional: bool) -> tuple[str, int]:
    qty = math.floor(amount / price)
    return _symbol_for(ticker, qty, use_fractional)


def last_price(ticker: str) -> float | None:
    if mt5 is None:
        return None
    mt5.symbol_select(ticker, True)
    tick = mt5.symbol_info_tick(ticker)
    return tick.last if tick else None


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
    return Order(symbol, side, qty, res.price if ok else price, sent=ok, comment=str(res.comment if res else ""))


def positions() -> dict[str, float]:
    if mt5 is None:
        return {}
    out: dict[str, float] = {}
    for p in mt5.positions_get() or []:
        out[p.symbol] = out.get(p.symbol, 0) + p.volume
    return out
