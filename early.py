"""Early buyers: первые 20 уникальных покупателей токена после запуска и что они сделали с ним сейчас.
Отдельно от скана (GET /api/early, фронт зовёт после вердикта): скан не ждёт и не замедляется.

Сеть — адаптер (early_buyers: кто купил первым, не меняется; early_status: балансы и выходы сейчас),
статусы и итог — detect.early_report (чистые функции). Здесь — кэш, лимиты и бюджет:
  - покупатели и их входы — по токену на BUYERS_TTL (сутки), статусы — на STATUS_TTL (10 мин);
  - сбой не кэшируется: отдаётся последний удачный ответ моложе STALE_TTL с полем stale_at;
  - не больше MAX_CONCURRENT расчётов одновременно, один расчёт на токен (остальные ждут его);
  - бюджет BUDGET секунд на расчёт: не успели — partial (часть выходов не разобрана);
  - TOO ESTABLISHED (вердикт в market-кэше) — не считаем.
"""
import threading, time

import detect as d
import engine
import market
from chains import flap, priority

N = 20                    # покупателей
BUDGET = 10.0             # секунд на расчёт (сеть)
BUYERS_TTL = 24 * 3600    # секунд: покупатели и входы не меняются
STATUS_TTL = 600          # секунд: балансы и выходы
STALE_TTL = 6 * 3600      # секунд: последний удачный ответ при сбое
MAX_CONCURRENT = 2        # расчётов одновременно (общий лимитер RPC со сканами)
CACHE_MAX = 500           # токенов в кэше

_BUYERS, _STATUS, _INFLIGHT = {}, {}, {}   # key -> (ts, data); key -> (ts, data, status); key -> Event
_lock = threading.Lock()
_sem = threading.BoundedSemaphore(MAX_CONCURRENT)


def background(chain, token):
    """Расчёт без истории живого скана (адаптер её хранит, но скана моложе SCAN_TTL нет) — фоновая работа:
    уступает живым сканам и идёт под BACKGROUND_RPS (server: priority.background() вместо live())."""
    if flap.candidate(token):   # Flap: истории скана для early нет (как у Solana) — расчёт живой
        return False
    a = engine.CHAINS[chain]
    return hasattr(a, "scan_history") and a.scan_history(token) is None


def budget(a):
    """Бюджет расчёта: у фонового — во столько раз больше, во сколько BACKGROUND_RPS ниже лимита адаптера."""
    return BUDGET * (priority.background_scale(a.RPS) if priority.is_background() else 1.0)


def clear_cache():
    with _lock:
        _BUYERS.clear()
        _STATUS.clear()


def _put(cache, key, value):
    with _lock:
        cache[key] = value
        if len(cache) > CACHE_MAX:
            for k, _ in sorted(cache.items(), key=lambda kv: kv[1][0])[:len(cache) - CACHE_MAX]:
                del cache[k]


def _out(base, hit, flags, stale=False):
    ts, data, status = hit
    out = base | {"available": True, "launch_ts": data["launch"]["ts"], "block": data["launch"]["block"],
                  "updated_at": int(ts)} | d.early_report(data, status, flags)
    if stale:
        out["stale_at"] = int(ts)
    return out


def _compute(a, key, token):
    """(ts, data, status) или None (токен не с лаунчпада). Исключения — наверх (сбой сети)."""
    if flap.candidate(token) and flap.detect(token) is not None:   # Flap (FLAP_ENABLED): один HTTP, Pons — без запросов
        a = flap
    t0, r0 = time.time(), a.REQUESTS[0]
    deadline = t0 + budget(a)
    with _lock:
        b = _BUYERS.get(key)
    data = b[1] if b and t0 - b[0] < BUYERS_TTL else None
    if data is None:
        data = a.early_buyers(token, N, deadline)
        if data is None:
            return None
        if len(data["buyers"]) >= N:   # первые N уже не изменятся; меньше — у свежего токена будут ещё
            _put(_BUYERS, key, (time.time(), data))
    status = a.early_status(token, data["buyers"], deadline, launch=data["launch"])
    hit = (time.time(), data, status)
    _put(_STATUS, key, hit)
    print(f"early: {token} {key[0]} {len(data['buyers'])} buyers, {a.REQUESTS[0] - r0} HTTP, "
          f"{time.time() - t0:.1f}s" + (" (partial)" if any(s["partial"] for s in status["wallets"].values()) else ""),
          flush=True)
    return hit


def get(token, flags=None):
    """Ответ /api/early. token — CA (ScanError при плохом адресе — наверх, это 400).
    flags — {кошелёк: [флаги]} из последнего скана токена (топ-20), см. server.scan_flags."""
    chain, token = engine.chain_of(token)
    base = {"token": token, "chain": chain}
    a = engine.CHAINS[chain]
    if not hasattr(a, "early_buyers"):
        return base | {"available": False, "reason": "not supported for this chain yet"}
    if market.established_get(token, engine.GT_NETWORK[chain]) is not None:
        return base | {"available": False, "reason": "too established"}
    key = (chain, token)
    with _lock:
        hit = _STATUS.get(key)
        if hit and time.time() - hit[0] < STATUS_TTL:
            return _out(base, hit, flags)
        ev = _INFLIGHT.get(key)
        owner = ev is None
        if owner:
            ev = _INFLIGHT[key] = threading.Event()
    if not owner:   # тот же токен уже считается — ждём его результат
        ev.wait(BUDGET * priority.background_scale(a.RPS) + 5)   # владелец мог считать в фоне
        with _lock:
            hit = _STATUS.get(key)
        if hit and time.time() - hit[0] < STATUS_TTL:
            return _out(base, hit, flags)
        return _fallback(base, key, flags, "busy")
    try:
        if not _sem.acquire(timeout=BUDGET):
            return _fallback(base, key, flags, "busy")
        try:
            res = _compute(a, key, token)
        finally:
            _sem.release()
        if res is None:
            return base | {"available": False, "reason": engine.NOT_LAUNCHPAD_FLAP if chain == "robinhood" and flap.enabled()
                           else engine.NOT_LAUNCHPAD[chain]}
        return _out(base, res, flags)
    except Exception as e:
        print(f"early: {token} failed: {type(e).__name__}: {e}", flush=True)
        if "history too long" in str(e):
            return base | {"available": False, "reason": "launch history too long"}
        return _fallback(base, key, flags, "temporarily unavailable")
    finally:
        with _lock:
            _INFLIGHT.pop(key, None)
        ev.set()


def _fallback(base, key, flags, err):
    """Последний удачный ответ моложе STALE_TTL (со stale_at) или ошибка."""
    with _lock:
        hit = _STATUS.get(key)
    if hit and time.time() - hit[0] < STALE_TTL:
        return _out(base, hit, flags, stale=True)
    return base | {"available": True, "error": err}


def known_share(chain, token):
    """Доля сапплая у первых N покупателей сейчас из последнего удачного расчёта моложе STALE_TTL, без сети;
    не считали — None (для снимков alerts)."""
    with _lock:
        hit = _STATUS.get((chain, token))
    if not hit or time.time() - hit[0] >= STALE_TTL:
        return None
    return d.early_report(hit[1], hit[2])["summary"]["now_share_supply"]
