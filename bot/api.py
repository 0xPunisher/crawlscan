"""CrawlScan site API client: POST /api/scan → job, GET /api/result?job= → result,
GET /api/rewards/status → rewards and burns (/rewards), GET /api/config → whether alerts are enabled,
/api/alerts/* → subscriptions (X-Alerts-Secret header).
The bot never talks to the blockchain itself. Cache (10 minutes) and the scan queue live on the site side."""
import json, urllib.error, urllib.parse, urllib.request

DEFAULT = "https://crawlscan.fun"


class ApiError(Exception):
    """Network failure or unexpected site response (not shown to the user as is)."""


class Busy(ApiError):
    """Site overloaded (503 busy) or rate limited (429): new scan not accepted, retry later."""


class Rejected(Exception):
    """Site rejected the address (400): user-facing text such as "not a token address"."""


class AlertsOff(Exception):
    """Alerts are disabled on the site (404 on /api/alerts/*)."""


class CrawlScan:
    def __init__(self, base=DEFAULT, timeout=15, alerts_secret=""):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.alerts_secret = alerts_secret   # ALERTS_API_SECRET: only in the header, never logged

    def _req(self, path, body=None, headers=None, ok=()):
        """Site JSON response. ok: error codes whose body is returned as the response (409, 422 for subscriptions)."""
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
            if (e.code == 503 and err == "busy") or e.code == 429:
                raise Busy(payload.get("message") or err) from None
            if e.code in ok and isinstance(payload, dict):
                return {"status": e.code} | payload
            if e.code == 404 and path.startswith("/api/alerts/"):
                raise AlertsOff() from None
            raise ApiError(f"HTTP {e.code} {path}") from None
        except (OSError, ValueError) as e:
            raise ApiError(f"{path}: {e}") from None

    def scan(self, token):
        """Start a scan (or get the site cache) → job id. With the alerts secret the site does not rate-limit the bot's
        scans (the per-IP new-scan limit applies to everyone else)."""
        headers = {"X-Alerts-Secret": self.alerts_secret} if self.alerts_secret else None
        job = self._req("/api/scan", {"token": token}, headers=headers).get("job")
        if not job:
            raise ApiError("no job in /api/scan response")
        return job

    def result(self, job):
        """{"done": False} | {"done": True, "result": {...}} | {"done": True, "error": "..."}."""
        return self._req("/api/result?job=" + urllib.parse.quote(job))

    def rewards_status(self):
        """Site Rewards & Burns status: {"enabled": False} or the full status (see rewards_service.status_json)."""
        return self._req("/api/rewards/status")

    def config(self):
        """Site {"solana", "alerts", "trade"}."""
        return self._req("/api/config")

    def _alerts(self, path, body=None):
        return self._req(path, body, headers={"X-Alerts-Secret": self.alerts_secret}, ok=(409, 422))

    def watch(self, chat_id, token):
        """Subscribe: 200 → {"ok", "renewed", "token", "expires_at", "items", "limit", "days"};
        {"status": 409, "error": "watch limit", "items", ...} | {"status": 422, "error": "too established"}.
        Bad address → Rejected, alerts disabled → AlertsOff."""
        return self._alerts("/api/alerts/watch", {"chat_id": chat_id, "token": token})

    def unwatch(self, chat_id, token):
        return self._alerts("/api/alerts/unwatch", {"chat_id": chat_id, "token": token})

    def watch_list(self, chat_id):
        return self._alerts("/api/alerts/list?chat_id=" + urllib.parse.quote(str(chat_id)))
