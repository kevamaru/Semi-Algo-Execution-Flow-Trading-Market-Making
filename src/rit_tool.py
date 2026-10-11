# -*- coding: utf-8 -*-
"""
RIT FLOW TOOL — Liability Trading 3
An execution exoskeleton: you make the decisions, the tool computes and executes your keystrokes.

INSTALL (once)          :  pip install requests pynput
RUN (VS Code terminal)  :  python rit_tool.py
REQUIREMENTS : RIT client open and connected, API enabled (port 9999).

SAFETY : DRY_RUN = True by default -> NO order is sent, everything is simulated and logged.
Run one full session in DRY_RUN, check the log (rit_logs folder), then set DRY_RUN to False.

HOTKEYS (global: they work even when the RIT window has focus)
  F2  UNWIND one lot, passive  (direction chosen automatically: sells if long, buys if short)
  F3  UNWIND one lot, market   (same, executes immediately)
  F6 / F7  lot size - / +       F8  liquidation algo ON/OFF (active ticker)
  F9  algo speed                F11 inventory-skewed quotes ON/OFF (active ticker)
  F12 KILL: stop everything + cancel all my orders
  Shift+F2 accept the client tender marked ->     Shift+F3 decline that tender
  Shift+F5 cancel all my orders                  Shift+F7 switch active ticker
On a laptop, if the F keys control volume/brightness, also hold Fn.

IMPORTANT : while the algo or the quotes run on a ticker, the tool cancels and re-posts
its orders on that ticker every tick. Do not place manual orders in RIT on that ticker
at the same time.
"""
import csv
import math
import os
import queue
import threading
import time
import tkinter as tk
from collections import deque
from datetime import datetime

import requests
from pynput import keyboard

# ============================ CONFIG ============================
API_KEY = os.environ.get("RIT_API_KEY", "PASTE_YOUR_KEY_HERE")   # RIT client > API info
BASE = "http://localhost:9999/v1"
TICKERS = ["TAME", "CRZY"]
DRY_RUN = True                 # True = simulation, nothing is sent to the market

LOT_STEPS = [1000, 2000, 5000, 10000]
LOT_DEFAULT = 2                # index in LOT_STEPS -> 5,000 at start
MAX_ORDER_FALLBACK = 10000     # used if the case does not provide max_trade_size
CONFIRM_SIZE = 10000           # lot >= this: press twice within 1.5 s
REDUCE_ONLY = True             # manual orders and quotes: only allowed to move position toward zero
TILT_LOSS = 50000              # daily loss beyond this: manual market hotkeys are locked
CLOSE_PENALTY = 1.0            # $ per share still open at the close

DEADLINE_TICK = 290            # the algo targets a flat position before this tick
FINAL_MKT_TICKS = 10           # last N ticks before the deadline: market orders only
AGGR_START_TICKS = 60          # below this many ticks left, the algo gets progressively more aggressive
SPEEDS = {"slow": 1.0, "normal": 1.6, "fast": 2.5}
MIN_CHILD = 1000               # smallest child order sent by the algo
TREND_WINDOW = 10              # ticks used for the trend arrow
TREND_THRESHOLD = 0.03         # mid move (in $) that counts as a trend
TREND_BOOST = 0.3              # trend against you: +30% aggressiveness (in your favour: -30%)
PASSIVE_IMPROVE = True         # spread >= 2 cents: step 1 cent ahead of the queue
PARTICIPATION = 0.25           # share of market volume you can take without too much impact

QUOTE_HALF_SPREAD = 0.02       # skewed quotes: half-spread around mid ($)
QUOTE_SKEW_PER_10K = 0.01      # quote shift per 10,000 shares of position ($)
QUOTE_SKEW_MAX = 0.05

POLL_S = 0.4                   # data refresh interval
ENGINE_S = 1.0                 # algo / quote cycle (about 1 tick)
ORDER_GAP_S = 0.12             # minimum delay between two orders (API rate limit)
TICK_SIZE = 0.01
# ================================================================

COLORS = {"w": "#e6e6e6", "g": "#3ad16b", "r": "#ff5b5b", "o": "#ffb347", "d": "#8a8a8a", "c": "#5fd7ff"}


# ---------------------------- helpers ----------------------------
def fmt(x, d=0):
    if x is None:
        return "-"
    return f"{x:,.{d}f}"


def sfmt(x, d=0):
    return ("+" if x > 0 else "") + fmt(x, d)


def mid_of(sec):
    b, a = sec.get("bid") or 0, sec.get("ask") or 0
    if b and a:
        return (b + a) / 2
    return sec.get("last") or b or a or 0.0


