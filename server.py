"""HTTP-сервер rh-crawler (стандартная библиотека, ThreadingHTTPServer).
Read-only: скан идёт в фоне, браузер опрашивает события.

  POST /api/scan {"token": "0x..."}      -> {"job": id}
  GET  /api/events?job=ID&after=N        -> {"events": [...], "done": bool}  (события с i >= N)
  GET  /api/result?job=ID                -> {"done", "result" | "error"}
  GET  /api/chart?token=CA               -> {"token", "chain", "pool", "dex", "timeframe", "candles", "price_usd"[, "stale_at" | "unavailable"]}
                                            свечи GeckoTerminal [[ts, o, h, l, c, v]] от старых к новым; в скан не входит
  GET  /api/early?token=CA               -> {"token", "chain", "available", "buyers": [...], "summary": {...}[, "stale_at"]}
                                            первые 20 покупателей и их статус сейчас (early.py); в скан не входит
  GET  /api/recent?limit=12              -> {"items": [{"token", "chain", "launchpad", "ticker", "name", "score", "band", "rug", "ts"}]}
                                            лента «Recently scanned»: последние уникальные токены, новые сверху
  GET  /api/config                       -> {"solana": bool, "alerts": bool, "flap": bool, "trade": {"robinhood": шаблон, "solana": шаблон}}
                                            шаблоны ссылки Trade on Axiom ({address}), env TRADE_URL_* (trade.py)
  GET  /                                 -> index.html
  GET  /favicon.svg, /favicon.png, /apple-touch-icon.png, /favicon.ico  -> иконки из static/
  GET  /health
  HEAD — на любой GET-путь: те же код и заголовки, без тела

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
  GET  /api/rewards/history?kind=burns|draws&limit=10&before=T -> страница полной истории: {"kind", "total",
                                            "items", "next_before"}; limit 1–50, before — unix-время или ISO 8601
  GET  /api/rewards/<YYYY-MM-DD>/participants -> полный список участников с весами
  GET  /api/rewards/<YYYY-MM-DD>/verify  -> входные данные и пересчёт победителя

Alerts, подписки для бота (только при ALERTS_ENABLED=true, иначе 404; X-Alerts-Secret == ALERTS_API_SECRET, иначе 403):
  POST /api/alerts/watch {"chat_id", "token"}   -> подписка на WATCH_DAYS дней; 409 — уже WATCH_LIMIT токенов,
                                                   422 — токен too established, 400 — плохой адрес
  POST /api/alerts/unwatch {"chat_id", "token"} -> {"ok", "removed"}
  GET  /api/alerts/list?chat_id=N               -> {"items": [{"token", "chain", "created_at", "expires_at"}], ...}
  POST /api/alerts/test {"chat_id", "token"}    -> пробное уведомление в chat_id сразу (без cooldown и подписки) по
                                                   последнему снимку токена, нет снимка — по свежему скану;
                                                   {"ok", "source": "snapshot" | "scan"}; 502 — Telegram отказал

Результат токена кэшируется 10 минут: повторный скан отдаёт сохранённые события сразу.
Чарт кэшируется 10 минут (без свечей — 1 минуту); сбой GeckoTerminal — пустые candles, не ошибка.
Лента /api/recent кэшируется 20 секунд; запись — после завершения скана, сбой базы скан не ломает.
При ALERTS_ENABLED=true после скана пишется снимок для alerts (alerts.py, alerts_store.py), так же без влияния на скан;
изменился важный показатель — уведомление подписчикам в Telegram (alerts_notify.py, свой поток, TG_BOT_TOKEN);
плановые перепроверки отслеживаемых токенов — alerts_recheck.py (свой поток, уступает живым сканам).
Не больше 3 сканов одновременно (остальные ждут слота). PORT из env, по умолчанию 8000.
"""
import hashlib, hmac, json, os, re, threading, time, uuid
from datetime import datetime, timezone
from email.utils import formatdate, parsedate_to_datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import alerts
import alerts_notify
import alerts_recheck
import detect
from chains import flap, priority
import early
import engine
import market
import trade
import draw_service as ds
import rewards_service as rs
from draw_store import Store
from rewards_store import RewardsStore
from recent_store import RecentStore
from alerts_store import AlertsStore
from bot.tg import TelegramError

