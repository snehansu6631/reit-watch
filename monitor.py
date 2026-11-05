"""REIT / InvIT Watch — price & volume spike alerts to Pushover.

Runs inside GitHub Actions. Modes:
  --mode run   : loop through the market session window, alert on triggers
  --mode test  : one pass over all scrips, print a table, send a test alert
"""
import argparse, csv, io, json, os, subprocess, sys, time, traceback, zipfile
from datetime import datetime, timedelta, timezone

import requests
import yaml

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(ROOT, "state.json")
LOG_FILE = os.path.join(ROOT, "logs", "alerts.csv")
HIST_FILE = os.path.join(ROOT, "nse_history.json")   # NSE daily close+volume per scrip
NSE_HIST = {"data": {}, "loaded": [], "no_file": []}  # filled at start of each run
LAST_NSE = {}                                         # today's NSE figures from Yahoo, for EOD capture
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
        paths = [p for p in ("state.json", "logs", "nse_history.json") if os.path.exists(os.path.join(ROOT, p))]
        if not paths:
            return
        subprocess.run(["git", "add", *paths], cwd=ROOT, check=True)
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
def fetch_yahoo_once(code):
    """Return dict with bars (IST date, close, volume) + live meta, or None."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}"
    try:
        r = requests.get(url, params={"range": "6mo", "interval": "1d"}, headers=UA, timeout=20)
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


def fetch_yahoo(code, tries=3):
    """Retry a couple of times — Yahoo occasionally drops a request."""
    for i in range(tries):
        d = fetch_yahoo_once(code)
        if d:
            return d
        if i < tries - 1:
            time.sleep(2)
    return None


# ---------------------------------------------------------------- NSE daily history
# Yahoo's NSE REIT/InvIT codes return only today's bar, so past NSE volumes come from
# (a) NSE's official daily bhavcopy files (nsearchives), and (b) as a backup, the
# script's own end-of-day capture. Stored in nse_history.json in the repo.
NSE_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
          "Accept": "*/*", "Referer": "https://www.nseindia.com/"}


def load_hist():
    global NSE_HIST
    try:
        with open(HIST_FILE) as f:
            NSE_HIST = json.load(f)
    except Exception:
        NSE_HIST = {"data": {}, "loaded": [], "no_file": []}
    for k in ("data", "loaded", "no_file"):
        NSE_HIST.setdefault(k, {} if k == "data" else [])


def save_hist():
    cutoff = (now_ist() - timedelta(days=75)).strftime("%Y-%m-%d")
    for sym in NSE_HIST["data"]:
        NSE_HIST["data"][sym] = {d: v for d, v in NSE_HIST["data"][sym].items() if d >= cutoff}
    NSE_HIST["loaded"] = sorted(d for d in NSE_HIST["loaded"] if d >= cutoff)
    NSE_HIST["no_file"] = sorted(d for d in NSE_HIST["no_file"] if d >= cutoff)
    with open(HIST_FILE, "w") as f:
        json.dump(NSE_HIST, f, indent=0, sort_keys=True)


def _col(header, *names):
    h = [c.strip().upper() for c in header]
    for n in names:
        if n.upper() in h:
            return h.index(n.upper())
    return None


def fetch_bhavcopy(day):
    """Return {(SYMBOL, SERIES): (close, volume)} for one trading day, None if unavailable."""
    ymd, dmy = day.strftime("%Y%m%d"), day.strftime("%d%m%Y")
    urls = [f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip",
            f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{dmy}.csv"]
    for url in urls:
        try:
            r = requests.get(url, headers=NSE_UA, timeout=30)
            if r.status_code != 200 or len(r.content) < 200:
                continue
            if url.endswith(".zip"):
                z = zipfile.ZipFile(io.BytesIO(r.content))
                text = z.read(z.namelist()[0]).decode("utf-8", "ignore")
            else:
                text = r.content.decode("utf-8", "ignore")
            rows = list(csv.reader(io.StringIO(text)))
            hdr = rows[0]
            iS, iR = _col(hdr, "TckrSymb", "SYMBOL"), _col(hdr, "SctySrs", "SERIES")
            iC, iV = _col(hdr, "ClsPric", "CLOSE_PRICE"), _col(hdr, "TtlTradgVol", "TTL_TRD_QNTY")
            if None in (iS, iR, iC, iV):
                print(f"  bhavcopy {ymd}: unexpected columns {hdr[:12]}")
                continue
            out = {}
            for row in rows[1:]:
                try:
                    out[(row[iS].strip(), row[iR].strip())] = (float(row[iC]), int(float(row[iV])))
                except Exception:
                    pass
            return out
        except Exception as e:
            print(f"  bhavcopy {ymd} {url[-30:]} failed: {e}")
    return None


def update_hist_from_bhavcopy(cfg, days_needed=30, max_downloads=40):
    """Load any missing trading days (weekdays) of the last ~45 calendar days."""
    wanted = {(sc["symbol"], sc.get("series")) for sc in cfg["scrips"]}
    today = now_ist().date()
    got, failed, tried = 0, 0, 0
    for back in range(1, 46):
        if len(NSE_HIST["loaded"]) >= days_needed and back > 7:
            break
        day = today - timedelta(days=back)
        key = day.strftime("%Y-%m-%d")
        if day.weekday() >= 5 or key in NSE_HIST["loaded"] or key in NSE_HIST["no_file"]:
            continue
        if tried >= max_downloads:
            break
        tried += 1
        data = fetch_bhavcopy(day)
        if data is None:
            failed += 1
            if back > 4:                   # older date with no file = holiday (recent ones may just be late)
                NSE_HIST["no_file"].append(key)
            if failed >= 6 and got == 0:  # NSE is blocking us — stop trying this run
                print("  NSE archives not reachable from here; relying on end-of-day capture")
                break
            continue
        got += 1
        NSE_HIST["loaded"].append(key)
        for (sym, ser), (c, v) in data.items():
            for wsym, wser in wanted:
                if sym == wsym and (wser is None or ser == wser):
                    NSE_HIST["data"].setdefault(wsym, {})[key] = [c, v, "bhav"]
        time.sleep(0.5)
    print(f"NSE bhavcopy: loaded {got} new day(s), {len(NSE_HIST['loaded'])} days stored, {failed} unavailable")
    return got, failed


def capture_eod():
    """Backup: store today's final NSE close+volume seen on Yahoo (bhavcopy overwrites later)."""
    for sym, (d, c, v) in LAST_NSE.items():
        key = d.strftime("%Y-%m-%d")
        rec = NSE_HIST["data"].setdefault(sym, {})
        if key not in rec or rec[key][2] != "bhav":
            rec[key] = [c, v, "eod"]


