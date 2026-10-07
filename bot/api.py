"""Клиент API сайта CrawlScan: POST /api/scan → job, GET /api/result?job= → результат,
GET /api/rewards/status → награды и сжигания (/rewards), GET /api/config → включены ли алерты,
/api/alerts/* → подписки (заголовок X-Alerts-Secret).
Бот сам в блокчейн не ходит. Кэш (10 минут) и очередь сканов — на стороне сайта."""
import json, urllib.error, urllib.parse, urllib.request

DEFAULT = "https://crawlscan.fun"


class ApiError(Exception):
    """Сбой сети или неожиданный ответ сайта (не показываем пользователю как есть)."""


class Rejected(Exception):
    """Сайт отклонил адрес (400): текст для пользователя — "not a token address" и т.п."""


class AlertsOff(Exception):
    """Алерты на сайте выключены (404 на /api/alerts/*)."""


class CrawlScan:
    def __init__(self, base=DEFAULT, timeout=15, alerts_secret=""):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.alerts_secret = alerts_secret   # ALERTS_API_SECRET: только в заголовке, нигде не логируется

    def _req(self, path, body=None, headers=None, ok=()):
        """JSON-ответ сайта. ok — коды ошибок, тело которых вернуть как ответ (409, 422 у подписок)."""
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method="POST" if data is not None else "GET",
                                     headers={"content-type": "application/json", "user-agent": "crawlscan-bot"}
                                     | (headers or {}))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read())
                err = payload.get("error")
            except (ValueError, OSError, AttributeError):
                payload, err = None, None
            if e.code == 400 and err:
                raise Rejected(err) from None
            if e.code in ok and isinstance(payload, dict):
                return {"status": e.code} | payload
            if e.code == 404 and path.startswith("/api/alerts/"):
                raise AlertsOff() from None
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

    def rewards_status(self):
        """Статус Rewards & Burns сайта: {"enabled": False} или полный статус (см. rewards_service.status_json)."""
        return self._req("/api/rewards/status")

    def config(self):
        """{"solana", "alerts", "trade"} сайта."""
        return self._req("/api/config")

    def _alerts(self, path, body=None):
        return self._req(path, body, headers={"X-Alerts-Secret": self.alerts_secret}, ok=(409, 422))

    def watch(self, chat_id, token):
        """Подписка: 200 → {"ok", "renewed", "token", "expires_at", "items", "limit", "days"};
        {"status": 409, "error": "watch limit", "items", ...} | {"status": 422, "error": "too established"}.
        Плохой адрес — Rejected, алерты выключены — AlertsOff."""
        return self._alerts("/api/alerts/watch", {"chat_id": chat_id, "token": token})

    def unwatch(self, chat_id, token):
        return self._alerts("/api/alerts/unwatch", {"chat_id": chat_id, "token": token})

    def watch_list(self, chat_id):
        return self._alerts("/api/alerts/list?chat_id=" + urllib.parse.quote(str(chat_id)))
