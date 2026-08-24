"""Notificação: log local (console + arquivo) e Telegram (opcional)."""

import json
import logging
import os
import urllib.request

_EMOJI = {
    "info": "ℹ️",
    "ok": "✅",
    "warn": "⚠️",
    "error": "❌",
}


class Notifier:
    def __init__(self, telegram_token=None, telegram_chat_id=None,
                 log_file=None, log_level=logging.INFO, bus=None):
        self.tg_token = telegram_token
        self.tg_chat = telegram_chat_id
        self.bus = bus      # EventBus opcional (portal)
        self.logger = logging.getLogger("a10flash")

        if log_file:
            os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
            handler = logging.FileHandler(log_file, encoding="utf-8")
        else:
            handler = logging.NullHandler()
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s"))
        self.logger.addHandler(handler)
        self.logger.setLevel(log_level)
        # espelha também no console
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        self.logger.addHandler(console)

    # ------------------------------------------------------------ base
    def _send(self, level, device, message, emoji=None):
        """`level` é o nível do logging; `emoji` é o nível SEMÂNTICO
        (info|ok|warn|error) — o dashboard (index.html) estiliza por ele
        e o Telegram usa o emoji correspondente."""
        line = f"[{device}] {message}" if device else message
        getattr(self.logger, level)(line)
        if self.bus is not None:
            self.bus.publish({"type": "log", "level": emoji or level,
                              "device": device, "message": message})
        if self.tg_token and self.tg_chat:
            self._telegram(line, emoji or level)

    def info(self, device, message):
        self._send("info", device, message)

    def ok(self, device, message):
        self._send("info", device, f"{message}", emoji="ok")

    def warn(self, device, message):
        self._send("warning", device, message, emoji="warn")

    def error(self, device, message):
        self._send("error", device, message)

    # ---------------------------------------------------------- telegram
    def _telegram(self, text, emoji_key):
        emoji = _EMOJI.get(emoji_key, "")
        payload = json.dumps({
            "chat_id": self.tg_chat,
            "text": f"{emoji} {text}",
            "disable_web_page_preview": True,
        }).encode("utf-8")
        url = (f"https://api.telegram.org/bot{self.tg_token}"
               f"/sendMessage")
        req = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode("utf-8", "replace")
            if '"ok":false' in body:
                self.logger.warning("Telegram recusou a mensagem: %s",
                                    body[:200])
        except Exception as exc:  # Telegram nunca deve derrubar o fluxo
            self.logger.warning("Falha ao enviar Telegram: %s", exc)