def fetch_scrip(sc, cache, cfg=None):
    """Price from NSE whenever NSE has current data; BSE price only as fallback.
    Yahoo lists NSE REITs/InvITs with the series attached (EMBASSY-RR.NS, INDIGRID-IV.NS);
    the plain code (INDIGRID.NS) can be an old, stale listing — so it is tried last and
    rejected if its data is older than BSE's. Volume = NSE + BSE combined (default on)."""
    sym = sc["symbol"]
    combine = ((cfg or {}).get("market", {}) or {}).get("combine_bse_volume", True)
    series = sc.get("series")
    if sc.get("nse"):
        nse_codes = [sc["nse"]]
    else:
        nse_codes = ([f"{sym}-{series}.NS"] if series else []) + [f"{sym}.NS"]
    bse = fetch_yahoo(sc.get("bse", f"{sym}.BO"), tries=2)
    nse = None
    for i, code in enumerate(nse_codes):
        d = fetch_yahoo(code, tries=3 if i == 0 else 1)
        if not d:
            continue
        if bse and d["mkt_time"].date() < bse["mkt_time"].date() - timedelta(days=5):
            print(f"  {code}: stale NSE listing (last {d['mkt_time']:%Y-%m-%d}) — skipped")
            continue
        nse = d
        break
    if nse:
        today_d = nse["mkt_time"].date()
        tb = [b for b in nse["bars"] if b[0] == today_d]
        if tb:
            LAST_NSE[sym] = (today_d, nse["price"], max(tb[-1][2], int(nse.get("mkt_volume") or 0)))
        have = {b[0] for b in nse["bars"]}
        for k, (c, v, _src) in NSE_HIST["data"].get(sym, {}).items():
            d = datetime.strptime(k, "%Y-%m-%d").date()
            if d < today_d and d not in have:
                nse["bars"].append((d, float(c), int(v)))
        nse["bars"].sort(key=lambda b: b[0])
    primary, other = (nse, bse) if nse else (bse, None)
    if not primary:
        return None
    primary["exch"] = "NSE" if nse else "BSE"
    primary["vol_src"] = primary["exch"]
    primary["hist_note"] = f"{len(primary['bars'])}" + (f"/{len(other['bars'])}" if other else "")
    if other and combine:
        # Union of dates: NSE close where NSE has the day, BSE close only to fill gaps
        # in NSE history; volume always NSE + BSE for each day.
        pc = {d: (c, v) for d, c, v in primary["bars"]}
        oc = {d: (c, v) for d, c, v in other["bars"]}
        merged = []
        for d in sorted(set(pc) | set(oc)):
            if d > primary["mkt_time"].date():
                continue
            c = pc[d][0] if d in pc else oc[d][0]
            # day missing on NSE -> volume unknown (-1), excluded from the volume average
            v = (pc[d][1] + (oc[d][1] if d in oc else 0)) if d in pc else -1
            merged.append((d, c, v))
        primary["bars"] = merged
        same_day = other["mkt_time"].date() == primary["mkt_time"].date()
        if primary.get("mkt_volume") is not None:
            primary["mkt_volume"] = int(primary["mkt_volume"]) + (int(other.get("mkt_volume") or 0) if same_day else 0)
        primary["vol_src"] = "NSE+BSE"
    cache[sym] = f"{primary['code']} ({primary['vol_src']})"
    return primary


