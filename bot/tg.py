"""Telegram Bot API через urllib (стандартная библиотека). Токен живёт только в URL запроса:
в тексты ошибок он не попадает (redact вычищает его на всякий случай)."""
import json, urllib.error, urllib.request

API = "https://api.telegram.org"


class TelegramError(Exception):
    """Ответ Bot API с ok=false или сбой сети. code — HTTP-код (409 — второй экземпляр бота), 0 — сеть."""

    def __init__(self, code, description):
        super().__init__(f"{code}: {description}")
        self.code, self.description = code, description


class Telegram:
    def __init__(self, token, api=API):
        self._token = token
        self._api = api

    def redact(self, s):
        return str(s).replace(self._token, "<token>") if self._token else str(s)

    def call(self, method, http_timeout=15, **params):
        """method(params) → result. TelegramError при ok=false, HTTP-ошибке или сбое сети.
        http_timeout — таймаут HTTP (не путать с параметром timeout у getUpdates)."""
        body = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
        req = urllib.request.Request(f"{self._api}/bot{self._token}/{method}", data=body,
                                     headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=http_timeout) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                data = json.loads(e.read())
            except (ValueError, OSError):
                raise TelegramError(e.code, self.redact(e.reason)) from None
        except (OSError, ValueError) as e:   # URLError, таймаут, обрыв, не JSON
            raise TelegramError(0, self.redact(e)) from None
        if not data.get("ok"):
            raise TelegramError(data.get("error_code", 0), self.redact(data.get("description", "unknown error")))
        return data.get("result")
