"""Клиент API сайта CrawlScan: POST /api/scan → job, GET /api/result?job= → результат,
GET /api/rewards/status → награды и сжигания (/rewards), GET /api/config → включены ли алерты,
/api/alerts/* → подписки, /api/premium/* → Premium (заголовок X-Alerts-Secret).
Бот сам в блокчейн не ходит. Кэш (10 минут) и очередь сканов — на стороне сайта."""
import json, urllib.error, urllib.parse, urllib.request

DEFAULT = "https://crawlscan.fun"


class ApiError(Exception):
    """Сбой сети или неожиданный ответ сайта (не показываем пользователю как есть)."""


class Busy(ApiError):
    """Сайт перегружен (503 busy) или лимит частоты (429): новый скан не принят, повторить позже."""


class TooFast(Busy):
    """Лимит новых сканов на пользователя бота (429 user_rate_limited) или Fresh scan (429 fresh_limited):
    args[0] — текст сайта для пользователя, upsell — показать ли под ним [⭐ Premium features]."""

    def __init__(self, message, upsell=False):
        super().__init__(message)
        self.upsell = upsell


class Rejected(Exception):
    """Сайт отклонил адрес (400): текст для пользователя — "not a token address" и т.п."""


class AlertsOff(Exception):
    """Алерты на сайте выключены (404 на /api/alerts/*)."""


class PremiumOff(Exception):
    """Premium на сайте выключен (404 на /api/premium/*)."""


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
            if e.code == 429 and err in ("user_rate_limited", "fresh_limited"):
                raise TooFast(payload.get("message") or err, bool(payload.get("upsell"))) from None
            if (e.code == 503 and err == "busy") or e.code == 429:
                raise Busy(payload.get("message") or err) from None
            if e.code in ok and isinstance(payload, dict):
                return {"status": e.code} | payload
            if e.code == 404 and path.startswith("/api/alerts/"):
                raise AlertsOff() from None
            if e.code == 404 and path.startswith("/api/premium/"):
                raise PremiumOff() from None
            raise ApiError(f"HTTP {e.code} {path}") from None
        except (OSError, ValueError) as e:
            raise ApiError(f"{path}: {e}") from None

    def scan(self, token, user_id=None, fresh=False):
        """Запустить скан (или получить кэш сайта) → job id. С секретом алертов сайт не ограничивает частоту сканов
        бота (лимит новых сканов по IP — для остальных). user_id (Telegram ID, только с секретом) — сайт сам
        проверяет премиум и при PREMIUM_PRIORITY ставит скан первым в очередь ожидания, считает лимит новых сканов
        на пользователя (PREMIUM_BOT_RATE). fresh — Fresh scan премиума (мимо кэша результата сайта)."""
        headers = {"X-Alerts-Secret": self.alerts_secret} if self.alerts_secret else None
        body = ({"token": token} | ({"user_id": user_id} if user_id is not None and self.alerts_secret else {})
                | ({"fresh": True} if fresh else {}))
        job = self._req("/api/scan", body, headers=headers).get("job")
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

    def _premium(self, path, body=None):
        return self._req(path, body, headers={"X-Alerts-Secret": self.alerts_secret}, ok=(409, 429))

    def premium_reserve(self, user_id, wallet):
        """Бронь кошелька: {"state": reserved|pending|yours, "wallet", "expires_at", "minutes"} |
        {"status": 409, "error": "taken"} | {"status": 429}. Не адрес — Rejected, выключено — PremiumOff."""
        return self._premium("/api/premium/reserve", {"user_id": user_id, "wallet": wallet})

    def premium_status(self, user_id):
        return self._premium("/api/premium/status?user_id=" + urllib.parse.quote(str(user_id)))

    def premium_unlink(self, user_id):
        return self._premium("/api/premium/unlink", {"user_id": user_id})

    def premium_admin_unlink(self, wallet):
        return self._premium("/api/premium/admin_unlink", {"wallet": wallet})

    def premium_import(self, user_id):
        """Импорт из кошелька: {"items": [{"token", "ticker", "launchpad", "value_usd", "status", "watching"}],
        "watch_count", "limit", "cached"} | {"status": 403 (не премиум) | 429 ("retry_in")}."""
        return self._req("/api/premium/import", {"user_id": user_id}, headers={"X-Alerts-Secret": self.alerts_secret},
                         ok=(403, 429))

    def premium_import_add(self, user_id, tokens):
        """tokens — список адресов или "all": {"added", "already", "full", "skipped", "watch_count", "limit"} |
        {"status": 410} (просмотр устарел) | {"status": 403}."""
        return self._req("/api/premium/import_add", {"user_id": user_id, "tokens": tokens},
                         headers={"X-Alerts-Secret": self.alerts_secret}, ok=(403, 410))

    def premium_digest(self, user_id, on=None):
        """/digest: {"digest": bool, "hour", "premium"}; on=None — только узнать."""
        return self._premium("/api/premium/digest", {"user_id": user_id} | ({} if on is None else {"on": on}))

    def _extra(self, path, body):
        return self._req(path, body, headers={"X-Alerts-Secret": self.alerts_secret}, ok=(403,))

    def premium_dev(self, user_id, token):
        """История дева: {"known", "dev", "total", "in_replay", "tokens"} | {"status": 403}."""
        return self._extra("/api/premium/dev", {"user_id": user_id, "token": token})

    def premium_memory(self, user_id, token):
        """Блок Memory: {"notable", "holders", "repeat", "snipers", "clusters"} | {"status": 403}."""
        return self._extra("/api/premium/memory", {"user_id": user_id, "token": token})

    def premium_trending(self, user_id):
        """Trending: {"items": [{"token", "chain", "scans", "band", "score", "ticker"}]} | {"status": 403}."""
        return self._extra("/api/premium/trending", {"user_id": user_id})