def levels(book, side, my_id):
    """Book levels excluding my own orders. side = 'bids' or 'asks'."""
    out = []
    for o in (book.get(side) or []):
        if my_id is not None and o.get("trader_id") == my_id:
            continue
        q = (o.get("quantity") or 0) - (o.get("quantity_filled") or 0)
        if q > 0:
            out.append((float(o["price"]), int(q)))
    out.sort(key=lambda x: -x[0] if side == "bids" else x[0])
    return out


def walk(lv, qty):
    """Average price to execute qty by walking the book. Returns (vwap, quantity covered)."""
    done, cost = 0, 0.0
    for p, q in lv:
        take = min(q, qty - done)
        cost += take * p
        done += take
        if done >= qty:
            break
    return (cost / done if done else None), done


def working(orders, ticker):
    """Remaining quantity in my open orders: (buys, sells)."""
    buy = sell = 0
    for o in orders:
        if o.get("ticker") != ticker:
            continue
        rem = (o.get("quantity") or 0) - (o.get("quantity_filled") or 0)
        if o.get("action") == "BUY":
            buy += rem
        else:
            sell += rem
    return int(buy), int(sell)


# ---------------------------- RIT API ----------------------------
class APIError(Exception):
    def __init__(self, status, body):
        self.status, self.body = status, body
        super().__init__(f"{status}: {body}")


class RIT:
    """One HTTP session per thread, handles the rate limit (429)."""

    def __init__(self):
        self.local = threading.local()

    def _sess(self):
        s = getattr(self.local, "s", None)
        if s is None:
            s = requests.Session()
            s.headers.update({"X-API-Key": API_KEY})
            self.local.s = s
        return s

    def _req(self, method, path, **params):
        for _ in range(4):
            r = self._sess().request(method, BASE + path, params=params or None, timeout=2)
            if r.status_code == 429:
                wait = 0.2
                try:
                    wait = float(r.json().get("wait", wait))
                except Exception:
                    pass
                time.sleep(min(max(wait, 0.05), 1.0))
                continue
            if r.status_code == 401:
                raise RuntimeError("401: invalid API key (check API_KEY)")
            if r.status_code >= 400:
                body = (r.text or "").strip().replace("\n", " ")[:120]
                raise APIError(r.status_code, body or r.reason)
            return r.json() if r.content else {}
        raise RuntimeError(f"persistent rate limit on {path}")

    def get(self, path, **p):
        return self._req("GET", path, **p)

    def post(self, path, **p):
        return self._req("POST", path, **p)

    def delete(self, path, **p):
        return self._req("DELETE", path, **p)


# ---------------------------- shared state ----------------------------
class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = True
        self.case, self.trader, self.limits = {}, {}, []
        self.secs, self.books, self.orders, self.tenders = {}, {}, [], []
        self.updated, self.error = 0.0, ""
        self.last_tick = -1
        self.mid_hist = {t: deque(maxlen=400) for t in TICKERS}
        self.vol_hist = {t: deque(maxlen=20) for t in TICKERS}
        self.active = TICKERS[0]
        self.lot_idx = LOT_DEFAULT
        self.flatten = {t: False for t in TICKERS}
        self.quote = {t: False for t in TICKERS}
        self.speed = "normal"
        self.events = deque(maxlen=8)


def trend_of(st, t, tick):
    """+1 up, -1 down, 0 flat. Call under st.lock."""
    pts = [m for k, m in st.mid_hist[t] if k >= tick - TREND_WINDOW]
    if len(pts) < 2:
        return 0
    d = pts[-1] - pts[0]
    return 1 if d > TREND_THRESHOLD else (-1 if d < -TREND_THRESHOLD else 0)


def vol_per_tick(st, t):
    """Average market volume traded per tick over the last ticks. Call under st.lock."""
    h = st.vol_hist[t]
    if len(h) < 2:
        return 0.0
    (k0, v0), (k1, v1) = h[0], h[-1]
    return max(0.0, v1 - v0) / max(1, k1 - k0)


class Journal:
    def __init__(self):
        os.makedirs("rit_logs", exist_ok=True)
        self.path = datetime.now().strftime("rit_logs/session_%Y%m%d_%H%M%S.csv")
        self.f = open(self.path, "w", newline="", encoding="utf-8")
        self.w = csv.writer(self.f)
        self.w.writerow(["time", "tick", "event", "ticker", "action", "qty", "price", "position", "detail"])
        self.lock = threading.Lock()

    def write(self, *row):
        with self.lock:
            if self.f.closed:
                return
            self.w.writerow([datetime.now().strftime("%H:%M:%S.%f")[:-3], *row])
            self.f.flush()

    def close(self):
        with self.lock:
            self.f.close()


