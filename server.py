"""HTTP-сервер rh-crawler (стандартная библиотека, ThreadingHTTPServer).
Read-only: скан идёт в фоне, браузер опрашивает события.

  POST /api/scan {"token": "0x..."}      -> {"job": id}
  GET  /api/events?job=ID&after=N        -> {"events": [...], "done": bool}  (события с i >= N)
  GET  /api/result?job=ID                -> {"done", "result" | "error"}
  GET  /api/chart?token=CA               -> {"token", "chain", "pool", "dex", "timeframe", "candles", "price_usd"}
                                            свечи GeckoTerminal [[ts, o, h, l, c, v]] от старых к новым; в скан не входит
  GET  /api/recent?limit=12              -> {"items": [{"token", "chain", "ticker", "name", "score", "band", "rug", "ts"}]}
                                            лента «Recently scanned»: последние уникальные токены, новые сверху
  GET  /api/config                       -> {"solana": bool, "trade": {"robinhood": шаблон, "solana": шаблон}}
                                            шаблоны ссылки Trade on Axiom ({address}), env TRADE_URL_* (trade.py)
  GET  /                                 -> index.html
  GET  /favicon.svg, /favicon.png, /apple-touch-icon.png, /favicon.ico  -> иконки из static/
  GET  /health

Розыгрыш среди холдеров (только при DRAW_ENABLED=true, иначе 404; status отвечает всегда):
  GET  /api/draw/status                  -> включено ли, токен, следующий розыгрыш, снимки и веса за сегодня
  GET  /api/draw/latest                  -> последний розыгрыш
  GET  /api/draw/history?limit=30        -> розыгрыши, новые первыми (limit до 365)
  GET  /api/draw/<YYYY-MM-DD>/participants -> полный список участников с весами
  GET  /api/draw/<YYYY-MM-DD>/verify     -> входные данные и пересчёт победителя
  POST /api/draw/<YYYY-MM-DD>/payout     -> админ: X-Admin-Token == ADMIN_TOKEN,
                                            {"prize_amount", "prize_currency", "payout_tx"}; иначе 403

Token Burn & Holder Rewards, Robinhood (только при REWARDS_ENABLED=true, иначе 404; status отвечает всегда):
  GET  /api/rewards/status               -> токен, следующий розыгрыш и плановое сжигание, последнее сжигание,
                                            всего сожжено, последний победитель (вес, шанс, выплата), участники
  GET  /api/rewards/history?limit=30     -> розыгрыши и сжигания, новые первыми (limit до 365)
  GET  /api/rewards/<YYYY-MM-DD>/participants -> полный список участников с весами
  GET  /api/rewards/<YYYY-MM-DD>/verify  -> входные данные и пересчёт победителя

Результат токена кэшируется 10 минут: повторный скан отдаёт сохранённые события сразу.
Чарт кэшируется 10 минут (без свечей — 1 минуту); сбой GeckoTerminal — пустые candles, не ошибка.
Лента /api/recent кэшируется 20 секунд; запись — после завершения скана, сбой базы скан не ломает.
Не больше 3 сканов одновременно (остальные ждут слота). PORT из env, по умолчанию 8000.
"""
import hmac, json, os, re, threading, time, uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import engine
import market
import trade
import draw_service as ds
import rewards_service as rs
from draw_store import Store
from rewards_store import RewardsStore
from recent_store import RecentStore

CACHE_TTL = 600            # секунд: кэш результата по токену
MAX_CONCURRENT = 3         # одновременных сканов
JOB_TTL = 3600             # секунд: старые задачи удаляются из памяти
ROOT = os.path.dirname(os.path.abspath(__file__))
ICONS = {                  # путь -> (файл в static/, content-type); .ico отдаёт тот же PNG
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/favicon.png": ("favicon.png", "image/png"),
    "/apple-touch-icon.png": ("favicon.png", "image/png"),
    "/favicon.ico": ("favicon.png", "image/png"),
}
ICON_CACHE = "public, max-age=86400"

CHART_TTL = 600            # секунд: кэш чарта по токену
CHART_EMPTY_TTL = 60       # секунд: кэш чарта без свечей (GT не ответил или токен слишком свежий)
CHART_MAX = 500            # чартов в памяти, старые вытесняются
RECENT_TTL = 20            # секунд: кэш ленты /api/recent
RECENT_DEFAULT = 12        # записей в ленте по умолчанию

