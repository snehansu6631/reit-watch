"""REIT / InvIT Watch — price & volume spike alerts to Pushover.

Runs inside GitHub Actions. Modes:
  --mode run   : loop through the market session window, alert on triggers
  --mode test  : one pass over all scrips, print a table, send a test alert
"""
import argparse, csv, json, os, subprocess, sys, time, traceback
from datetime import datetime, timedelta, timezone

import requests
import yaml

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(ROOT, "state.json")
LOG_FILE = os.path.join(ROOT, "logs", "alerts.csv")
UA = {"User-Agent": "Mozilla/5.0 (reit-watch personal monitor)"}
SESSION_SPLIT = "12:20"   # morning job runs until here, afternoon job from here


# ---------------------------------------------------------------- helpers
def now_ist():
    return datetime.now(IST)


def hm(s):
    h, m = map(int, s.split(":"))
    return h, m


def at(day, s):
    h, m = hm(s)
    return day.replace(hour=h, minute=m, second=0, microsecond=0)


def load_config():
    with open(os.path.join(ROOT, "config.yaml")) as f:
        return yaml.safe_load(f)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)


def git_commit(msg):
    """Persist state + log to the repo (shares state between the two daily jobs
    and keeps the repo 'active' so GitHub never disables the schedule)."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    try:
        subprocess.run(["git", "add", "state.json", "logs"], cwd=ROOT, check=True)
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode == 0:
            return
        subprocess.run(["git", "commit", "-q", "-m", msg], cwd=ROOT, check=True)
        for _ in range(3):
            subprocess.run(["git", "pull", "-q", "--rebase", "-X", "theirs"], cwd=ROOT)
            if subprocess.run(["git", "push", "-q"], cwd=ROOT).returncode == 0:
                return
            time.sleep(5)
    except Exception as e:
        print("git commit failed:", e)


# ---------------------------------------------------------------- pushover
def push(title, message, priority=0, cfg=None):
    user, token = os.environ.get("PUSHOVER_USER"), os.environ.get("PUSHOVER_TOKEN")
    print(f"[PUSH p{priority}] {title}\n{message}\n")
    if not user or not token:
        print("  (no Pushover keys set — not sent)")
        return False
    data = {"token": token, "user": user, "title": title,
            "message": message[:1000], "priority": priority}
    if priority == 2:
        a = (cfg or {}).get("alerts", {})
        data["retry"] = max(30, int(a.get("emergency_retry_seconds", 60)))
        data["expire"] = min(10800, int(a.get("emergency_expire_seconds", 900)))
    for attempt in range(3):
        try:
            r = requests.post("https://api.pushover.net/1/messages.json", data=data, timeout=20)
            if r.status_code == 200:
                return True
            print("  pushover error", r.status_code, r.text[:200])
            if r.status_code < 500:
                return False
        except Exception as e:
            print("  pushover exception", e)
        time.sleep(5 * (attempt + 1))
    return False


# ---------------------------------------------------------------- data
def fetch_yahoo(code):
    """Return dict with bars (IST date, close, volume) + live meta, or None."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}"
    try:
        r = requests.get(url, params={"range": "3mo", "interval": "1d"}, headers=UA, timeout=20)
        if r.status_code != 200:
            return None
        res = (r.json().get("chart") or {}).get("result")
        if not res:
            return None
        res = res[0]
        meta = res.get("meta", {})
        ts = res.get("timestamp") or []
        q = (res.get("indicators", {}).get("quote") or [{}])[0]
        closes, vols = q.get("close") or [], q.get("volume") or []
        bars = []
        for i, t in enumerate(ts):
            c = closes[i] if i < len(closes) else None
            v = vols[i] if i < len(vols) else None
            if c is None:
                continue
            bars.append((datetime.fromtimestamp(t, IST).date(), float(c), int(v or 0)))
        if not bars or meta.get("regularMarketPrice") is None:
            return None
        return {"code": code, "bars": bars, "price": float(meta["regularMarketPrice"]),
                "mkt_time": datetime.fromtimestamp(meta.get("regularMarketTime", 0), IST),
                "mkt_volume": meta.get("regularMarketVolume")}
    except Exception as e:
        print(f"  fetch {code} failed: {e}")
        return None


def fetch_scrip(sc, cache):
    """NSE first; BSE only if NSE has no data. Remember which one worked."""
    sym = sc["symbol"]
    candidates = [sc.get("nse", f"{sym}.NS"), sc.get("bse", f"{sym}.BO")]
    if sym in cache:                       # try the one that worked last time first
        candidates.sort(key=lambda c: c != cache[sym])
    for code in candidates:
        d = fetch_yahoo(code)
        if d:
            cache[sym] = code
            d["exch"] = "NSE" if code.endswith(".NS") else "BSE"
            return d
    return None


