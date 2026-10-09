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
  GET  /api/config                       -> {"solana": bool, "alerts": bool, "flap": bool, "bankr": bool, "trade": {"robinhood": шаблон, "solana": шаблон}}
                                            шаблоны ссылки Trade on Axiom ({address}), env TRADE_URL_* (trade.py)
  GET  /api/index?token=CA               -> {"token", "state": ready|building|queued|queue_full|too_large|unavailable|none, "eta_s"[, "position"]} (BANKR_ENABLED)
                                            полный индекс холдеров Bankr готов? (сайт перескан делает, когда ready)
  GET  /api/replay                       -> {"items": [...], "stats": {...}, "since"} (REPLAY_ENABLED, иначе 404; кэш 60 с)
                                            Rug Replay: DANGER-токены, упавшие на ≥ 90% после скана, и доли за 7 дней
  GET  /replay                           -> replay.html (REPLAY_ENABLED, иначе 404)
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
  У премиума (PREMIUM_ENABLED) лимит и срок подписок — по Telegram ID: до premium.WATCH_LIMIT токенов, без срока.

Premium для бота (только при PREMIUM_ENABLED=true, иначе 404; тот же X-Alerts-Secret, иначе 403; premium*.py):
  POST /api/premium/reserve {"user_id", "wallet"} -> {"state": reserved|pending|yours, "wallet", "expires_at", "minutes"};
                                                   409 taken — кошелёк забронирован или привязан к другому аккаунту;
                                                   429 — больше premium.ATTEMPTS_PER_HOUR броней в час; 400 — не адрес
  GET  /api/premium/status?user_id=N              -> {"linked", "premium", "wallet", "balance_tokens", "min_tokens",
                                                   "next_check_at", "grace_until", "reservation", "watch_limit", ...}
  POST /api/premium/unlink {"user_id"}            -> {"ok", "wallet" | null, "removed": [токены, снятые с Watchlist]}
  POST /api/premium/admin_unlink {"wallet"}       -> {"ok", "found", "removed"} (бот пускает только PREMIUM_ADMIN_ID)