# ---------------------------- tender evaluation ----------------------------
def eval_tender(td, secs, books, lim, my_id, tick, vpt):
    t = td.get("ticker")
    q = int(td.get("quantity") or 0)
    price = float(td.get("price") or 0)
    sign = 1 if td.get("action") == "BUY" else -1      # BUY = you buy if you accept
    sec = secs.get(t, {})
    pos = int(sec.get("position") or 0)
    mid = mid_of(sec)

    # internalisation: the part of the tender that reduces your current position
    internal = min(q, abs(pos)) if pos * sign < 0 else 0
    rest = q - internal
    edge_int = sign * (mid - price) * internal

    # immediate exit of the rest by walking the book (pessimistic scenario)
    cov, edge_rest = 0, 0.0
    if rest > 0:
        lv = levels(books.get(t, {}), "bids" if sign > 0 else "asks", my_id)
        vw, cov = walk(lv, rest)
        if vw is not None:
            edge_rest = sign * (vw - price) * cov
    edge_dump = edge_int + edge_rest
    edge_mid = sign * (mid - price) * q
    coverage = cov / rest if rest else 1.0

    ticks_left = DEADLINE_TICK - tick
    ticks_need = rest / max(1.0, vpt * PARTICIPATION) if rest else 0.0
    feasible = ticks_need <= ticks_left

    # limits after acceptance
    gross_now = sum(abs(int(s.get("position") or 0)) for s in secs.values())
    net_now = sum(int(s.get("position") or 0) for s in secs.values())
    new_pos = pos + sign * q
    gross_new = gross_now - abs(pos) + abs(new_pos)
    net_new = net_now + sign * q
    breach = False
    if lim:
        breach = gross_new > (lim.get("gross_limit") or 1e18) or abs(net_new) > (lim.get("net_limit") or 1e18)

    if breach:
        verdict, tag = "LIMIT BREACH", "r"
    elif edge_dump > 0 and feasible:
        verdict, tag = "GOOD", "g"
    elif edge_mid > 0 and feasible:
        verdict, tag = "OK IF UNWOUND PASSIVELY", "o"
    elif edge_mid > 0:
        verdict, tag = "EDGE BUT EXIT TOO SLOW", "o"
    else:
        verdict, tag = "BAD", "r"

    return dict(ticker=t, qty=q, price=price, sign=sign, internal=internal, edge_dump=edge_dump,
                edge_mid=edge_mid, coverage=coverage, ticks_need=ticks_need, verdict=verdict,
                tag=tag, breach=breach, gross_new=gross_new, net_new=net_new)