CACHE_TTL = 600            # секунд: кэш результата по токену
MAX_CONCURRENT = 3         # одновременных сканов по умолчанию (env MAX_CONCURRENT)
JOB_TTL = 3600             # секунд: старые задачи удаляются из памяти
ROOT = os.path.dirname(os.path.abspath(__file__))
ICONS = {                  # путь -> (файл в static/, content-type); .ico отдаёт тот же PNG
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/favicon.png": ("favicon.png", "image/png"),
    "/apple-touch-icon.png": ("favicon.png", "image/png"),
    "/favicon.ico": ("favicon.png", "image/png"),
}
ICON_CACHE = "public, max-age=86400"
HTML_CACHE = "no-cache"    # страница: браузер хранит, но каждый раз сверяется (ETag / Last-Modified → 304)
_PAGE = {}                 # (путь, mtime_ns, size) -> (body, etag, last_modified)

CHART_TTL = 600            # секунд: кэш чарта по токену
CHART_EMPTY_TTL = 60       # секунд: кэш чарта без свечей (GT не ответил или токен слишком свежий)
CHART_STALE_TTL = 6 * 3600 # секунд: последний удачный чарт отдаётся (со stale_at), пока GT не отвечает
CHART_MAX = 500            # чартов в памяти, старые вытесняются
RECENT_TTL = 20            # секунд: кэш ленты /api/recent
RECENT_DEFAULT = 12        # записей в ленте по умолчанию

JOBS = {}                  # job_id -> {"token", "chain", "events", "done", "result", "error", "ts"}
CHARTS = {}                # token -> {"ok": (ts, удачный ответ) | None, "miss": (ts, пустой ответ) | None}
RECENT = {}                # limit -> (ts, ответ /api/recent)
BY_TOKEN = {}              # token -> job_id последнего скана
_lock = threading.Lock()


def _env_int(name, default):
    try:
        v = int(os.environ.get(name, "").strip())
        return v if v > 0 else default
    except ValueError:
        return default


MAX_CONCURRENT = _env_int("MAX_CONCURRENT", MAX_CONCURRENT)
_sem = threading.BoundedSemaphore(MAX_CONCURRENT)


def _run(job_id, token):
    with priority.live():   # живой скан: фоновые перепроверки alerts ждут и не стартуют
        _run_job(job_id, token)


def _run_job(job_id, token):
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
        record_snapshot(res)


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


def scan_flags(token):
    """{кошелёк: ["operator", "virgin"]} по последнему готовому скану токена (только топ-20, которых скан проверял);
    скана нет — None. operator — кошелёк в операторе из 2+ кошельков."""
    with _lock:
        job = JOBS.get(BY_TOKEN.get(token))
        res = job and job["done"] and job["result"]
    if not res or not res.get("holders"):
        return None
    multi = {w for o in res.get("operators") or [] if len(o.get("wallets") or []) > 1 for w in o["wallets"]}
    return {h["wallet"]: [f for f, on in (("operator", h["wallet"] in multi),
                                          ("virgin", (h.get("signals") or {}).get("virgin"))) if on]
            for h in res["holders"]}


