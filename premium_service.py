"""Premium: сеть (только через chains/robinhood.py) и фоновый поток "premium". Только при PREMIUM_ENABLED.

Раз в POLL_S секунд, если есть брони (нет броней — ни одного RPC):
  - eth_blockNumber и один eth_getLogs по Transfer токена с получателем = OR-список всех забронированных кошельков
    (get_token_transfers(..., to=[...])), с блока, до которого прочитано (новая бронь — с BACK_BLOCKS до головы:
    покупка могла попасть между бронью и первым проходом); время блока — из лога (blockTimestamp);
  - покупка = перевод на кошелёк внутри брони по времени блока (premium.counts_as_buy):
    (а) от пула или роутера токена (ch.market_addresses: кривая, роутеры Pons, V4 PoolManager) или от известного
        роутера сети из адаптеров Flap / Bankr (bankr.ROUTERS) — без лишних запросов;
    (б) от любого другого адреса (приложение, агрегатор), если в той же транзакции есть Transfer токена от пула
        (кривая или V4 PoolManager: токены вышли из пула) и своп в пуле (V4 Swap PoolManager или CurveBuy кривой;
        выход из пула без свопа — вывод ликвидности — не покупка) — по чеку: один HTTP-батч eth_getTransactionReceipt на
        такие переводы за проход (ch.receipt_logs, кэш); чека ещё нет — перевод перечитается в следующем проходе;
    перевод с обычного кошелька (из пула в транзакции ничего не выходило) не засчитывается;
  - нашли — balanceOf кошелька (1 eth_call), привязка, сообщение; бронь прошла (и прочитан блок после её конца) —
    снимается, сообщение "expired".
Раз в BALANCE_TICK секунд (фоном, priority.background(): уступает живым сканам) — балансы привязок, которым пора
(раз в сутки), батчами по BATCH eth_call: события premium.balance_event (on / paused / trim) с сообщениями;
trim — Watchlist обрезается до обычного (downgrade). Сообщения шлёт сайт (notify, тот же отправщик, что у alerts).
"""
import threading, time, traceback

import premium
from chains import bankr, priority

POLL_S = 15            # секунд между проходами по броням
BALANCE_TICK = 600     # секунд между проходами по балансам (сама проверка у привязки — раз в сутки)
BACK_BLOCKS = 2000     # новая бронь: логи читаются с head − BACK_BLOCKS (по времени отсекает counts_as_buy)
FINAL_S = 30           # бронь снимается, когда после её конца прошло столько секунд (нода успела отдать блоки)
BATCH = 50             # eth_call balanceOf в одном HTTP