# ---------------------------- the desk: every action ----------------------------
class Desk:
    def __init__(self, api, st, journal):
        self.api, self.st, self.j = api, st, journal
        self.order_lock = threading.Lock()
        self.last_order = 0.0
        self.confirms = {}
        self.comma_price = False        # switched on if RIT refuses "9.21" and wants "9,21"

    @property
    def my_id(self):
        with self.st.lock:
            return self.st.trader.get("trader_id")

    def note(self, msg):
        with self.st.lock:
            self.st.events.appendleft(f"[{self.st.last_tick}] {msg}")

    def tilted(self):
        with self.st.lock:
            nlv = self.st.trader.get("nlv")
        return nlv is not None and nlv <= -TILT_LOSS     # RIT P&L = trader NLV (starts at 0)

    def confirm(self, key, window=1.5):
        now = time.time()
        if now - self.confirms.get(key, 0) < window:
            self.confirms.pop(key, None)
            return True
        self.confirms[key] = now
        return False

    def fresh(self, t):
        """Fresh data (not the display snapshot) right before acting."""
        case = self.api.get("/case")
        sl = self.api.get("/securities", ticker=t)
        sec = sl[0] if isinstance(sl, list) and sl else (sl if isinstance(sl, dict) else {})
        book = self.api.get("/securities/book", ticker=t, limit=40)
        return int(sec.get("position") or 0), sec, book, int(case.get("tick") or 0), case

    @staticmethod
    def max_order(sec):
        return int(sec.get("max_trade_size") or MAX_ORDER_FALLBACK)

    def passive_price(self, book, side):
        bids = levels(book, "bids", self.my_id)
        asks = levels(book, "asks", self.my_id)
        if side == "SELL":
            if not asks:
                return round(bids[0][0] + TICK_SIZE, 2) if bids else None
            p = asks[0][0]
            if PASSIVE_IMPROVE and bids and p - bids[0][0] >= 2 * TICK_SIZE - 1e-9:
                p -= TICK_SIZE
            return round(p, 2)
        if not bids:
            return round(asks[0][0] - TICK_SIZE, 2) if asks else None
        p = bids[0][0]
        if PASSIVE_IMPROVE and asks and asks[0][0] - p >= 2 * TICK_SIZE - 1e-9:
            p += TICK_SIZE
        return round(p, 2)

    def send(self, t, action, qty, otype, price, reason, tick, pos, maxo):
        qty = int(qty)
        with self.order_lock:
            while qty > 0:
                q = min(qty, maxo)
                wait = ORDER_GAP_S - (time.time() - self.last_order)
                if wait > 0:
                    time.sleep(wait)
                ptxt = f" @ {price:.2f}" if otype == "LIMIT" else ""
                if DRY_RUN:
                    self.note(f"[SIM] {action} {fmt(q)} {t} {otype}{ptxt}  ({reason})")
                else:
                    self.post_order(t, action, q, otype, price)
                    self.note(f"{action} {fmt(q)} {t} {otype}{ptxt}  ({reason})")
                self.j.write(tick, "ORDER", t, action, q, round(price, 2) if price else "", pos, reason)
                self.last_order = time.time()
                qty -= q

    def post_order(self, t, action, q, otype, price):
        p = {"ticker": t, "type": otype, "quantity": q, "action": action}
        if otype != "LIMIT":
            return self.api.post("/orders", **p)
        txt = f"{price:.2f}"
        p["price"] = txt.replace(".", ",") if self.comma_price else txt
        try:
            return self.api.post("/orders", **p)
        except APIError as e:
            if e.status != 500:
                raise
            self.comma_price = not self.comma_price            # try the other decimal separator once
            p["price"] = txt.replace(".", ",") if self.comma_price else txt
            r = self.api.post("/orders", **p)
            self.note(f"price format fixed: using '{p['price']}' style from now on")
            return r

    def cancel(self, t=None, quiet=True):
        if DRY_RUN:
            if not quiet:
                self.note("[SIM] cancel " + (t or "all my orders"))
            return
        if t:
            self.api.post("/commands/cancel", ticker=t)
        else:
            self.api.post("/commands/cancel", all=1)
        if not quiet:
            self.note("orders cancelled: " + (t or "all"))

    def cancel_confirmed(self, t, timeout=0.6):
        """Cancel my orders on t, then wait until RIT confirms none are left.
        Returns what is still working (buy, sell) if RIT has not caught up in time."""
        self.cancel(t)
        if DRY_RUN:
            return 0, 0
        deadline = time.time() + timeout
        while True:
            wb, ws = working(self.api.get("/orders", status="OPEN"), t)
            if (wb == 0 and ws == 0) or time.time() > deadline:
                return wb, ws
            time.sleep(0.05)

    # ----- manual hotkeys -----
    def unwind(self, aggressive):
        """One lot toward zero. Direction chosen from the live position."""
        with self.st.lock:
            t = self.st.active
        pos = self.fresh(t)[0]
        if pos == 0:
            self.note(f"{t} already flat, nothing to unwind")
            return
        self.manual("SELL" if pos > 0 else "BUY", aggressive)

    def manual(self, side, aggressive):
        with self.st.lock:
            t, lot = self.st.active, LOT_STEPS[self.st.lot_idx]
        if aggressive and self.tilted():
            self.note(f"BLOCKED: loss > {fmt(TILT_LOSS)}, market hotkeys locked. Use the algo (F8).")
            return
        pos, sec, book, tick, case = self.fresh(t)
        if case.get("status") != "ACTIVE":
            self.note("case not active")
            return
        qty = lot
        if REDUCE_ONLY:
            wb, ws = working(self.api.get("/orders", status="OPEN"), t)
            room = (pos - ws) if side == "SELL" else (-pos - wb)
            if room <= 0:
                self.note(f"BLOCKED: {side} {t} would move you away from zero (prop trade = fine)")
                return
            qty = min(qty, room)
        if qty >= CONFIRM_SIZE and not self.confirm(("manual", side, aggressive, t)):
            self.note(f"Large lot {fmt(qty)}: press again within 1.5 s to confirm")
            return
        maxo = self.max_order(sec)
        if aggressive:
            self.send(t, side, qty, "MARKET", None, "manual market", tick, pos, maxo)
        else:
            price = self.passive_price(book, side)
            if price is None:
                self.note("empty book, no passive price")
                return
            self.send(t, side, qty, "LIMIT", price, "manual passive", tick, pos, maxo)

    # ----- liquidation algo with deadline (TWAP with rising urgency) -----
    def flatten_step(self, t):
        pos, sec, book, tick, case = self.fresh(t)
        if case.get("status") != "ACTIVE":
            return
        if pos == 0:
            with self.st.lock:
                self.st.flatten[t] = False
            self.cancel(t)
            self.note(f"ALGO {t} done: position flat")
            self.j.write(tick, "FLAT_DONE", t, "", "", "", 0, "")
            return
        wb, ws = self.cancel_confirmed(t)
        if not DRY_RUN:
            pos, sec, book, tick, case = self.fresh(t)   # position after the confirmed cancel
            if pos == 0:
                return
        side = "SELL" if pos > 0 else "BUY"
        still = ws if pos > 0 else wb                    # reducing orders RIT has not cancelled yet
        rem, maxo = abs(pos) - still, self.max_order(sec)
        if rem <= 0:
            return                                       # wait: those orders alone would reach zero
        ticks_left = DEADLINE_TICK - tick
        if ticks_left <= 0:
            self.send(t, side, min(rem, maxo), "MARKET", None, "algo deadline", tick, pos, maxo)
            return
        if ticks_left <= FINAL_MKT_TICKS:                        # end of case: no passive, market only
            q = min(rem, maxo, max(MIN_CHILD, math.ceil(rem / ticks_left)))
            self.send(t, side, q, "MARKET", None, f"algo final ({ticks_left} ticks)", tick, pos, maxo)
            return
        with self.st.lock:
            speed = SPEEDS[self.st.speed]
            tr = trend_of(self.st, t, tick)
        rate = rem / ticks_left                                  # shares to unwind per tick
        child = math.ceil(rate * speed * max(1.0, ENGINE_S))
        child = min(max(MIN_CHILD, child), maxo, rem)
        frac = max(0.0, min(1.0, (AGGR_START_TICKS - ticks_left) / AGGR_START_TICKS))
        if (pos > 0 and tr < 0) or (pos < 0 and tr > 0):        # price moving against you
            frac = min(1.0, frac + TREND_BOOST)
        elif (pos > 0 and tr > 0) or (pos < 0 and tr < 0):      # price moving in your favour
            frac = max(0.0, frac - TREND_BOOST)
        aggr = int(round(child * frac))
        passive = min(rem - aggr, child * 2, maxo)               # aggr + passive <= rem: never crosses zero
        if aggr > 0:
            self.send(t, side, aggr, "MARKET", None, f"algo aggressive {int(frac * 100)}%", tick, pos, maxo)
        if passive > 0:
            price = self.passive_price(book, side)
            if price is not None:
                try:
                    self.send(t, side, passive, "LIMIT", price, "algo passive", tick, pos, maxo)
                except APIError as e:                            # server refused the passive order: retry next tick
                    self.note(f"passive order refused by RIT ({e}), algo keeps running")

    # ----- inventory-skewed quotes -----
    def quote_step(self, t):
        pos, sec, book, tick, case = self.fresh(t)
        if case.get("status") != "ACTIVE":
            return
        wb, ws = self.cancel_confirmed(t)
        if not DRY_RUN:
            pos, sec, book, tick, case = self.fresh(t)
        if REDUCE_ONLY and pos == 0:
            return
        if wb or ws:
            return                                       # old quotes still alive: skip this tick
        bids, asks = levels(book, "bids", self.my_id), levels(book, "asks", self.my_id)
        if not bids or not asks:
            return
        bb, ba = bids[0][0], asks[0][0]
        mid = (bb + ba) / 2
        skew = max(-QUOTE_SKEW_MAX, min(QUOTE_SKEW_MAX, -pos / 10000 * QUOTE_SKEW_PER_10K))
        bid = min(round(mid - QUOTE_HALF_SPREAD + skew, 2), round(ba - TICK_SIZE, 2))   # never crosses
        ask = max(round(mid + QUOTE_HALF_SPREAD + skew, 2), round(bb + TICK_SIZE, 2))
        with self.st.lock:
            lot = LOT_STEPS[self.st.lot_idx]
        maxo = self.max_order(sec)
        if pos > 0 or not REDUCE_ONLY:
            q = min(lot, pos) if REDUCE_ONLY else lot
            if q > 0:
                self.send(t, "SELL", q, "LIMIT", ask, "skewed quote", tick, pos, maxo)
        if pos < 0 or not REDUCE_ONLY:
            q = min(lot, -pos) if REDUCE_ONLY else lot
            if q > 0:
                self.send(t, "BUY", q, "LIMIT", bid, "skewed quote", tick, pos, maxo)

    # ----- client tenders -----
    def tender(self, accept):
        tenders = self.api.get("/tenders")
        if not tenders:
            self.note("no client tender right now")
            return
        td = tenders[0]
        tid = td.get("tender_id")
        with self.st.lock:
            tick = self.st.last_tick
            secs = {k: dict(v) for k, v in self.st.secs.items()}
            books = dict(self.st.books)
            lim = self.st.limits[0] if self.st.limits else {}
            vpt = vol_per_tick(self.st, td.get("ticker")) if td.get("ticker") in TICKERS else 0.0
        if not accept:
            if DRY_RUN:
                self.note(f"[SIM] tender #{tid} declined")
            else:
                self.api.delete(f"/tenders/{tid}")
                self.note(f"tender #{tid} declined")
            self.j.write(tick, "TENDER_DECLINE", td.get("ticker"), td.get("action"), td.get("quantity"), td.get("price"), "", "")
            return
        if not td.get("is_fixed_bid", True):
            self.note("open-price tender: accept it directly in RIT")
            return
        ev = eval_tender(td, secs, books, lim, self.my_id, tick, vpt)
        if ev["breach"] and not self.confirm(("tender", tid), window=2.0):
            self.note(f"Tender #{tid} BREACHES YOUR LIMITS. Press Shift+F2 again within 2 s to force.")
            return
        if DRY_RUN:
            self.note(f"[SIM] tender #{tid} accepted ({ev['verdict']})")
        else:
            self.api.post(f"/tenders/{tid}")
            self.note(f"tender #{tid} accepted ({ev['verdict']})")
        self.j.write(tick, "TENDER_ACCEPT", ev["ticker"], td.get("action"), ev["qty"], ev["price"], "",
                     f"edge_exit={ev['edge_dump']:.0f} edge_mid={ev['edge_mid']:.0f} verdict={ev['verdict']}")

    # ----- settings -----
    def cycle_ticker(self):
        with self.st.lock:
            i = TICKERS.index(self.st.active)
            self.st.active = TICKERS[(i + 1) % len(TICKERS)]
            t = self.st.active
        self.note(f"active ticker: {t}")

    def lot_change(self, d):
        with self.st.lock:
            self.st.lot_idx = max(0, min(len(LOT_STEPS) - 1, self.st.lot_idx + d))
            lot = LOT_STEPS[self.st.lot_idx]
        self.note(f"lot: {fmt(lot)}")

    def speed_cycle(self):
        names = list(SPEEDS)
        with self.st.lock:
            self.st.speed = names[(names.index(self.st.speed) + 1) % len(names)]
            s = self.st.speed
        self.note(f"algo speed: {s}")

    def toggle_flatten(self):
        with self.st.lock:
            t = self.st.active
            self.st.flatten[t] = not self.st.flatten[t]
            on = self.st.flatten[t]
            if on:
                self.st.quote[t] = False
            speed = self.st.speed
        self.note(f"ALGO {t} {'ON (' + speed + ')' if on else 'OFF'}")
        if not on:
            self.cancel(t)

    def toggle_quote(self):
        with self.st.lock:
            t = self.st.active
            self.st.quote[t] = not self.st.quote[t]
            on = self.st.quote[t]
            if on:
                self.st.flatten[t] = False
        self.note(f"SKEWED QUOTES {t} {'ON' if on else 'OFF'}")
        if not on:
            self.cancel(t)


