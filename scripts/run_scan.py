"""Scan tokens via engine.scan: events are printed as they come in, then the totals.
Usage: python scripts/run_scan.py 0x... [0x... ...]"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engine


def short(a):
    return f"{a[:6]}…{a[-4:]}" if a else ""


def show(e):
    extra = ""
    if e["type"] == "link":
        extra = f" — {short(e['b'])} [{e['level']}]"
    if e["type"] == "done":
        extra = f"  score={e['score']} band={e['band']} | {e['headline']}"
    sp = "" if e["spider"] is None else f" spider {e['spider']}"
    print(f"  #{e['i']:<3} {e['t'] / 1000:6.2f}s {e['type']:<12}{sp:<8} {short(e['wallet']):<12} {e['detail']}{extra}",
          flush=True)


summary = []
for tok in sys.argv[1:]:
    print(f"=== {tok}")
    t0, n = time.time(), [0]
    try:
        r = engine.scan(tok, emit=lambda e: (n.__setitem__(0, n[0] + 1), show(e)))
    except engine.ScanError as e:
        print(f"  error: {e}\n")
        summary.append((tok, time.time() - t0, n[0], None, "ERROR", str(e)))
        continue
    h = r["header"]
    print(f"  header: {h}")
    print(f"  unread: {len(r['unread'])}, RPC requests: {r['rpc_requests']}, reason: {r['reason']}")
    rug = r.get("rug")
    if rug:
        parts = ", ".join(f"{p['kind']} {len(p['wallets'])} ({p['share'] * 100:.1f}%)" for p in rug["parts"])
        lvl = "" if rug["level_usd"] is None else f", level ${rug['level_usd']:.8g}"
        print(f"  probably rug: −{rug['drop'] * 100:.1f}%{lvl}; reserve {rug['share'] * 100:.1f}% of volume: {parts}")
    else:
        print("  probably rug: no")
    print(f"  total: {time.time() - t0:.1f} s\n")
    summary.append((tok, time.time() - t0, n[0], r["score"], r["band"], r["headline"]))

print("Summary")
for tok, dt, n, s, b, h in summary:
    print(f"  {short(tok)}  {dt:5.1f} s  events {n:<3} score {s}  {b:<18} {h}")