def metrics(sc, d, cfg, now):
    """Compute day %, month %, relative volume. Reported facts vs calculations kept explicit."""
    t = cfg["thresholds"]
    today = now.date()
    bars = d["bars"]
    traded_today = bars[-1][0] == today and d["mkt_time"].date() == today
    hist = bars[:-1] if bars[-1][0] == today else bars
    m = {"price": d["price"], "exch": d["exch"], "code": d["code"], "vol_src": d.get("vol_src", d["exch"]),
         "hist_note": d.get("hist_note", ""),
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
    window = [b for b in hist if b[2] >= 0][-n:]   # only days with complete volume
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
                        f"({m['today_vol']:,} vs avg {m['avg_vol']:,.0f}/day, {m['vol_src']})", p))
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
        d = fetch_scrip(sc, cache, cfg)
        if not d:
            missing.append(sc["symbol"])
            continue
        results.append((sc, metrics(sc, d, cfg, now)))
        time.sleep(0.3)
    return results, missing


def fmt_alert(sc, m, hits):
    lines = [f"{sc['name']} ({sc['symbol']} · price {m['exch']}, vol {m['vol_src']})",
             f"Price ₹{m['price']:,.2f} · data as of {m['data_time']:%H:%M}"]
    lines += [f"• {h[0]}" for h in hits]
    return "\n".join(lines)


def run(cfg, mode):
    now = now_ist()
    state = load_state()
    cache = state.setdefault("volume_source", {}); state.pop("exchange_used", None)
    load_hist()
    bhav_got, bhav_failed = update_hist_from_bhavcopy(cfg)
    save_hist()

    if mode == "test":
        results, missing = scan(cfg, state, now, cache)
        rows = ["| Scrip | Price from (Yahoo code) | Volume from | Price | Day % | 1M % | Rel vol | Avg value ₹L/day | Days of history (NSE/BSE) |",
                "|---|---|---|---|---|---|---|---|---|"]
        f2 = lambda x, s="": "–" if x is None else f"{x:.2f}{s}"
        for sc, m in results:
            rows.append(f"| {sc['symbol']} | {m['code']} | {m['vol_src']} | {m['price']:,.2f} | {f2(m['day_pct'])} | "
                        f"{f2(m['month_pct'])} | {f2(m['rel_vol'], 'x')} | {f2(m['avg_value_lakh'])} | {m['hist_note']} |")
        report = "\n".join(rows)
        if missing:
            report += "\n\n**MISSING (fix Yahoo code in config.yaml):** " + ", ".join(missing)
        report += (f"\n\nNSE history files: {len(NSE_HIST['loaded'])} trading days stored"
                   f" ({bhav_got} downloaded this run" + (", NSE archives NOT reachable" if bhav_got == 0 and bhav_failed >= 6 else "") + ")")
        print(report)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
                f.write("## REIT Watch — test run\n\n" + report + "\n")
        bse = [sc["symbol"] for sc, m in results if m["exch"] == "BSE"]  # price fallback only
        msg = (f"{len(results)}/{len(results) + len(missing)} scrips OK."
               + f"\nNSE history: {len(NSE_HIST['loaded'])} days" + (" (NSE blocked — will build daily)" if bhav_got == 0 and bhav_failed >= 6 else "")
               + (f"\nPrice from BSE (no NSE data): {', '.join(bse)}" if bse else "")
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
    if end_t > close_t:                       # afternoon job: take final figures after the close
        wait = (end_t + timedelta(minutes=2) - now_ist()).total_seconds()
        if wait > 0:
            time.sleep(wait)
        scan(cfg, state, now_ist(), cache)    # refreshes today's NSE close/volume
        capture_eod()
        save_hist()
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