# ---------------------------- threads ----------------------------
def poller(api, st, journal):
    while st.running:
        try:
            case = api.get("/case")
            trader = api.get("/trader")
            limits = api.get("/limits")
            secs = {s["ticker"]: s for s in api.get("/securities") if s.get("ticker") in TICKERS}
            orders = api.get("/orders", status="OPEN")
            tenders = api.get("/tenders")
            books = {t: api.get("/securities/book", ticker=t, limit=40) for t in TICKERS}
            with st.lock:
                st.case, st.trader, st.limits = case, trader, limits or []
                st.secs, st.orders, st.tenders, st.books = secs, orders or [], tenders or [], books
                st.updated, st.error = time.time(), ""
                tick = int(case.get("tick") or 0)
                new_tick = tick != st.last_tick
                if tick < st.last_tick:                      # new period / new session
                    for t in TICKERS:
                        st.mid_hist[t].clear()
                        st.vol_hist[t].clear()
                if new_tick:
                    st.last_tick = tick
                    for t, s in secs.items():
                        st.mid_hist[t].append((tick, mid_of(s)))
                        st.vol_hist[t].append((tick, float(s.get("volume") or 0)))
            if new_tick:
                for t, s in secs.items():
                    journal.write(tick, "SNAP", t, "", "", round(mid_of(s), 2), int(s.get("position") or 0),
                                  f"nlv={trader.get('nlv')}")
        except Exception as e:
            with st.lock:
                st.error = str(e)[:140]
        time.sleep(POLL_S)