def get_chart(token):
    """Ответ /api/chart: адрес через engine.chain_of (ScanError — плохой адрес / Solana выключена),
    свечи из market.fetch_chart с кэшем. Любой сбой GT — не исключение: unavailable = True (свечей нет из-за
    сбоя, а не потому, что сделок мало) или последний удачный чарт моложе
    CHART_STALE_TTL с полем stale_at (unix-время, когда он получен), иначе пустой. Пустой ответ удачный
    кэш не перезаписывает."""
    chain, token = engine.chain_of(token)
    now = time.time()

    def fallback(e):
        ok = e.get("ok")
        if ok and now - ok[0] < CHART_STALE_TTL:
            return ok[1] | {"stale_at": int(ok[0])}
        return e["miss"][1]

    with _lock:
        e = CHARTS.get(token) or {"ok": None, "miss": None}
        if e["ok"] and now - e["ok"][0] < CHART_TTL:
            return e["ok"][1]
        if e["miss"] and now - e["miss"][0] < CHART_EMPTY_TTL:
            return fallback(e)
    try:
        c = market.fetch_chart(token, engine.GT_NETWORK[chain])
    except Exception:
        c = {"failed": True}
    out = {"token": token, "chain": chain, "pool": c.get("pool"), "dex": c.get("dex"),
           "timeframe": c.get("timeframe"), "candles": c.get("candles") or [], "price_usd": c.get("price_usd")}
    if c.get("failed") and not out["candles"]:
        out["unavailable"] = True
    with _lock:
        e = CHARTS.get(token) or {"ok": None, "miss": None}
        res = out
        if out["candles"]:
            e = {"ok": (now, out), "miss": None}
        else:
            e = e | {"miss": (now, out)}
            res = fallback(e)
            print(f"chart: {token} no candles from gt -> "
                  + (f"stale cache from {time.strftime('%H:%M', time.gmtime(res['stale_at']))} UTC"
                     if "stale_at" in res else "empty"), flush=True)
        CHARTS[token] = e
        if len(CHARTS) > CHART_MAX:
            last = lambda kv: max(x[0] for x in (kv[1]["ok"], kv[1]["miss"]) if x)
            for t, _ in sorted(CHARTS.items(), key=last)[:len(CHARTS) - CHART_MAX]:
                del CHARTS[t]
    return res


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


