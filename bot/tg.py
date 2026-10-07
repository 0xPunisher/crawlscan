"""Telegram Bot API через urllib (стандартная библиотека). Токен живёт только в URL запроса:
в тексты ошибок он не попадает (redact вычищает его на всякий случай)."""
import json, os, urllib.error, urllib.request, uuid

API = "https://api.telegram.org"


class TelegramError(Exception):
    """Ответ Bot API с ok=false или сбой сети. code — HTTP-код (409 — второй экземпляр бота), 0 — сеть.
    retry_after — секунды из parameters ответа 429."""

    def __init__(self, code, description, retry_after=None):
        super().__init__(f"{code}: {description}")
        self.code, self.description, self.retry_after = code, description, retry_after


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
        return self._post(method, body, "application/json", http_timeout)

    def upload(self, method, field, path, http_timeout=60, **params):
        """method с файлом path в поле field (multipart/form-data), напр. sendPhoto photo=<файл>.
        Остальные параметры — строки, словари и списки (reply_markup) — JSON."""
        boundary = uuid.uuid4().hex
        parts = []
        for k, v in params.items():
            if v is None:
                continue
            v = v if isinstance(v, str) else json.dumps(v)
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        with open(path, "rb") as f:
            data = f.read()
        name = os.path.basename(path)
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{name}"\r\n'
                     f"Content-Type: application/octet-stream\r\n\r\n".encode() + data + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        return self._post(method, b"".join(parts), f"multipart/form-data; boundary={boundary}", http_timeout)

    def _post(self, method, body, ctype, http_timeout):
        req = urllib.request.Request(f"{self._api}/bot{self._token}/{method}", data=body,
                                     headers={"content-type": ctype})
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
            raise TelegramError(data.get("error_code", 0), self.redact(data.get("description", "unknown error")),
                                (data.get("parameters") or {}).get("retry_after"))
        return data.get("result")
