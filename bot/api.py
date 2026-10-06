"""Клиент API сайта CrawlScan: POST /api/scan → job, GET /api/result?job= → результат.
Бот сам в блокчейн не ходит. Кэш (10 минут) и очередь сканов — на стороне сайта."""
import json, urllib.error, urllib.parse, urllib.request

DEFAULT = "https://crawlscan.fun"


class ApiError(Exception):
    """Сбой сети или неожиданный ответ сайта (не показываем пользователю как есть)."""


class Rejected(Exception):
    """Сайт отклонил адрес (400): текст для пользователя — "not a token address" и т.п."""


class CrawlScan:
    def __init__(self, base=DEFAULT, timeout=15):
        self.base = base.rstrip("/")
        self.timeout = timeout

    def _req(self, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method="POST" if data is not None else "GET",
                                     headers={"content-type": "application/json", "user-agent": "crawlscan-bot"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read()).get("error")
            except (ValueError, OSError, AttributeError):
                err = None
            if e.code == 400 and err:
                raise Rejected(err) from None
            raise ApiError(f"HTTP {e.code} {path}") from None
        except (OSError, ValueError) as e:
            raise ApiError(f"{path}: {e}") from None

    def scan(self, token):
        """Запустить скан (или получить кэш сайта) → job id."""
        job = self._req("/api/scan", {"token": token}).get("job")
        if not job:
            raise ApiError("no job in /api/scan response")
        return job

    def result(self, job):
        """{"done": False} | {"done": True, "result": {...}} | {"done": True, "error": "..."}."""
        return self._req("/api/result?job=" + urllib.parse.quote(job))