class Service:
    def __init__(self, store, notify, upgrade, downgrade, chain, clock=time.time, sleep=time.sleep, log=None,
                 threshold=None, token=None):
        """store — PremiumStore; notify(user_id, html) — сообщение в Telegram; upgrade(user_id) — подписки без срока;
        downgrade(user_id) → [токены, с которых сняты подписки] — Watchlist как у обычных; chain — адаптер Robinhood."""
        self.store, self.notify, self.upgrade, self.downgrade, self.ch = store, notify, upgrade, downgrade, chain
        self.clock, self.sleep = clock, sleep
        self.log = log or (lambda m: print(m, flush=True))
        self.threshold = threshold or premium.min_tokens()
        self.token = (token or premium.token()).lower()
        self._senders, self._pools, self._swaps, self._decimals = None, None, None, None
        self.balance_at = 0.0
        self.thread = None

    # --- сеть ---------------------------------------------------------------------------------------

    def senders(self):
        """Пул и роутеры токена: перевод от них — покупка. Запуск читается один раз (кэш адаптера)."""
        if self._senders is None:
            launch = self.ch.get_launch(self.token)
            curve = launch.get("curve") if launch else None
            self._senders = (self.ch.market_addresses(curve) if curve
                             else set(self.ch.ROUTERS) | {self.ch.V4_POOL_MGR}) | bankr.ROUTERS
            self._pools = {self.ch.V4_POOL_MGR} | ({curve.lower()} if curve else set())
            self._swaps = {(self.ch.V4_POOL_MGR, self.ch.V4_SWAP_TOPIC)} | (
                {(curve.lower(), self.ch.CURVE_BUY)} if curve else set())
        return self._senders

    def pools(self):
        """Пул токена (кривая и V4 PoolManager): Transfer токена от него в транзакции — токены вышли из пула."""
        self.senders()
        return self._pools

    def swaps(self):
        """Своп в пуле: {(адрес, topic0)} — V4 Swap PoolManager, CurveBuy кривой."""
        self.senders()
        return self._swaps

    def decimals(self):
        if self._decimals is None:
            self._decimals = self.ch.token_decimals(self.token)
        return self._decimals

    def threshold_raw(self):
        return self.threshold * 10 ** self.decimals()

    def balances(self, wallets):
        """{кошелёк: баланс} батчами eth_call balanceOf; не ответил — кошелька нет в ответе."""
        out = {}
        for i in range(0, len(wallets), BATCH):
            part = wallets[i:i + BATCH]
            calls = [("eth_call", [{"to": self.token, "data": "0x70a08231" + "0" * 24 + w[2:]}, "latest"]) for w in part]
            for w, r in zip(part, self.ch.rpc_batch(calls)):
                if r is not None:
                    out[w] = int(r, 16) if r not in ("0x", "") else 0
        return out

    # --- поток --------------------------------------------------------------------------------------

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self.run, daemon=True, name="premium")
            self.thread.start()
        return self

    def run(self):
        while True:
            try:
                self.tick()
            except Exception:
                self.log("premium: loop:\n" + traceback.format_exc())
            self.sleep(POLL_S)

    def tick(self):
        self.verify_tick()
        if self.clock() - self.balance_at >= BALANCE_TICK:
            self.balance_at = self.clock()
            with priority.background():
                self.balance_tick()

    # --- брони --------------------------------------------------------------------------------------

    def verify_tick(self):
        """Один проход по броням. → сколько привязано."""
        res = self.store.reservations()
        if not res:
            return 0
        now = int(self.clock())
        head = self.ch.block_number()
        by_wallet = {r["wallet"]: r for r in res}
        lo = max(0, min(r["scanned_to"] + 1 if r["scanned_to"] is not None else head - BACK_BLOCKS for r in res))
        trs = self.ch.get_token_transfers(self.token, lo, head, to=sorted(by_wallet)) if lo <= head else []
        need = sorted({t["block"] for t in trs if t.get("ts") is None})
        if need:
            ts = self.ch.block_timestamps(need)
            for t in trs:
                if t.get("ts") is None:
                    t["ts"] = ts.get(t["block"])
        senders, done, other = self.senders(), set(), []
        for t in trs:
            r = by_wallet.get(t["to"])
            if not r or r["wallet"] in done or not premium.in_window(t, r):
                continue
            if premium.counts_as_buy(t, r, senders):
                done.add(r["wallet"])
                self.verified(r, t, now)
            else:
                other.append((t, r))           # не от пула / роутера: покупка ли — по чеку транзакции
        other = [(t, r) for t, r in other if r["wallet"] not in done]
        retry = set()
        if other:
            logs = self.ch.receipt_logs(sorted({t["tx"] for t, _ in other}))
            for t, r in other:
                if r["wallet"] in done:
                    continue
                if not logs.get(t["tx"]):          # чека ещё нет: перечитать в следующем проходе
                    retry.add(r["wallet"])
                elif premium.counts_as_buy(t, r, senders, premium.pool_out(logs[t["tx"]], self.token, self.pools(), self.swaps())):
                    done.add(r["wallet"])
                    self.verified(r, t, now)
        left = [w for w in by_wallet if w not in done]
        self.store.set_scanned([w for w in left if w not in retry], head)
        for w in left:
            r = by_wallet[w]
            if now >= r["expires_at"] + FINAL_S and self.store.drop_reservation(w):
                self.log(f"premium: reservation expired {premium.short(w)} {premium.log_user(r['user_id'])}")
                self.notify(r["user_id"], premium.expired_message(w))
        return len(done)

    def verified(self, r, t, now):
        balance = self.ch.token_balance(self.token, r["wallet"])
        link, old = self.store.link(r, t["tx"], balance, self.threshold_raw(), now)
        if not link:
            return
        self.log(f"premium: verified {premium.short(r['wallet'])} {premium.log_user(r['user_id'])} "
                 f"tx {t['tx']} premium {link['premium']}")
        self.notify(r["user_id"], premium.verified_message(r["wallet"], balance, self.decimals(), self.threshold, old))
        if link["premium"]:
            self.upgrade(r["user_id"])

    # --- балансы ------------------------------------------------------------------------------------

    def balance_tick(self):
        """Проверка балансов, которым пора (раз в сутки). → [(кошелёк, событие)]."""
        now = int(self.clock())
        due = self.store.due(now)
        if not due:
            return []
        bal = self.balances([l["wallet"] for l in due])
        thr, events = self.threshold_raw(), []
        for link in due:
            b = bal.get(link["wallet"])
            if b is None:            # нода не ответила — повтор при следующем проходе
                continue
            fields, ev = premium.balance_event(link, b, thr, now)
            self.store.checked(link["wallet"], b, now, fields)
            if ev:
                events.append((link["wallet"], ev))
                self.log(f"premium: {ev} {premium.short(link['wallet'])} {premium.log_user(link['user_id'])}")
            user, w = link["user_id"], link["wallet"]
            if ev == "on":
                self.upgrade(user)
                self.notify(user, premium.on_message(w, b, self.decimals()))
            elif ev == "paused":
                self.notify(user, premium.paused_message(w, b, self.decimals(), self.threshold))
            elif ev == "trim":
                self.notify(user, premium.trim_message(w, self.threshold, self.downgrade(user)))
        self.log(f"premium: balances checked {len(bal)}/{len(due)}")
        return events