Результат токена кэшируется 10 минут: повторный скан отдаёт сохранённые события сразу.
Чарт кэшируется 10 минут (без свечей — 1 минуту); сбой GeckoTerminal — пустые candles, не ошибка.
/api/rewards/status, /api/draw/status и /api/config кэшируются в памяти 30 секунд (Cache-Control: public, max-age=30 —
для Cloudflare; у статуса наград поле now всегда текущее); сбой не кэшируется.
Новых сканов с одного IP (CF-Connecting-IP) — не больше SCAN_RATE_PER_MIN в минуту, иначе 429 rate_limited с Retry-After;
user agent crawlscan-bot — SCAN_RATE_BOT_PER_MIN, верный X-Alerts-Secret (наш бот) — без лимита.
Лента /api/recent кэшируется 15 секунд (запись скана сбрасывает кэш; чтение, пересёкшееся с записью, в кэш не идёт);
запись — после завершения скана, сбой базы скан не ломает (в лог «recent: not saved»).
При ALERTS_ENABLED=true после скана пишется снимок для alerts (alerts.py, alerts_store.py), так же без влияния на скан;
изменился важный показатель — уведомление подписчикам в Telegram (alerts_notify.py, свой поток, TG_BOT_TOKEN);
плановые перепроверки отслеживаемых токенов — alerts_recheck.py (свой поток, уступает живым сканам).
При OPMEM_ENABLED=true результат скана и перепроверки — в очередь памяти операторов (opmem.py: своя база
memory.db, свой поток записи, токен не чаще раза в OPMEM_MIN_INTERVAL_H); скан запись не ждёт.
При REPLAY_ENABLED=true — Rug Replay (replay.py): свой поток раз в REPLAY_CHECK_MIN сверяет капу DANGER-токенов из
memory.db с DexScreener (без RPC), /replay и /api/replay; скан, бот и розыгрыш не трогаются.
Не больше 3 сканов одновременно (остальные ждут слота), в очереди — не больше SCAN_QUEUE_MAX; сверх очереди — 503 busy
с Retry-After (кэш и уже идущий скан того же токена отдаются как раньше). Память процесса (memguard.py) выше мягкого
порога после сброса кэшей — сканы до MAX_CONCURRENT без очереди, выше жёсткого — 503 busy. Раз в минуту в лог
строка с памятью, числом активных сканов и счётчиками за минуту. Сбой базы в /api/rewards/* и /api/draw/* — 503, процесс и сканы живут.
PORT из env, по умолчанию 8000.
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
from chains import bankr, flap, priority
import early
import engine
import market
import memguard
import opmem
import premium
import premium_service
import replay
import trade
import draw_service as ds
import rewards_service as rs
from draw_store import Store
from rewards_store import RewardsStore
from recent_store import RecentStore
from alerts_store import AlertsStore
from premium_store import PremiumStore
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
RECENT_TTL = 15            # секунд: кэш ленты /api/recent (и max-age для Cloudflare)
STATUS_TTL = 30            # секунд: кэш /api/rewards/status, /api/draw/status, /api/config (и max-age)
STATUS_CACHE = f"public, max-age={STATUS_TTL}"
REPLAY_TTL = 60            # секунд: кэш /api/replay (и max-age)
RECENT_DEFAULT = 12        # записей в ленте по умолчанию

JOBS = {}                  # job_id -> {"token", "chain", "events", "done", "result", "error", "ts"}
CHARTS = {}                # token -> {"ok": (ts, удачный ответ) | None, "miss": (ts, пустой ответ) | None}
RECENT = {}                # limit -> (ts, ответ /api/recent)
_RECENT_GEN = [0]          # поколение ленты: +1 при каждой записи скана (под _lock)
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
SCAN_QUEUE_MAX = 6         # новых сканов ждут слота сверх MAX_CONCURRENT (env SCAN_QUEUE_MAX); дальше — 503 busy
SCAN_QUEUE_MAX = _env_int("SCAN_QUEUE_MAX", SCAN_QUEUE_MAX)
_ACTIVE = [0]              # сканов идёт или ждёт слота (под _lock)
_STATS = {"started": 0, "busy": 0}   # новых сканов и отказов busy с прошлой строки memguard (под _lock)
RETRY_AFTER = 5            # секунд: заголовок Retry-After у 503 busy
BUSY = {"error": "busy", "message": "Scanner is busy, try again in a few seconds"}
# частота новых сканов с одного IP (CF-Connecting-IP): обычный клиент / бот CrawlScan по user agent;
# запрос с верным X-Alerts-Secret (наш бот) — без лимита
SCAN_RATE_PER_MIN = _env_int("SCAN_RATE_PER_MIN", 10)
SCAN_RATE_BOT_PER_MIN = _env_int("SCAN_RATE_BOT_PER_MIN", 120)
RATE_WINDOW = 60           # секунд
RATE_MAX_CLIENTS = 20_000  # IP в памяти; больше — забываем всех (лимит мягкий)
RATE_LIMITED = {"error": "rate_limited", "message": "Too many scans from your address, try again in a minute"}
_RATE = {}                 # client -> [время новых сканов за RATE_WINDOW]

for _a in engine.CHAINS.values():       # выше порога памяти memguard сбрасывает историю сканов и кэш переводов
    if hasattr(_a, "drop_caches"):
        memguard.on_relieve(_a.drop_caches)
memguard.on_relieve(bankr.drop_caches)  # ... и индексы Bankr (идущее построение прерывается)
bankr.memory_high = memguard.near       # построение индекса Bankr не идёт у порога памяти
bankr.after_build = memguard.trim       # ... а после него память страниц возвращается ОС


class Busy(Exception):
    """Новый скан не принят: очередь полна или память выше MEMORY_SOFT_LIMIT_MB."""


class RateLimited(Exception):
    """Слишком много новых сканов с одного IP; args[0] — через сколько секунд можно снова."""


def _rate_take(client, now):
    """Учесть новый скан клиента (client = (ключ, лимит в минуту) или None — без лимита). Под _lock.
    Лимит исчерпан — RateLimited(секунд до освобождения)."""
    if client is None:
        return
    key, limit = client
    hits = [t for t in _RATE.get(key, ()) if now - t < RATE_WINDOW]
    if len(hits) >= limit:
        _RATE[key] = hits
        raise RateLimited(max(1, int(RATE_WINDOW - (now - hits[0])) + 1))
    if key not in _RATE and len(_RATE) >= RATE_MAX_CLIENTS:
        _RATE.clear()
    hits.append(now)
    _RATE[key] = hits


def _run(job_id, token):
    try:
        with priority.live():   # живой скан: фоновые перепроверки alerts ждут и не стартуют
            _run_job(job_id, token)
    finally:
        with _lock:
            _ACTIVE[0] -= 1


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
        opmem.record(res)       # только в очередь (OPMEM_ENABLED), запись — в своём потоке
        record_recent(res)
        record_snapshot(res)


def _reuse(token, now):
    """job_id свежего кэша или идущего скана токена, иначе None (под _lock)."""
    jid = BY_TOKEN.get(token)
    job = JOBS.get(jid)
    if job and (not job["done"] or (job["result"] and now - job["ts"] < CACHE_TTL and not _index_ready(job["result"]))):
        return jid
    return None


def _index_ready(result):
    """Частичный скан Bankr (индекс холдеров строился), а индекс уже готов — кэш устарел: новый скан будет полным."""
    ps = result.get("partial_scan")
    return bool(ps) and bankr.index_status(result["token"])["state"] == "ready"


def _busy(why):
    with _lock:
        _STATS["busy"] += 1
    raise Busy(why)


def scan_stats():
    """Для строки memguard раз в минуту: (активных сканов, хвост строки); счётчики обнуляются."""
    with _lock:
        active, started, busy = _ACTIVE[0], _STATS["started"], _STATS["busy"]
        _STATS["started"] = _STATS["busy"] = 0
    return active, f", last min: {started} started, {busy} busy"


def start_scan(token, client=None):
    """job_id: свежий кэш по токену или уже идущий скан того же токена, иначе новый.
    Новый — только если в очереди есть место и память ниже порога, иначе Busy; и если client (ключ, лимит в минуту)
    не исчерпал лимит новых сканов, иначе RateLimited. Кэш и подключение к идущему скану лимит не тратят."""
    chain, token = engine.chain_of(token)
    now = time.time()
    with _lock:
        for jid in [j for j, v in JOBS.items() if v["done"] and now - v["ts"] > JOB_TTL]:
            if BY_TOKEN.get(JOBS[jid]["token"]) == jid:
                del BY_TOKEN[JOBS[jid]["token"]]
            del JOBS[jid]
        jid = _reuse(token, now)
        if jid:
            return jid
        active = _ACTIVE[0]
    if active >= MAX_CONCURRENT + SCAN_QUEUE_MAX:
        _busy("queue")
    if memguard.over(live=active, cap=MAX_CONCURRENT):   # вне _lock: сброс кэшей и gc не держат остальные запросы
        _busy("memory")
    with _lock:
        jid = _reuse(token, now)
        if jid:
            return jid
        if _ACTIVE[0] >= MAX_CONCURRENT + SCAN_QUEUE_MAX:
            _STATS["busy"] += 1
            raise Busy("queue")
        _rate_take(client, now)
        _ACTIVE[0] += 1
        _STATS["started"] += 1
        jid = uuid.uuid4().hex[:12]
        JOBS[jid] = {"token": token, "chain": chain, "events": [], "done": False, "result": None, "error": None,
                     "ts": now}
        BY_TOKEN[token] = jid
    try:
        threading.Thread(target=_run, args=(jid, token), daemon=True).start()
    except RuntimeError:   # can't start new thread: как полная очередь
        with _lock:
            _ACTIVE[0] -= 1
            JOBS.pop(jid, None)
            if BY_TOKEN.get(token) == jid:
                del BY_TOKEN[token]
        raise Busy("thread") from None
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
                _RECENT_GEN[0] += 1
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
    if not alerts.enabled() or result.get("partial_scan") or result.get("band") == detect.TOO_ACTIVE:
        return   # частичный скан Bankr: diff с полным был бы ложным; TOO ACTIVE — без вердикта, сравнивать нечего
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
            alerts_store().set_early(body["token"], body["summary"].get("held_share_supply", body["summary"]["now_share_supply"]))   # локер — не выход
    except Exception as e:
        print(f"alerts: early share not saved: {type(e).__name__}: {e}", flush=True)


WATCH_MARKET_BUDGET = 3.0  # секунд на проверку «too established» при подписке на токен, который ещё не сканировали
TOO_ESTABLISHED_WATCH = "This token is too established for CrawlScan, so it can't be watched."
TOO_ACTIVE_WATCH = "This token has too many trades for a full scan right now, so it can't be watched."
WATCH_LIMIT_TEXT = "You can watch up to {} tokens. Unwatch one first."


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
    try:
        jid = start_scan(token)
    except Busy:
        return None, None, BUSY["message"]
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
    limit, days = premium_terms(chat_id, now)   # без PREMIUM_ENABLED — всегда обычные (3, 7), база премиума не открывается
    meta = {"limit": limit, "days": days}
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
    if chain == "robinhood" and bankr.recently_active(token):   # TOO ACTIVE: вердикта нет — следить не за чем
        return 422, {"error": "too active", "message": TOO_ACTIVE_WATCH, "token": token}
    if too_established(chain, token):
        return 422, {"error": "too established", "message": TOO_ESTABLISHED_WATCH, "token": token}
    status, w, items = store.watch(chat_id, token, chain, now, limit=limit, days=days)
    if status == "limit":
        return 409, {"error": "watch limit", "message": WATCH_LIMIT_TEXT.format(limit), "items": items} | meta
    return 200, {"ok": True, "renewed": status == "renewed", **w, "items": items} | meta


_premium = {"store": {}, "svc": None}
PREMIUM_ATTEMPTS = premium.Attempts()
PREMIUM_TAKEN = "This wallet is already linked to another account."


def premium_store():
    """Хранилище премиума (своя база premium.db, не draw.db), одно на путь."""
    path = premium.db_path()
    with _stores_lock:
        if path not in _premium["store"]:
            _premium["store"][path] = PremiumStore(path)
        return _premium["store"][path]


def premium_terms(chat_id, now):
    """(лимит, дней | None) подписок чата: премиум — (10, None), иначе обычные. Выключено или сбой базы — обычные."""
    if not premium.enabled():
        return alerts.WATCH_LIMIT, alerts.WATCH_DAYS
    try:
        return premium_store().watch_terms(chat_id, now)
    except Exception as e:
        print(f"premium: terms not read: {type(e).__name__}: {e}", flush=True)
        return alerts.WATCH_LIMIT, alerts.WATCH_DAYS


def premium_upgrade(user_id):
    """Стал премиумом: подписки — без срока (только при ALERTS_ENABLED)."""
    if alerts.enabled():
        alerts_store().make_permanent(user_id)


def premium_downgrade(user_id):
    """Премиум закончился: Watchlist как у обычных (3 самых старых, 7 дней). → снятые токены."""
    if not alerts.enabled():
        return []
    return [w["token"] for w in alerts_store().downgrade(user_id)]


def premium_svc():
    """Сервис премиума (поток стартует start_premium); здесь — для decimals токена в статусе.
    Хранилище берётся до _stores_lock: premium_store() сам берёт этот замок (он не реентерабельный)."""
    store = premium_store()
    with _stores_lock:
        if _premium["svc"] is None:
            from chains import robinhood
            _premium["svc"] = premium_service.Service(store, notify_message, premium_upgrade,
                                                      premium_downgrade, robinhood)
        return _premium["svc"]


def premium_status(user_id, now):
    st = premium_store()
    link, res = st.link_of(user_id), st.reservation_of(user_id)
    limit, days = premium.watch_terms(link, now)
    out = {"user_id": user_id, "linked": bool(link), "premium": bool(link and link["premium"]),
           "min_tokens": premium.min_tokens(), "watch_limit": limit, "watch_days": days,
           "reservation": {"wallet": res["wallet"], "expires_at": res["expires_at"]} if res else None}
    if link:
        out |= {"wallet": link["wallet"], "balance_tokens": link["balance"] // 10 ** premium_svc().decimals(),
                "linked_at": link["linked_at"], "checked_at": link["checked_at"], "next_check_at": link["next_check_at"],
                "grace_until": link["below_since"] + premium.GRACE_DAYS * 86400 if premium.in_grace(link, now) else None}
    return out


def premium_api(method, path, body, q):
    """/api/premium/* → (код, тело). Выключатель и секрет проверяет вызывающий."""
    now = int(time.time())
    if path == "/api/premium/admin_unlink" and method == "POST":
        wallet = premium.wallet_of(body.get("wallet"))
        if not wallet:
            return 400, {"error": "not a wallet address"}
        link = premium_store().unlink_wallet(wallet)
        removed = premium_downgrade(link["user_id"]) if link else []
        if link:
            print(f"premium: admin unlinked {premium.short(wallet)} {premium.log_user(link['user_id'])}", flush=True)
            notify_message(link["user_id"], premium.admin_unlinked_message(wallet, removed))
        return 200, {"ok": True, "found": bool(link), "wallet": wallet, "removed": len(removed)}
    try:
        user_id = _chat_id((q.get("user_id") or [None])[0] if method == "GET" else body.get("user_id"))
    except (ValueError, TypeError, AttributeError):
        return 400, {"error": "need user_id"}
    if method == "GET" and path == "/api/premium/status":
        return 200, premium_status(user_id, now)
    if method != "POST" or path not in ("/api/premium/reserve", "/api/premium/unlink"):
        return 404, {"error": "not found"}
    st = premium_store()
    if path == "/api/premium/unlink":
        link = st.unlink(user_id)
        removed = premium_downgrade(user_id) if link else []
        if link:
            print(f"premium: unlinked {premium.short(link['wallet'])} {premium.log_user(user_id)}", flush=True)
        return 200, {"ok": True, "wallet": link["wallet"] if link else None, "removed": removed,
                     "watch_limit": alerts.WATCH_LIMIT, "watch_days": alerts.WATCH_DAYS}
    wallet = premium.wallet_of(body.get("wallet"))
    from chains import robinhood
    if not wallet or wallet in robinhood.INFRA or wallet in robinhood.ROUTERS or wallet == premium.token():
        return 400, {"error": "not a wallet address"}
    res, link = st.reservation_of(user_id), st.link_of(user_id)
    again = (res and res["wallet"] == wallet) or (link and link["wallet"] == wallet)
    if not again and not PREMIUM_ATTEMPTS.take(user_id, now):
        return 429, {"error": "too many attempts", "message": "Too many verification attempts. Try again later."}
    state, row = st.reserve(user_id, wallet, now)
    if state == "taken":
        return 409, {"error": "taken", "message": PREMIUM_TAKEN, "wallet": wallet}
    if state == "reserved":
        print(f"premium: reserved {premium.short(wallet)} {premium.log_user(user_id)}", flush=True)
    out = {"ok": True, "state": state, "wallet": wallet, "minutes": premium.RESERVE_MIN,
           "min_tokens": premium.min_tokens()}
    return 200, out | ({"expires_at": row["expires_at"]} if state in ("reserved", "pending") else {})


_RESP = {}                 # ключ -> (ts, тело): статусы и конфиг для опросов страницы


def cached_response(key, ttl, fn):
    """Тело ответа из кэша процесса моложе ttl, иначе fn() (исключение не кэшируется — его ловит вызывающий)."""
    now = time.time()
    with _lock:
        hit = _RESP.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    body = fn()
    with _lock:
        _RESP[key] = (now, body)
    return body


def _fresh_now(body):
    """У кэшированного статуса поле now — текущее (бот считает «через 5h 12m» от него)."""
    return body | {"now": rs._iso(time.time())} if isinstance(body, dict) and "now" in body else body


def scan_client(headers, addr):
    """(ключ, лимит в минуту) для лимита новых сканов или None (без лимита). IP — CF-Connecting-IP (сайт за
    Cloudflare), иначе адрес соединения. Верный X-Alerts-Secret — наш бот, без лимита; user agent crawlscan-bot —
    высокий лимит."""
    if alerts_secret_ok(headers.get("X-Alerts-Secret")):
        return None
    ip = (headers.get("CF-Connecting-IP") or "").strip() or addr
    if (headers.get("User-Agent") or "").startswith("crawlscan-bot"):
        return ("bot:" + ip, SCAN_RATE_BOT_PER_MIN)
    return (ip, SCAN_RATE_PER_MIN)


def get_recent(limit):
    """Ответ /api/recent с кэшем RECENT_TTL. Сбой базы — пустая лента, не ошибка."""
    now = time.time()
    with _lock:
        hit = RECENT.get(limit)
        if hit and now - hit[0] < RECENT_TTL:
            return hit[1]
        gen = _RECENT_GEN[0]
    try:
        out = {"items": recent_store().recent(limit)}
    except Exception as e:
        print(f"recent: not read: {type(e).__name__}: {e}", flush=True)
        return {"items": []}
    with _lock:
        if _RECENT_GEN[0] == gen:   # пока читали, скан записался — этот ответ уже старый, в кэш не кладём
            RECENT[limit] = (now, out)
    return out


def admin_ok(header):
    """X-Admin-Token совпадает с ADMIN_TOKEN (сравнение за постоянное время). Не задан ADMIN_TOKEN — всегда нет."""
    token = os.environ.get("ADMIN_TOKEN") or ""
    return bool(token) and bool(header) and hmac.compare_digest(header.encode(), token.encode())


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json", cache="no-store", headers=None):
        b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(b)))
        self.send_header("cache-control", cache)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
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
            body = cached_response(("draw", os.environ.get("DRAW_DB_PATH")), STATUS_TTL,
                                   lambda: ds.status_json(draw_store(), ds.config()))
            return self._send(200, _fresh_now(body), cache=STATUS_CACHE)
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
            body = cached_response(("rewards", os.environ.get("DRAW_DB_PATH"), cfg["enabled"], cfg["token"]), STATUS_TTL,
                                   lambda: rs.status_json(rewards_store(), cfg))
            return self._send(200, _fresh_now(body), cache=STATUS_CACHE)
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

    def _db_guard(self, path, fn, *args):
        """/api/draw/*, /api/rewards/*: сбой базы (database is locked и т.п.) — 503, процесс и сканы живут."""
        try:
            return fn(*args)
        except Exception as e:
            print(f"db: {path} failed: {type(e).__name__}: {e}", flush=True)
            return self._send(503, {"error": "temporarily unavailable"}, headers={"retry-after": str(RETRY_AFTER)})

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

    def _premium(self, method, path, q):
        if not premium.enabled():
            return self._send(404, {"error": "not found"})
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
            return self._send(*premium_api(method, path, body, q))
        except Exception as e:   # сбой базы и т.п.: бот покажет «недоступно»
            print(f"premium: {path} failed: {type(e).__name__}: {e}", flush=True)
            return self._send(500, {"error": "premium temporarily unavailable"})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path.startswith("/api/alerts/"):
            return self._alerts("POST", u.path, parse_qs(u.query))
        if u.path.startswith("/api/premium/"):
            return self._premium("POST", u.path, parse_qs(u.query))
        m = DRAW_PATH.match(urlparse(self.path).path)
        if m and m.group(2) == "payout":
            return self._db_guard(u.path, self._draw_payout, m.group(1))
        if urlparse(self.path).path != "/api/scan":
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            return self._send(200, {"job": start_scan(body.get("token"),
                                                      scan_client(self.headers, self.client_address[0]))})
        except Busy:
            return self._send(503, BUSY, headers={"retry-after": str(RETRY_AFTER)})
        except RateLimited as e:
            return self._send(429, RATE_LIMITED, headers={"retry-after": str(e.args[0])})
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
            return self._db_guard(u.path, self._draw_get, u.path, q)
        if u.path.startswith("/api/rewards/"):
            return self._db_guard(u.path, self._rewards_get, u.path, q)
        if u.path.startswith("/api/alerts/"):
            return self._alerts("GET", u.path, q)
        if u.path.startswith("/api/premium/"):
            return self._premium("GET", u.path, q)
        if u.path == "/api/config":  # фронт и бот: какие сети включены (SOLANA_ENABLED), алерты (ALERTS_ENABLED), Trade on Axiom
            env = tuple(os.environ.get(k) for k in ("SOLANA_ENABLED", "ALERTS_ENABLED", "FLAP_ENABLED", "BANKR_ENABLED",
                                                    "REPLAY_ENABLED", "TRADE_URL_ROBINHOOD", "TRADE_URL_SOLANA",
                                                    "PREMIUM_ENABLED"))
            body = cached_response(("config",) + env, STATUS_TTL, lambda: {
                "solana": engine.solana_enabled(), "alerts": alerts.enabled(), "flap": flap.enabled(),
                "bankr": bankr.enabled(), "replay": replay.enabled(), "trade": trade.templates()}
                | ({"premium": True} if premium.enabled() else {}))   # выключен — ответ как раньше, без поля
            return self._send(200, body, cache=STATUS_CACHE)
        if u.path == "/api/index":   # Bankr: готов ли полный индекс холдеров (сайт перескан делает, когда ready)
            token = (q.get("token") or [""])[0]
            if not bankr.enabled() or not bankr.index_enabled():   # фоновый индекс выключен — его нет
                return self._send(404, {"error": "not found"})
            try:
                chain, ca = engine.chain_of(token)
            except engine.ScanError as e:
                return self._send(400, {"error": str(e)})
            return self._send(200, {"token": ca, **bankr.index_status(ca)} if chain == "robinhood" else
                              {"token": ca, "state": "none", "eta_s": None})
        if u.path in ("/api/replay", "/replay"):   # Rug Replay: выключено — страницы и API нет
            if not replay.enabled():
                return self._send(404, {"error": "not found"})
            if u.path == "/replay":
                return self._send_page(os.path.join(ROOT, "replay.html"))
            try:
                body = cached_response(("replay",), REPLAY_TTL, lambda: replay.api(replay.db_path()))
            except Exception as e:   # сбой базы: не кэшируется, процесс живёт
                print(f"replay: not read: {type(e).__name__}: {e}", flush=True)
                return self._send(503, {"error": "temporarily unavailable"})
            return self._send(200, body, cache=f"public, max-age={REPLAY_TTL}")
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


def recheck_done(result):
    """Принятая перепроверка: снимок alerts и память операторов (та же дедупликация по токену, что у живых)."""
    opmem.record(result)
    record_snapshot(result)


def start_alerts_rechecker():
    """Плановые перепроверки отслеживаемых токенов — только при ALERTS_ENABLED=true."""
    if not alerts.enabled():
        return None
    cfg = alerts_recheck.config()
    print(f"alerts: rechecks every {cfg['recheck_min']} min, max {cfg['max_per_hour']}/hour", flush=True)
    return alerts_recheck.Rechecker(
        alerts_store, recheck_scan, recheck_done, notify_message,
        rate_limited=lambda: sum(a.RATE_LIMITED[0] for a in engine.CHAINS.values()), cfg=cfg,
        memory_high=lambda: memguard.over(live=1)).start()


def start_premium():
    """Premium: поток броней и проверки балансов — только при PREMIUM_ENABLED=true."""
    if not premium.enabled():
        return None
    print(f"premium: on, min {premium.min_tokens():,} tokens, db {premium.db_path()}", flush=True)
    return premium_svc().start()


def start_replay_checker():
    """Rug Replay: фоновая проверка исхода DANGER-токенов — только при REPLAY_ENABLED=true."""
    if not replay.enabled():
        return None
    cfg = replay.config()
    print(f"replay: checks every {cfg['check_min']} min, market cap at scan >= ${cfg['min_mcap']:,.0f}, "
          f"clean max {cfg['max_clean_per_hour']}/hour, db {replay.db_path()}", flush=True)
    if not opmem.enabled():
        print("replay: OPMEM_ENABLED is off, new scans are not recorded: only scans already in memory.db are checked",
              flush=True)
    return replay.Checker(replay.db_path(), cfg).start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"rh-crawler on http://0.0.0.0:{port}", flush=True)
    print(memguard.status_line(0), flush=True)
    memguard.start_monitor(scan_stats)
    start_draw_scheduler()
    start_rewards_scheduler()
    start_alerts_rechecker()
    start_replay_checker()
    start_premium()
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