def history_args(q):
    """kind, limit, before из запроса страницы /api/rewards/history?kind=burns|draws&limit=10&before=...
    before — unix-время (секунды) или ISO 8601; ошибка — ValueError с текстом для 400."""
    kind = q["kind"][0]
    if kind not in ("burns", "draws"):
        raise ValueError("kind must be burns or draws")
    try:
        limit = int((q.get("limit") or ["10"])[0])
    except ValueError:
        raise ValueError("limit must be an integer") from None
    raw = (q.get("before") or [""])[0].strip()
    before = None
    if raw:
        try:
            if raw.replace(".", "", 1).isdigit():
                before = int(float(raw))
            else:   # ISO без пояса — UTC
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                before = int((dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp())
        except (ValueError, OverflowError):
            raise ValueError("before must be unix time or ISO 8601") from None
    return kind, limit, before


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


_alerts_stores = {}


def alerts_store():
    """Хранилище снимков alerts (файл DRAW_DB_PATH), одно на путь."""
    path = os.environ.get("DRAW_DB_PATH") or ""
    with _stores_lock:
        if path not in _alerts_stores:
            _alerts_stores[path] = AlertsStore(path or None)
        return _alerts_stores[path]


_notifier = {"obj": None, "warned": False}


def notifier():
    """Отправщик уведомлений (свой фоновый поток, alerts_notify) или None без TG_BOT_TOKEN — тогда одно
    предупреждение в лог. Токен нигде не печатается."""
    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    with _stores_lock:
        if not token:
            if not _notifier["warned"]:
                _notifier["warned"] = True
                print("alerts: TG_BOT_TOKEN is not set, notifications are not sent", flush=True)
            return None
        if _notifier["obj"] is None:
            _notifier["obj"] = alerts_notify.Notifier(alerts_store, alerts_notify.telegram(token)).start()
        return _notifier["obj"]


def notify_message(chat_id, text, markup=None):
    """Служебное сообщение подписчику через очередь отправщика; без TG_BOT_TOKEN — не пишем."""
    n = notifier()
    if n:
        n.push_message(chat_id, text, markup)


def record_snapshot(result):
    """Снимок завершённого скана для alerts (только при ALERTS_ENABLED) — у живого скана и у перепроверки.
    Вызывается после done, как лента: клиент уже получил вердикт; сбой базы только логируется.
    diff со старым снимком не пуст — событие в очередь уведомлений (подписчиков ищет и шлёт фоновый поток;
    здесь — без сети и ожидания). TOO_ESTABLISHED — авто-отписка всех подписчиков токена с одним сообщением."""
    if not alerts.enabled():
        return
    try:
        snap = alerts.snapshot(result, early.known_share(result.get("chain"), result.get("token")))
        if not snap:
            return
        prev, cur = alerts_store().record(snap)
        if cur["band"] == detect.TOO_ESTABLISHED:
            chats = alerts_store().unwatch_token(cur["token"])
            if chats:
                print(f"alerts: {cur['token']} became too established, unwatched {len(chats)} chats", flush=True)
            for chat_id in chats:
                notify_message(chat_id, *alerts.established_message(cur["token"], cur.get("ticker"), cur.get("chain")))
            return
        changes = alerts.diff(prev, cur)
        if changes:
            n = notifier()
            if n:
                n.push(prev, cur, changes)
    except Exception as e:
        print(f"alerts: snapshot not saved: {type(e).__name__}: {e}", flush=True)


def record_early(body):
    """/api/early посчитал блок → доля ранних покупателей в последний снимок токена (только при ALERTS_ENABLED).
    Вызывается после ответа клиенту; без сети; сбой только логируется. Устаревший ответ (stale_at) и ошибки не пишутся."""
    if not alerts.enabled():
        return
    try:
        if body.get("available") and not body.get("error") and not body.get("stale_at") and body.get("summary"):
            alerts_store().set_early(body["token"], body["summary"]["now_share_supply"])
    except Exception as e:
        print(f"alerts: early share not saved: {type(e).__name__}: {e}", flush=True)


WATCH_MARKET_BUDGET = 3.0  # секунд на проверку «too established» при подписке на токен, который ещё не сканировали
TOO_ESTABLISHED_WATCH = "This token is too established for CrawlScan, so it can't be watched."
WATCH_LIMIT_TEXT = f"You can watch up to {alerts.WATCH_LIMIT} tokens. Unwatch one first."


def too_established(chain, token):
    """Можно ли подписать: последний снимок, вердикт too established в кэше или рынок (GT / DexScreener, кэш 15 мин;
    в блокчейн не ходим). Рынок не ответил — подписываем (плановая перепроверка отпишет)."""
    try:
        _, cur = alerts_store().get(token)
    except Exception:
        cur = None
    if cur and cur.get("band") == detect.TOO_ESTABLISHED:
        return True
    network, limits = engine.GT_NETWORK[chain], engine.established_limits()
    cached = market.established_get(token, network)
    if cached is not None:
        return detect.too_established(cached, limits)
    try:
        gt = market.fetch_market(token, network, budget=WATCH_MARKET_BUDGET)
    except Exception:
        return False
    if detect.too_established(gt, limits):
        market.established_put(token, network, gt)
        return True
    return False


def alerts_secret_ok(header):
    """X-Alerts-Secret == ALERTS_API_SECRET за постоянное время; секрет не задан — всегда нет. Нигде не логируется."""
    secret = os.environ.get("ALERTS_API_SECRET", "")
    return bool(secret) and hmac.compare_digest((header or "").encode(), secret.encode())


def _chat_id(v):
    if isinstance(v, bool):
        raise ValueError
    if isinstance(v, str):
        v = int(v)
    if not isinstance(v, int):
        raise ValueError
    return v


TEST_SCAN_WAIT = 60   # секунд: ждать скан для пробного уведомления, если снимка токена нет


def test_snapshot(chain, token):
    """(снимок, diff, источник) для пробного уведомления: последний снимок токена; нет — готовый результат скана
    из памяти или новый скан (ждём до TEST_SCAN_WAIT). → (None, None, текст ошибки), если результата нет."""
    prev, cur = alerts_store().get(token)
    if cur:
        return cur, alerts.diff(prev, cur), "snapshot"
    jid = start_scan(token)
    t0 = time.time()
    while time.time() - t0 < TEST_SCAN_WAIT:
        with _lock:
            job = JOBS.get(jid)
            done, res, err = job["done"], job["result"], job["error"]
        if done:
            if err or not res:
                return None, None, err or "scan failed"
            return alerts.snapshot(res), [], "scan"
        time.sleep(0.2)
    return None, None, "scan timed out"


def alerts_test(body):
    """POST /api/alerts/test → (код, тело). Шлёт сразу, в этом потоке: без очереди, cooldown и проверки подписки;
    403 Telegram подписки не трогает."""
    token_env = os.environ.get("TG_BOT_TOKEN", "").strip()
    if not token_env:
        return 503, {"error": "TG_BOT_TOKEN is not set"}
    snap, changes, source = test_snapshot(*engine.chain_of(body.get("token")))
    if snap is None:
        return 409, {"error": f"no result for this token: {source}"}
    text, markup = alerts.message(snap, changes, trade.url(snap.get("chain"), snap["token"]), test=True)
    try:
        alerts_notify.telegram(token_env)(body["chat_id"], text, markup)
    except TelegramError as e:
        return 502, {"error": f"telegram: {e.description}", "code": e.code}
    print(f"alerts: test alert {snap['token']} to {body['chat_id']} ({source})", flush=True)
    return 200, {"ok": True, "token": snap["token"], "source": source, "band": snap.get("band"),
                 "changes": [c["kind"] for c in changes]}


def alerts_api(method, path, body, q):
    """/api/alerts/* → (код, тело). Выключатель и секрет проверяет вызывающий."""
    try:
        chat_id = _chat_id((q.get("chat_id") or [None])[0] if method == "GET" else body.get("chat_id"))
    except (ValueError, TypeError, AttributeError):
        return 400, {"error": "need chat_id"}
    store, now = alerts_store(), int(time.time())
    meta = {"limit": alerts.WATCH_LIMIT, "days": alerts.WATCH_DAYS}
    if method == "GET" and path == "/api/alerts/list":
        return 200, {"chat_id": chat_id, "items": store.watches(chat_id, now)} | meta
    if method != "POST" or path not in ("/api/alerts/watch", "/api/alerts/unwatch", "/api/alerts/test"):
        return 404, {"error": "not found"}
    try:
        chain, token = engine.chain_of(body.get("token"))
    except engine.ScanError as e:
        return 400, {"error": str(e)}
    if path == "/api/alerts/test":
        return alerts_test(body | {"chat_id": chat_id})
    if path == "/api/alerts/unwatch":
        removed = store.unwatch(chat_id, token)
        return 200, {"ok": True, "token": token, "removed": removed, "items": store.watches(chat_id, now)} | meta
    if too_established(chain, token):
        return 422, {"error": "too established", "message": TOO_ESTABLISHED_WATCH, "token": token}
    status, w, items = store.watch(chat_id, token, chain, now)
    if status == "limit":
        return 409, {"error": "watch limit", "message": WATCH_LIMIT_TEXT, "items": items} | meta
    return 200, {"ok": True, "renewed": status == "renewed", **w, "items": items} | meta


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
        if self.command != "HEAD":
            self.wfile.write(b)

    def _send_page(self, path):
        """index.html с Cache-Control: no-cache, ETag и Last-Modified: после деплоя браузер сразу видит новую
        страницу, неизменная — 304 без тела. If-None-Match важнее If-Modified-Since; слабый ETag (W/, его ставит
        Cloudflare при сжатии) сравнивается как сильный."""
        try:
            st = os.stat(path)
        except FileNotFoundError:
            return self._send(200, b"<h1>rh-crawler</h1>", "text/html; charset=utf-8")
        key = (path, st.st_mtime_ns, st.st_size)
        page = _PAGE.get(key)
        if page is None:
            with open(path, "rb") as f:
                body = f.read()
            page = (body, '"' + hashlib.sha256(body).hexdigest()[:20] + '"', formatdate(st.st_mtime, usegmt=True))
            _PAGE.clear()
            _PAGE[key] = page
        body, etag, modified = page
        inm, ims = self.headers.get("If-None-Match"), self.headers.get("If-Modified-Since")
        if inm is not None:
            fresh = any(t.strip().removeprefix("W/") in (etag, "*") for t in inm.split(","))
        else:
            try:
                fresh = ims is not None and int(st.st_mtime) <= parsedate_to_datetime(ims).timestamp()
            except (TypeError, ValueError):
                fresh = False
        self.send_response(304 if fresh else 200)
        self.send_header("cache-control", HTML_CACHE)
        self.send_header("etag", etag)
        self.send_header("last-modified", modified)
        if fresh:
            self.end_headers()
            return
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

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
        if path == "/api/rewards/history" and q.get("kind"):
            try:
                return self._send(200, rs.history_page(store, cfg, *history_args(q)))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
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

    def _alerts(self, method, path, q):
        if not alerts.enabled():
            return self._send(404, {"error": "alerts are disabled"})
        if not alerts_secret_ok(self.headers.get("X-Alerts-Secret")):
            return self._send(403, {"error": "forbidden"})
        body = {}
        if method == "POST":
            try:
                n = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self._send(400, {"error": "bad json"})
        try:
            return self._send(*alerts_api(method, path, body, q))
        except Exception as e:   # сбой базы и т.п.: бот покажет «недоступно»
            print(f"alerts: {path} failed: {type(e).__name__}: {e}", flush=True)
            return self._send(500, {"error": "alerts temporarily unavailable"})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path.startswith("/api/alerts/"):
            return self._alerts("POST", u.path, parse_qs(u.query))
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

    def do_HEAD(self):
        """HEAD = GET без тела: те же код и заголовки (content-length — как у GET), тело не пишется (_send, _send_page)."""
        return self.do_GET()

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/health":
            return self._send(200, {"ok": True})
        if u.path.startswith("/api/draw/"):
            return self._draw_get(u.path, q)
        if u.path.startswith("/api/rewards/"):
            return self._rewards_get(u.path, q)
        if u.path.startswith("/api/alerts/"):
            return self._alerts("GET", u.path, q)
        if u.path == "/api/config":  # фронт и бот: какие сети включены (SOLANA_ENABLED), алерты (ALERTS_ENABLED), Trade on Axiom
            return self._send(200, {"solana": engine.solana_enabled(), "alerts": alerts.enabled(),
                                    "flap": flap.enabled(), "trade": trade.templates()})
        if u.path == "/api/chart":
            try:
                return self._send(200, get_chart((q.get("token") or [""])[0]))
            except engine.ScanError as e:
                return self._send(400, {"error": str(e)})
        if u.path == "/api/early":
            try:
                token = (q.get("token") or [""])[0]
                chain, ca = engine.chain_of(token)
                # по истории живого скана — как живой; без неё — фон (уступает сканам, BACKGROUND_RPS)
                bg = priority.background_rps() > 0 and early.background(chain, ca)
                with priority.background() if bg else priority.live():
                    body = early.get(token, scan_flags(ca))
            except engine.ScanError as e:
                return self._send(400, {"error": str(e)})
            self._send(200, body)
            return record_early(body)
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
            return self._send_page(os.path.join(ROOT, "index.html"))
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


def recheck_scan(token):
    """Перепроверка: обычный скан движка без задачи, ленты и событий."""
    return engine.scan(token)


def start_alerts_rechecker():
    """Плановые перепроверки отслеживаемых токенов — только при ALERTS_ENABLED=true."""
    if not alerts.enabled():
        return None
    cfg = alerts_recheck.config()
    print(f"alerts: rechecks every {cfg['recheck_min']} min, max {cfg['max_per_hour']}/hour", flush=True)
    return alerts_recheck.Rechecker(
        alerts_store, recheck_scan, record_snapshot, notify_message,
        rate_limited=lambda: sum(a.RATE_LIMITED[0] for a in engine.CHAINS.values()), cfg=cfg).start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"rh-crawler on http://0.0.0.0:{port}", flush=True)
    start_draw_scheduler()
    start_rewards_scheduler()
    start_alerts_rechecker()
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
