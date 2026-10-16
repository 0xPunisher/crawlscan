"""Telegram Bot API via urllib (standard library). The token lives only in the request URL:
it never gets into error texts (redact scrubs it just in case)."""
import json, os, urllib.error, urllib.request, uuid

API = "https://api.telegram.org"


class TelegramError(Exception):
    """Bot API response with ok=false, or a network failure. code is the HTTP code (409 = a second bot instance), 0 = network.
    retry_after is seconds from the parameters of a 429 response."""

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
        """method(params) → result. TelegramError on ok=false, an HTTP error or a network failure.
        http_timeout is the HTTP timeout (not to be confused with the getUpdates timeout parameter)."""
        body = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
        return self._post(method, body, "application/json", http_timeout)

    def upload(self, method, field, path, http_timeout=60, **params):
        """method with the file path in field field (multipart/form-data), e.g. sendPhoto photo=<file>.
        Other params are strings; dicts and lists (reply_markup) are sent as JSON."""
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
        except (OSError, ValueError) as e:   # URLError, timeout, dropped connection, not JSON
            raise TelegramError(0, self.redact(e)) from None
        if not data.get("ok"):
            raise TelegramError(data.get("error_code", 0), self.redact(data.get("description", "unknown error")),
                                (data.get("parameters") or {}).get("retry_after"))
        return data.get("result")