def engine(desk, st):
    while st.running:
        t0 = time.time()
        for t in TICKERS:
            with st.lock:
                fl, qu = st.flatten[t], st.quote[t]
            try:
                if fl:
                    desk.flatten_step(t)
                elif qu:
                    desk.quote_step(t)
            except Exception as e:
                desk.note(f"ENGINE ERROR {t}: {str(e)[:80]}")
        time.sleep(max(0.05, ENGINE_S - (time.time() - t0)))


def worker(desk, st, actions):
    while st.running:
        try:
            name, fn = actions.get(timeout=0.2)
        except queue.Empty:
            continue
        try:
            fn()
        except Exception as e:
            desk.note(f"ERROR {name}: {str(e)[:80]}")


# ---------------------------- window ----------------------------
class App:
    def __init__(self, st):
        self.st = st
        self.root = tk.Tk()
        self.root.title("RIT Flow Tool — " + ("SIMULATION" if DRY_RUN else "LIVE"))
        self.root.attributes("-topmost", True)
        self.root.geometry("980x500+20+20")
        self.root.configure(bg="black")
        self.txt = tk.Text(self.root, bg="black", fg=COLORS["w"], font=("Consolas", 10),
                           bd=0, highlightthickness=0, wrap="none")
        self.txt.pack(fill="both", expand=True, padx=6, pady=4)
        for tag, col in COLORS.items():
            self.txt.tag_config(tag, foreground=col)
        self.txt.tag_config("b", foreground=COLORS["c"], font=("Consolas", 10, "bold"))
        self.txt.tag_config("R", foreground=COLORS["r"], font=("Consolas", 10, "bold"))

    def render(self):
        st = self.st
        with st.lock:
            case, trader = dict(st.case), dict(st.trader)
            lim = dict(st.limits[0]) if st.limits else {}
            secs = {k: dict(v) for k, v in st.secs.items()}
            books, orders, tenders = dict(st.books), list(st.orders), list(st.tenders)
            age = time.time() - st.updated if st.updated else 99.0
            err, events = st.error, list(st.events)
            active, lot, speed = st.active, LOT_STEPS[st.lot_idx], st.speed
            flat, quo = dict(st.flatten), dict(st.quote)
            tick = int(case.get("tick") or 0)
            trends = {t: trend_of(st, t, tick) for t in TICKERS}
            vpts = {t: vol_per_tick(st, t) for t in TICKERS}
        my_id = trader.get("trader_id")
        pnl = trader.get("nlv") or 0                       # RIT P&L = trader NLV (starts at 0)
        tilted = pnl <= -TILT_LOSS

        out = []

        def s(text, tag="w"):
            out.append((text, tag))

        def nl():
            out.append(("\n", "w"))

        # header
        s("SIMULATION (no order sent)" if DRY_RUN else "LIVE", "o" if DRY_RUN else "R")
        s(f"   tick {tick}/{case.get('ticks_per_period', '?')}  {case.get('status', '?')}   ", "w")
        s(f"P&L {sfmt(pnl)}", "g" if pnl >= 0 else "r")
        if tilted:
            s("   TILT: market hotkeys locked", "R")
        s(f"   data {age:.1f}s", "r" if age > 1.0 else "d")
        nl()
        if err:
            s(f"API ERROR: {err}", "r")
            nl()
        s(f"Active ▶ {active}   Lot {fmt(lot)}   Algo speed {speed}", "b")
        nl()

        # limits
        if lim:
            gl, nlim = lim.get("gross_limit") or 0, lim.get("net_limit") or 0
            g, n = lim.get("gross") or 0, lim.get("net") or 0
            gp = g / gl if gl else 0
            npct = abs(n) / nlim if nlim else 0
            s(f"LIMITS  gross {fmt(g)} / {fmt(gl)} ({gp:.0%})", "r" if gp > 0.8 else "o" if gp > 0.6 else "w")
            s(f"   net {fmt(n)} / {fmt(nlim)} ({npct:.0%})", "r" if npct > 0.8 else "o" if npct > 0.6 else "w")
            nl()
        nl()

        # tickers
        for t in TICKERS:
            sec = secs.get(t)
            if not sec:
                s(f"{t}: no data", "d")
                nl()
                continue
            pos = int(sec.get("position") or 0)
            wb, ws = working(orders, t)
            bid, ask, mid = sec.get("bid") or 0, sec.get("ask") or 0, mid_of(sec)
            tr = trends[t]
            arrow = "↑" if tr > 0 else "↓" if tr < 0 else "→"
            against = (pos > 0 and tr < 0) or (pos < 0 and tr > 0)
            s(("▶ " if t == active else "  ") + f"{t:<5}", "b" if t == active else "w")
            s(f" pos {sfmt(pos):>9}", "w" if pos == 0 else "o")
            s(f"   orders +{fmt(wb)}/-{fmt(ws)}   bid {bid:.2f}  ask {ask:.2f}  mid {mid:.2f} ", "w")
            s(arrow, "r" if against else "g" if tr != 0 else "d")
            s(f"   vol/tick {fmt(vpts[t])}", "d")
            if flat.get(t):
                s("   ALGO ON", "c")
            if quo.get(t):
                s("   QUOTES ON", "c")
            nl()
            if pos != 0:
                side = "bids" if pos > 0 else "asks"
                vw, cov = walk(levels(books.get(t, {}), side, my_id), abs(pos))
                cost = abs(mid - vw) * cov if vw is not None else 0
                covp = cov / abs(pos)
                ticks_zero = abs(pos) / max(1.0, vpts[t] * PARTICIPATION)
                left = DEADLINE_TICK - tick
                s(f"         dump now: cost {fmt(cost)} (book covers {covp:.0%})", "o" if covp < 1 else "w")
                s(f"   penalty if idle: {fmt(abs(pos) * CLOSE_PENALTY)}", "r")
                s(f"   flat in ~{ticks_zero:.0f} ticks ({left} left)", "r" if ticks_zero > left else "w")
                nl()
        nl()

        # client tenders
        s("CLIENT TENDERS", "b")
        nl()
        if not tenders:
            s("  none", "d")
            nl()
        for i, td in enumerate(tenders):
            vpt = vpts.get(td.get("ticker"), 0.0)
            ev = eval_tender(td, secs, books, lim, my_id, tick, vpt)
            act = "BUY" if ev["sign"] > 0 else "SELL"
            s(("→ " if i == 0 else "  ") + f"#{td.get('tender_id')} {ev['ticker']} you {act} {fmt(ev['qty'])} @ {ev['price']:.2f}"
              f" (expires tick {td.get('expires', '?')})", "w")
            nl()
            if ev["internal"]:
                s(f"     MATCH: {fmt(ev['internal'])} reduce your current position", "c")
                nl()
            s(f"     edge if dumped now {sfmt(ev['edge_dump'])} (book {ev['coverage']:.0%})", "g" if ev["edge_dump"] > 0 else "r")
            s(f"   vs mid {sfmt(ev['edge_mid'])}", "g" if ev["edge_mid"] > 0 else "r")
            s(f"   exit ~{ev['ticks_need']:.0f} ticks   ", "w")
            s(ev["verdict"], ev["tag"])
            nl()
        nl()

        # short log
        s("LAST ACTIONS", "b")
        nl()
        for e in events:
            s("  " + e, "o" if "BLOCKED" in e or "ERROR" in e or "BREACH" in e else "w")
            nl()
        nl()
        s("F2 unwind passive  F3 unwind market  F6/F7 lot  F8 algo  F9 speed  "
          "F11 quotes  F12 KILL  |  Shift+F2 accept  Shift+F3 decline  Shift+F5 cancel  Shift+F7 ticker", "d")

        self.txt.config(state="normal")
        self.txt.delete("1.0", "end")
        for text, tag in out:
            self.txt.insert("end", text, tag)
        self.txt.config(state="disabled")
        if self.st.running:
            self.root.after(300, self.render)

    def run(self):
        self.render()
        self.root.mainloop()


