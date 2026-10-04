"""HTTP-сервер rh-crawler (стандартная библиотека, ThreadingHTTPServer).
Read-only: скан идёт в фоне, браузер опрашивает события.

  POST /api/scan {"token": "0x..."}      -> {"job": id}
  GET  /api/events?job=ID&after=N        -> {"events": [...], "done": bool}  (события с i >= N)
  GET  /api/result?job=ID                -> {"done", "result" | "error"}
  GET  /                                 -> index.html
  GET  /favicon.svg, /favicon.png, /apple-touch-icon.png, /favicon.ico  -> иконки из static/
  GET  /health

Результат токена кэшируется 10 минут: повторный скан отдаёт сохранённые события сразу.
Не больше 3 сканов одновременно (остальные ждут слота). PORT из env, по умолчанию 8000.
"""
import json, os, threading, time, uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import engine

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

JOBS = {}                  # job_id -> {"token", "events", "done", "result", "error", "ts"}
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
                                      "type": "error", "spider": None, "wallet": None, "detail": err})
            job["done"] = True


def start_scan(token):
    """job_id: свежий кэш по токену или уже идущий скан того же токена, иначе новый."""
    token = engine.validate(token)
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
        JOBS[jid] = {"token": token, "events": [], "done": False, "result": None, "error": None, "ts": now}
        BY_TOKEN[token] = jid
    threading.Thread(target=_run, args=(jid, token), daemon=True).start()
    return jid


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

    def do_POST(self):
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"rh-crawler on http://0.0.0.0:{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