JOBS = {}                  # job_id -> {"token", "chain", "events", "done", "result", "error", "ts"}
CHARTS = {}                # token -> (ts, ответ /api/chart)
RECENT = {}                # limit -> (ts, ответ /api/recent)
BY_TOKEN = {}              # token -> job_id последнего скана
_lock = threading.Lock()
_sem = threading.BoundedSemaphore(MAX_CONCURRENT)


def _run(job_id, token):
    job = JOBS[job_id]

    def emit(e):
        with _lock:
            job["events"].append(e)

    with _sem:
        try:
            res = engine.scan(token, emit)
            with _lock:
                job["result"] = res
        except engine.ScanError as e:
            err = str(e)
        except Exception as e:  # сбой RPC и т.п.: отдаём текст, процесс живёт
            err = f"scan failed: {e}"
        else:
            err = None
        with _lock:
            if err:
                job["error"] = err
                job["events"].append({"i": len(job["events"]), "t": int((time.time() - job["ts"]) * 1000),
                                      "type": "error", "spider": None, "wallet": None, "detail": err,
                                      "chain": job["chain"]})
            job["done"] = True
    if not err:
        record_recent(res)


def start_scan(token):
    """job_id: свежий кэш по токену или уже идущий скан того же токена, иначе новый."""
    chain, token = engine.chain_of(token)
    now = time.time()
    with _lock:
        for jid in [j for j, v in JOBS.items() if v["done"] and now - v["ts"] > JOB_TTL]:
            if BY_TOKEN.get(JOBS[jid]["token"]) == jid:
                del BY_TOKEN[JOBS[jid]["token"]]
            del JOBS[jid]
        jid = BY_TOKEN.get(token)
        job = JOBS.get(jid)
        if job and (not job["done"] or (job["result"] and now - job["ts"] < CACHE_TTL)):
            return jid
        jid = uuid.uuid4().hex[:12]
        JOBS[jid] = {"token": token, "chain": chain, "events": [], "done": False, "result": None, "error": None,
                     "ts": now}
        BY_TOKEN[token] = jid
    threading.Thread(target=_run, args=(jid, token), daemon=True).start()
    return jid


def get_chart(token):
    """Ответ /api/chart: адрес через engine.chain_of (ScanError — плохой адрес / Solana выключена),
    свечи из market.fetch_chart с кэшем. Любой сбой GT — пустой чарт, не исключение."""
    chain, token = engine.chain_of(token)
    now = time.time()
    with _lock:
        hit = CHARTS.get(token)
        if hit and now - hit[0] < (CHART_TTL if hit[1]["candles"] else CHART_EMPTY_TTL):
            return hit[1]
    try:
        c = market.fetch_chart(token, engine.GT_NETWORK[chain])
    except Exception:
        c = {}
    out = {"token": token, "chain": chain, "pool": c.get("pool"), "dex": c.get("dex"),
           "timeframe": c.get("timeframe"), "candles": c.get("candles") or [], "price_usd": c.get("price_usd")}
    with _lock:
        CHARTS[token] = (now, out)
        if len(CHARTS) > CHART_MAX:
            for t, _ in sorted(CHARTS.items(), key=lambda kv: kv[1][0])[:len(CHARTS) - CHART_MAX]:
                del CHARTS[t]
    return out


DRAW_PATH = re.compile(r"^/api/draw/(\d{4}-\d{2}-\d{2})/(participants|verify|payout)$")
_stores, _stores_lock = {}, threading.Lock()


def draw_store():
    """Хранилище розыгрыша (одно на путь DRAW_DB_PATH) или None, если розыгрыш выключен."""
    if not ds.enabled():
        return None
    path = os.environ.get("DRAW_DB_PATH") or ""
    with _stores_lock:
        if path not in _stores:
            _stores[path] = Store(path or None)
        return _stores[path]


REWARDS_PATH = re.compile(r"^/api/rewards/(\d{4}-\d{2}-\d{2})/(participants|verify)$")
_rw_stores = {}


def rewards_store():
    """Хранилище Rewards & Burns (файл DRAW_DB_PATH) или None, если выключено."""
    if not rs.enabled():
        return None
    path = os.environ.get("DRAW_DB_PATH") or ""
    with _stores_lock:
        if path not in _rw_stores:
            _rw_stores[path] = RewardsStore(path or None)
        return _rw_stores[path]