# ---------------------------- main ----------------------------
def main():
    if API_KEY == "PASTE_YOUR_KEY_HERE":
        print("Put your API key in API_KEY (top of the file) before running.")
        return
    st, journal, api = State(), Journal(), RIT()
    desk = Desk(api, st, journal)
    actions = queue.Queue()
    last_hit = {}

    def hk(name, fn):
        def cb():
            now = time.time()
            if now - last_hit.get(name, 0) < 0.25:      # anti key-repeat
                return
            last_hit[name] = now
            actions.put((name, fn))
        return cb

    def kill():
        with st.lock:                                   # immediate effect, no queue
            for t in TICKERS:
                st.flatten[t] = False
                st.quote[t] = False
        actions.put(("kill", lambda: (desk.cancel(None), desk.note("KILL: everything stopped, orders cancelled"))))

    mapping = {
        "<f2>": hk("unwind_passive", lambda: desk.unwind(False)),
        "<f3>": hk("unwind_market", lambda: desk.unwind(True)),
        "<f6>": hk("lot_dn", lambda: desk.lot_change(-1)),
        "<f7>": hk("lot_up", lambda: desk.lot_change(1)),
        "<f8>": hk("algo", desk.toggle_flatten),
        "<f9>": hk("speed", desk.speed_cycle),
        "<f11>": hk("quote", desk.toggle_quote),
        "<f12>": kill,
        "<shift>+<f2>": hk("accept", lambda: desk.tender(True)),
        "<shift>+<f3>": hk("decline", lambda: desk.tender(False)),
        "<shift>+<f5>": hk("cancel", lambda: desk.cancel(None, quiet=False)),
        "<shift>+<f7>": hk("ticker", desk.cycle_ticker),
    }

    for target, args in ((poller, (api, st, journal)), (engine, (desk, st)), (worker, (desk, st, actions))):
        threading.Thread(target=target, args=args, daemon=True).start()
    listener = keyboard.GlobalHotKeys(mapping)
    listener.start()

    print(f"RIT Flow Tool started ({'SIMULATION' if DRY_RUN else 'LIVE'}). Log: {journal.path}")
    app = App(st)

    def on_close():
        st.running = False
        try:
            listener.stop()
        except Exception:
            pass
        if not DRY_RUN:
            try:
                api.post("/commands/cancel", all=1)
            except Exception:
                pass
        journal.close()
        app.root.destroy()

    app.root.protocol("WM_DELETE_WINDOW", on_close)
    app.run()


if __name__ == "__main__":
    main()
