"""Envio de mensagens pelo Telegram (Bot API)."""
from __future__ import annotations

import os

import requests


def send_telegram(text: str, cfg: dict) -> bool:
    token = os.environ.get(cfg["telegram"]["token_env"])
    chat_id = os.environ.get(cfg["telegram"]["chat_id_env"])
    if not token or not chat_id:
        print("[telegram] TELEGRAM_TOKEN/TELEGRAM_CHAT_ID não definidos; mensagem abaixo:\n" + text)
        return False
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=30,
    )
    if not r.ok:
        print("[telegram] erro:", r.text)
    return r.ok