def metrics(sc, d, cfg, now):
    """Compute day %, month %, relative volume. Reported facts vs calculations kept explicit."""
    t = cfg["thresholds"]
    today = now.date()
    bars = d["bars"]
    traded_today = bars[-1][0] == today and d["mkt_time"].date() == today
    hist = bars[:-1] if bars[-1][0] == today else bars
    m = {"price": d["price"], "exch": d["exch"], "code": d["code"],
         "traded_today": traded_today, "day_pct": None, "month_pct": None,
         "rel_vol": None, "today_vol": 0, "avg_vol": None, "avg_value_lakh": None,
         "data_time": d["mkt_time"]}
    if not hist:
        return m
    prev_close = hist[-1][1]
    if traded_today and prev_close:
        m["day_pct"] = (d["price"] / prev_close - 1) * 100
        m["today_vol"] = max(bars[-1][2], int(d.get("mkt_volume") or 0))
    lb = int(t.get("monthly_lookback_days", 21))
    if len(hist) >= lb and hist[-lb][1]:
        m["month_pct"] = (d["price"] / hist[-lb][1] - 1) * 100
    n = int(t.get("volume_avg_days", 20))
    window = hist[-n:]
    if len(window) >= max(5, n // 2):
        avg = sum(b[2] for b in window) / len(window)
        m["avg_vol"] = avg
        m["avg_value_lakh"] = avg * d["price"] / 1e5
        if avg > 0 and traded_today:
            # fraction of session elapsed, measured at the data's own timestamp
            o, c = at(now, cfg["market"]["open"]), at(now, cfg["market"]["close"])
            ref = min(max(d["mkt_time"], o), c)
            frac = max(0.10, (ref - o).total_seconds() / (c - o).total_seconds())
            m["rel_vol"] = m["today_vol"] / (avg * frac)
    return m


# ---------------------------------------------------------------- triggers
def check_triggers(sc, m, cfg, state, now):
    """Return list of (text, priority). Updates state for de-duplication."""
    t, a = cfg["thresholds"], cfg["alerts"]
    sym, out = sc["symbol"], []
    day_key = now.strftime("%Y-%m-%d")
    daily = state.setdefault("daily", {})
    if daily.get("_date") != day_key:
        state["daily"] = daily = {"_date": day_key}
    monthly = state.setdefault("monthly", {})
    pri0 = int(a.get("default_priority", 1))

    # 1) Daily price move — re-alert only as move grows by daily_step_pct
    thr = float(sc.get("daily_pct", t["daily_pct"]))
    step = float(t.get("daily_step_pct", 1.0))
    if m["day_pct"] is not None and abs(m["day_pct"]) >= thr:
        direction = "up" if m["day_pct"] > 0 else "down"
        level = thr + step * int((abs(m["day_pct"]) - thr) // step)
        k = f"{sym}|day|{direction}"
        if level > daily.get(k, 0):
            daily[k] = level
            p = 2 if abs(m["day_pct"]) >= float(a.get("emergency_if_daily_pct", 99)) else pri0
            out.append((f"Price {m['day_pct']:+.2f}% today (vs prev close)", p))

    # 2) Monthly move — persistent across days; resets when move fades below threshold - 1%
    thr = float(sc.get("monthly_pct", t["monthly_pct"]))
    step = float(t.get("monthly_step_pct", 2.0))
    if m["month_pct"] is not None:
        mp = m["month_pct"]
        for direction, sign in (("up", 1), ("down", -1)):
            k = f"{sym}|month|{direction}"
            if sign * mp >= thr:
                level = thr + step * int((abs(mp) - thr) // step)
                if level > monthly.get(k, 0):
                    monthly[k] = level
                    p = 2 if abs(mp) >= float(a.get("emergency_if_monthly_pct", 99)) else pri0
                    out.append((f"Price {mp:+.2f}% over ~1 month", p))
            elif sign * mp < thr - 1 and k in monthly:
                del monthly[k]

    # 3) Volume vs usual-by-this-time
    vthr = float(sc.get("volume_multiple", t["volume_multiple"]))
    vstart = at(now, cfg["market"].get("volume_checks_from", "10:00"))
    liquid = (m["avg_value_lakh"] or 0) >= float(t.get("volume_min_avg_value_lakh", 0))
    if m["rel_vol"] is not None and liquid and now >= vstart and m["rel_vol"] >= vthr:
        k = f"{sym}|vol"
        prev = daily.get(k)
        if prev is None or m["rel_vol"] >= 2 * prev:   # re-alert only if volume doubles again
            daily[k] = m["rel_vol"]
            p = 2 if m["rel_vol"] >= float(a.get("emergency_if_volume_multiple", 99)) else pri0
            out.append((f"Volume {m['rel_vol']:.1f}x usual for this time "
                        f"({m['today_vol']:,} vs avg {m['avg_vol']:,.0f}/day)", p))
    return out


def log_alert(now, sc, m, text, p):
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time_ist", "symbol", "exchange", "price", "day_pct", "month_pct",
                        "rel_volume", "data_time_ist", "alert", "priority"])
        f2 = lambda x: "" if x is None else f"{x:.2f}"
        w.writerow([now.strftime("%Y-%m-%d %H:%M"), sc["symbol"], m["exch"], m["price"],
                    f2(m["day_pct"]), f2(m["month_pct"]), f2(m["rel_vol"]),
                    m["data_time"].strftime("%H:%M"), text, p])


# ---------------------------------------------------------------- one pass
def scan(cfg, state, now, cache):
    results, missing = [], []
    for sc in cfg["scrips"]:
        if sc.get("active", True) is False:
            continue
        d = fetch_scrip(sc, cache)
        if not d:
            missing.append(sc["symbol"])
            continue
        results.append((sc, metrics(sc, d, cfg, now)))
        time.sleep(0.3)
    return results, missing


def fmt_alert(sc, m, hits):
    lines = [f"{sc['name']} ({sc['symbol']} · {m['exch']})",
             f"Price ₹{m['price']:,.2f} · data as of {m['data_time']:%H:%M}"]
    lines += [f"• {h[0]}" for h in hits]
    return "\n".join(lines)


def run(cfg, mode):
    now = now_ist()
    state = load_state()
    cache = state.setdefault("exchange_used", {})

    if mode == "test":
        results, missing = scan(cfg, state, now, cache)
        rows = ["| Scrip | Exch | Price | Day % | 1M % | Rel vol | Avg value ₹L/day |",
                "|---|---|---|---|---|---|---|"]
        f2 = lambda x, s="": "–" if x is None else f"{x:.2f}{s}"
        for sc, m in results:
            rows.append(f"| {sc['symbol']} | {m['exch']} | {m['price']:,.2f} | {f2(m['day_pct'])} | "
                        f"{f2(m['month_pct'])} | {f2(m['rel_vol'], 'x')} | {f2(m['avg_value_lakh'])} |")
        report = "\n".join(rows)
        if missing:
            report += "\n\n**MISSING (fix Yahoo code in config.yaml):** " + ", ".join(missing)
        print(report)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
                f.write("## REIT Watch — test run\n\n" + report + "\n")
        bse = [sc["symbol"] for sc, m in results if m["exch"] == "BSE"]
        msg = (f"{len(results)}/{len(results) + len(missing)} scrips OK."
               + (f"\nUsing BSE: {', '.join(bse)}" if bse else "")
               + (f"\nMISSING: {', '.join(missing)}" if missing else ""))
        push("REIT Watch — test ✅", msg, 1, cfg)
        save_state(state)
        git_commit("test run")
        return

    # ---- live session loop
    mk = cfg["market"]
    open_t, close_t = at(now, mk["open"]), at(now, mk["close"])
    end_t = at(now, SESSION_SPLIT) if now < at(now, "12:00") else close_t + timedelta(minutes=1)
    poll = max(1, int(mk.get("poll_minutes", 5))) * 60
    if now >= end_t:
        print("Outside session window; exiting.")
        return
    if now < open_t:
        wait = (open_t - now).total_seconds()
        print(f"Waiting {wait/60:.0f} min for market open")
        time.sleep(wait)

    first, fail_loops, holiday_checked = True, 0, False
    while now_ist() < end_t:
        now = now_ist()
        results, missing = scan(cfg, state, now, cache)

        # holiday / no-trading detection: after 09:45 nothing traded today -> stop quietly
        if not holiday_checked and now >= at(now, "09:45"):
            holiday_checked = True
            if results and not any(m["traded_today"] for _, m in results):
                print("No trades today on any scrip — market holiday? Exiting.")
                return

        if first and now < at(now, "12:00") and cfg["alerts"].get("morning_heartbeat", True):
            push("REIT Watch is live", f"Monitoring {len(results)} scrips till 15:30."
                 + (f"\nNo data: {', '.join(missing)}" if missing else ""), 0, cfg)
        first = False

        if not results:
            fail_loops += 1
            if fail_loops == 3:
                push("REIT Watch — data problem ⚠️",
                     "No prices received for 3 checks in a row. Monitoring may be impaired.", 1, cfg)
        else:
            fail_loops = 0

        changed = False
        for sc, m in results:
            hits = check_triggers(sc, m, cfg, state, now)
            if hits:
                changed = True
                p = max(h[1] for h in hits)
                push(f"{sc['symbol']} alert", fmt_alert(sc, m, hits), p, cfg)
                for h in hits:
                    log_alert(now, sc, m, h[0], h[1])
        save_state(state)
        if changed:
            git_commit(f"alerts {now:%Y-%m-%d %H:%M}")

        sleep = poll - (now_ist() - now).total_seconds()
        if sleep > 0 and now_ist() + timedelta(seconds=sleep) < end_t:
            time.sleep(sleep)
        elif sleep > 0:
            break
    save_state(state)
    git_commit(f"session end {now_ist():%Y-%m-%d %H:%M}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="run", choices=["run", "test"])
    args = ap.parse_args()
    cfg = load_config()
    try:
        run(cfg, args.mode)
    except Exception:
        tb = traceback.format_exc()
        print(tb)
        push("REIT Watch stopped ❌", "The monitor crashed:\n" + tb[-600:], 1, cfg)
        sys.exit(1)