_recent_stores = {}


def recent_store():
    """Хранилище ленты (файл DRAW_DB_PATH), одно на путь."""
    path = os.environ.get("DRAW_DB_PATH") or ""
    with _stores_lock:
        if path not in _recent_stores:
            _recent_stores[path] = RecentStore(path or None)
        return _recent_stores[path]


def record_recent(result):
    """Записать завершённый скан в ленту. Вызывается после done: клиент уже получил вердикт.
    Любой сбой базы только логируется — скан от ленты не зависит."""
    try:
        if recent_store().record(result):
            with _lock:
                RECENT.clear()
    except Exception as e:
        print(f"recent: not saved: {type(e).__name__}: {e}", flush=True)


def get_recent(limit):
    """Ответ /api/recent с кэшем RECENT_TTL. Сбой базы — пустая лента, не ошибка."""
    now = time.time()
    with _lock:
        hit = RECENT.get(limit)
        if hit and now - hit[0] < RECENT_TTL:
            return hit[1]
    try:
        out = {"items": recent_store().recent(limit)}
    except Exception as e:
        print(f"recent: not read: {type(e).__name__}: {e}", flush=True)
        return {"items": []}
    with _lock:
        RECENT[limit] = (now, out)
    return out


def admin_ok(header):
    """X-Admin-Token совпадает с ADMIN_TOKEN (сравнение за постоянное время). Не задан ADMIN_TOKEN — всегда нет."""
    token = os.environ.get("ADMIN_TOKEN") or ""
    return bool(token) and bool(header) and hmac.compare_digest(header.encode(), token.encode())


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json", cache="no-store"):
        b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(b)))
        self.send_header("cache-control", cache)
        self.end_headers()
        self.wfile.write(b)

    def _job(self, q):
        jid = (q.get("job") or [""])[0]
        with _lock:
            return JOBS.get(jid)

    def _draw_payout(self, day):
        if not admin_ok(self.headers.get("X-Admin-Token")):
            return self._send(403, {"error": "forbidden"})
        store = draw_store()
        if store is None:
            return self._send(404, {"error": "draw is disabled"})
        try:
            n = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            amount, currency, tx = body["prize_amount"], body["prize_currency"], body["payout_tx"]
            if not isinstance(currency, str) or not isinstance(tx, str) or not currency or not tx \
                    or isinstance(amount, bool) or float(amount) < 0:
                raise ValueError
        except (ValueError, KeyError, TypeError, AttributeError):
            return self._send(400, {"error": "need prize_amount, prize_currency, payout_tx"})
        res = store.set_payout(day, amount, currency, tx)
        if res == "not_found":
            return self._send(404, {"error": "no draw for this day"})
        if res in ("no_winner", "conflict"):
            return self._send(409, {"error": "no winner, prize carries over" if res == "no_winner"
                                    else "payout already recorded with another tx"})
        return self._send(200, ds.draw_json(store.get_draw(day)))

    def _draw_get(self, path, q):
        if path == "/api/draw/status":
            return self._send(200, ds.status_json(draw_store(), ds.config()))
        store = draw_store()
        if store is None:
            return self._send(404, {"error": "draw is disabled"})
        if path == "/api/draw/latest":
            row = store.latest_draw()
            return self._send(200, ds.draw_json(row)) if row else self._send(404, {"error": "no draws yet"})
        if path == "/api/draw/history":
            try:
                limit = min(365, max(1, int((q.get("limit") or ["30"])[0])))
            except ValueError:
                limit = 30
            return self._send(200, {"draws": [ds.draw_json(r) for r in store.history(limit)]})
        m = DRAW_PATH.match(path)
        if not m or m.group(2) == "payout":
            return self._send(404, {"error": "not found"})
        day, what = m.groups()
        row = store.get_draw(day)
        if not row:
            return self._send(404, {"error": "no draw for this day"})
        if what == "participants":
            parts = store.participants(day)
            return self._send(200, {"day": day, "list_hash": row["list_hash"], "total_weight": int(row["total_weight"]),
                                    "participants": [{"address": a, "weight": w} for a, w in sorted(parts.items())]})
        return self._send(200, ds.verify_json(store, day))

    def _rewards_get(self, path, q):
        cfg = rs.config()
        if path == "/api/rewards/status":
            return self._send(200, rs.status_json(rewards_store(), cfg))
        store = rewards_store()
        if store is None:
            return self._send(404, {"error": "rewards are disabled"})
        if path == "/api/rewards/history":
            try:
                limit = min(365, max(1, int((q.get("limit") or ["30"])[0])))
            except ValueError:
                limit = 30
            return self._send(200, rs.history_json(store, cfg, limit))
        m = REWARDS_PATH.match(path)
        if not m:
            return self._send(404, {"error": "not found"})
        day, what = m.groups()
        body = rs.participants_json(store, day) if what == "participants" else rs.verify_json(store, day)
        return self._send(200, body) if body else self._send(404, {"error": "no draw for this day"})

    def do_POST(self):
        m = DRAW_PATH.match(urlparse(self.path).path)
        if m and m.group(2) == "payout":
            return self._draw_payout(m.group(1))
        if urlparse(self.path).path != "/api/scan":
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            return self._send(200, {"job": start_scan(body.get("token"))})
        except engine.ScanError as e:
            return self._send(400, {"error": str(e)})
        except (ValueError, AttributeError):
            return self._send(400, {"error": "bad json"})

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/health":
            return self._send(200, {"ok": True})
        if u.path.startswith("/api/draw/"):
            return self._draw_get(u.path, q)
        if u.path.startswith("/api/rewards/"):
            return self._rewards_get(u.path, q)
        if u.path == "/api/config":  # фронт: какие сети включены (Solana — флаг SOLANA_ENABLED), шаблоны Trade on Axiom
            return self._send(200, {"solana": engine.solana_enabled(), "trade": trade.templates()})
        if u.path == "/api/chart":
            try:
                return self._send(200, get_chart((q.get("token") or [""])[0]))
            except engine.ScanError as e:
                return self._send(400, {"error": str(e)})
        if u.path == "/api/recent":
            try:
                limit = min(50, max(1, int((q.get("limit") or [str(RECENT_DEFAULT)])[0])))
            except ValueError:
                limit = RECENT_DEFAULT
            return self._send(200, get_recent(limit), cache=f"public, max-age={RECENT_TTL}")
        if u.path == "/api/events":
            job = self._job(q)
            if job is None:
                return self._send(404, {"error": "unknown job"})
            try:
                after = max(0, int((q.get("after") or ["0"])[0]))
            except ValueError:
                after = 0
            with _lock:
                return self._send(200, {"events": job["events"][after:], "done": job["done"]})
        if u.path == "/api/result":
            job = self._job(q)
            if job is None:
                return self._send(404, {"error": "unknown job"})
            with _lock:
                if not job["done"]:
                    return self._send(200, {"done": False})
                if job["error"]:
                    return self._send(200, {"done": True, "error": job["error"]})
                return self._send(200, {"done": True, "result": job["result"]})
        if u.path in ICONS:
            name, ctype = ICONS[u.path]
            try:
                with open(os.path.join(ROOT, "static", name), "rb") as f:
                    return self._send(200, f.read(), ctype, ICON_CACHE)
            except FileNotFoundError:
                return self._send(404, {"error": "not found"})
        if u.path in ("/", "/index.html"):
            try:
                with open(os.path.join(ROOT, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                return self._send(200, b"<h1>rh-crawler</h1>", "text/html; charset=utf-8")
        return self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass


def start_draw_scheduler():
    """Фоновый планировщик розыгрыша — только при DRAW_ENABLED=true и заданном DRAW_MINT."""
    cfg = ds.config()
    if not cfg["enabled"]:
        return None
    if not cfg["mint"]:
        ds.log("DRAW_ENABLED=true, but DRAW_MINT is empty: scheduler not started")
        return None
    sch = ds.Scheduler(draw_store(), cfg)
    sch.start()
    return sch


def start_rewards_scheduler():
    """Фоновый поток Rewards & Burns — только при REWARDS_ENABLED=true."""
    cfg = rs.config()
    if not cfg["enabled"]:
        return None
    sch = rs.Scheduler(rewards_store(), cfg)
    sch.start()
    return sch


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"rh-crawler on http://0.0.0.0:{port}", flush=True)
    start_draw_scheduler()
    start_rewards_scheduler()
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
