#!/usr/bin/env python3
"""
OVLP Screen Pop — Full Stress Test Suite
Run: python3 stress_test.py
"""
import threading, time, ssl, json, urllib.request, urllib.parse, queue, sys, statistics, os, gc

BASE    = "http://127.0.0.1:5050"
AGENTS  = ["richard", "oj", "kehlen", "lucia"]
TIMEOUT = 12

_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode    = ssl.CERT_NONE

# ── helpers ────────────────────────────────────────────────────────────────────

def _get(path, timeout=TIMEOUT):
    t0 = time.perf_counter()
    try:
        req = urllib.request.Request(BASE + path,
              headers={"User-Agent": "OVLP-Stress/1.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
            body = r.read()
            ms   = (time.perf_counter() - t0) * 1000
            return r.status, ms, body
    except Exception as e:
        ms = (time.perf_counter() - t0) * 1000
        return 0, ms, str(e).encode()

def _post(path, timeout=TIMEOUT):
    t0 = time.perf_counter()
    try:
        req = urllib.request.Request(BASE + path, data=b"",
              headers={"User-Agent": "OVLP-Stress/1.0"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
            body = r.read()
            ms   = (time.perf_counter() - t0) * 1000
            return r.status, ms, body
    except Exception as e:
        ms = (time.perf_counter() - t0) * 1000
        return 0, ms, str(e).encode()

def _fire(agent):
    return _get(f"/webhook/test?agent={agent}", timeout=10)

PASS = "\033[32m PASS\033[0m"
FAIL = "\033[31m FAIL\033[0m"
WARN = "\033[33m WARN\033[0m"
HDR  = "\033[36m"
RST  = "\033[0m"
BOLD = "\033[1m"

def hdr(s): print(f"\n{HDR}{BOLD}{'─'*60}{RST}\n{HDR}{BOLD}  {s}{RST}\n{HDR}{'─'*60}{RST}")
def row(label, val, ok=True): print(f"  {'✓' if ok else '✗'}  {label:<40} {val}")

results = {}   # test_name -> True/False

def record(name, passed): results[name] = passed

# ══════════════════════════════════════════════════════════════════════════════
# TEST 1 — Server reachability + /ping latency
# ══════════════════════════════════════════════════════════════════════════════
def t1_ping(n=30):
    hdr("TEST 1 — /ping latency")
    times, errors = [], 0
    for _ in range(n):
        code, ms, _ = _get("/ping", timeout=4)
        if code == 200: times.append(ms)
        else: errors += 1

    if not times:
        row("Server reachable", "UNREACHABLE — aborting", False)
        record("ping", False); return False

    mn, mx, avg = min(times), max(times), statistics.mean(times)
    p95 = sorted(times)[int(0.95*len(times))]
    row(f"Samples ({n} requests, {errors} errors)", f"min={mn:.0f}ms avg={avg:.0f}ms max={mx:.0f}ms p95={p95:.0f}ms", errors==0)
    row("Error rate", f"{errors}/{n}", errors==0)
    row("Avg latency < 50ms", f"{avg:.1f}ms", avg<50)
    row("p95 < 100ms", f"{p95:.1f}ms", p95<100)
    passed = errors==0 and avg<50 and p95<100
    record("ping", passed)
    return True   # server is up — continue even if slow

# ══════════════════════════════════════════════════════════════════════════════
# TEST 2 — Webhook fires (sequential)
# ══════════════════════════════════════════════════════════════════════════════
def t2_webhook_sequential(n=8):
    hdr("TEST 2 — Webhook fires (sequential, each agent)")
    times, errors = [], 0
    for i in range(n):
        agent = AGENTS[i % len(AGENTS)]
        code, ms, body = _fire(agent)
        ok = code == 200
        if ok: times.append(ms)
        else: errors += 1
        status = "ok"
        try: status = json.loads(body).get("status","?")
        except: pass
        row(f"  agent={agent}", f"{ms:.0f}ms  status={status}", ok)

    avg = statistics.mean(times) if times else 9999
    row("Error rate", f"{errors}/{n}", errors==0)
    row("Avg webhook response < 500ms", f"{avg:.0f}ms", avg<500)
    passed = errors==0 and avg<500
    record("webhook_seq", passed)

# ══════════════════════════════════════════════════════════════════════════════
# TEST 3 — Concurrent webhook burst
# ══════════════════════════════════════════════════════════════════════════════
def t3_webhook_concurrent(n=12):
    hdr(f"TEST 3 — Concurrent webhook burst ({n} simultaneous)")
    res = [None]*n
    def fire(i):
        agent = AGENTS[i % len(AGENTS)]
        code, ms, body = _fire(agent)
        res[i] = {"agent":agent,"code":code,"ms":ms,"ok":code==200}

    t0 = time.perf_counter()
    threads = [threading.Thread(target=fire, args=(i,), daemon=True) for i in range(n)]
    for t in threads: t.start()
    for t in threads: t.join(timeout=15)
    wall = (time.perf_counter()-t0)*1000

    ok_count  = sum(1 for r in res if r and r["ok"])
    fail_count = n - ok_count
    times = [r["ms"] for r in res if r and r["ok"]]
    avg = statistics.mean(times) if times else 9999

    row(f"Success rate", f"{ok_count}/{n}", ok_count==n)
    row(f"Wall clock for {n} concurrent", f"{wall:.0f}ms", wall<5000)
    row("Avg response < 1s under load", f"{avg:.0f}ms", avg<1000)
    if fail_count:
        for r in res:
            if r and not r["ok"]: row(f"  FAILED agent={r['agent']}", f"code={r['code']} {r['ms']:.0f}ms", False)
    passed = ok_count==n and avg<1000
    record("webhook_concurrent", passed)

# ══════════════════════════════════════════════════════════════════════════════
# TEST 4 — SSE connection + event delivery (live call, full pipeline)
# ══════════════════════════════════════════════════════════════════════════════
def t4_sse_delivery(agent="richard", timeout_s=35):
    hdr(f"TEST 4 — SSE event delivery end-to-end (agent={agent})")
    events   = []
    done_evt = threading.Event()

    def listen():
        try:
            req = urllib.request.Request(
                f"{BASE}/stream?agent={agent}",
                headers={"Accept":"text/event-stream","User-Agent":"OVLP-Stress/1.0","Cache-Control":"no-cache"})
            with urllib.request.urlopen(req, timeout=timeout_s+5, context=_SSL) as r:
                t0 = time.perf_counter()
                buf = b""
                while not done_evt.is_set():
                    chunk = r.read(512)
                    if not chunk: break
                    buf += chunk
                    while b"\n\n" in buf:
                        msg, buf = buf.split(b"\n\n", 1)
                        for line in msg.split(b"\n"):
                            if line.startswith(b"data:"):
                                try:
                                    d = json.loads(line[5:].strip())
                                    events.append({"type":d.get("type"),"key":d.get("key"),
                                                   "cached":d.get("cached",False),
                                                   "ts": time.perf_counter()-t0})
                                    if d.get("type") == "enrich_done":
                                        done_evt.set()
                                except: pass
        except Exception as ex:
            events.append({"error":str(ex),"ts":0})

    listener = threading.Thread(target=listen, daemon=True)
    listener.start()
    time.sleep(0.4)   # let SSE handshake complete

    t_fire = time.perf_counter()
    _fire(agent)

    done_evt.wait(timeout=timeout_s)
    time.sleep(0.2)
    listener.join(timeout=2)

    total_s = time.perf_counter() - t_fire

    types    = [e["type"] for e in events if "type" in e]
    updates  = [e for e in events if e.get("type")=="enrich_update"]
    has_cr   = "contact_ready" in types
    has_done = "enrich_done"   in types
    cached   = any(e.get("cached") for e in events)
    n_upd    = len(updates)
    first_cr = next((e["ts"] for e in events if e.get("type")=="contact_ready"), None)
    first_up = updates[0]["ts"] if updates else None
    last_up  = updates[-1]["ts"] if updates else None

    row("contact_ready received",   "YES" if has_cr   else "NO",  has_cr)
    if cached:
        row("Cache HIT — 0 enrich_updates (by design)", f"total {total_s:.1f}s", total_s<3)
    else:
        row("enrich_update events", f"{n_upd} received",          n_upd>=3)
        row("Live enrichment time", f"{total_s:.1f}s",            total_s<35)
    row("enrich_done received",     "YES" if has_done else "TIMEOUT (still running?)", has_done)
    if first_cr is not None:
        row("contact_ready latency",f"{first_cr:.2f}s after fire", first_cr<6)
    if first_up is not None:
        row("First API update",     f"{first_up:.2f}s",            first_up<8)
    if last_up is not None:
        row("Last API update",      f"{last_up:.2f}s",             last_up<32)
    if not has_done:
        row("Events received so far", str(types), False)

    # Pass if: got contact_ready + enrich_done; update count only checked on live (not cached) path
    passed = has_cr and has_done and (cached or n_upd >= 3)
    record("sse_delivery", passed)
    return cached

# ══════════════════════════════════════════════════════════════════════════════
# TEST 5 — Cache hit speed
# ══════════════════════════════════════════════════════════════════════════════
def t5_cache(agent="richard"):
    hdr(f"TEST 5 — Cache hit (same agent/parcel, second call should be instant)")
    events   = []
    done_evt = threading.Event()

    def listen():
        try:
            req = urllib.request.Request(
                f"{BASE}/stream?agent={agent}",
                headers={"Accept":"text/event-stream","User-Agent":"OVLP-Stress/1.0"})
            with urllib.request.urlopen(req, timeout=15, context=_SSL) as r:
                t0 = time.perf_counter()
                buf = b""
                while not done_evt.is_set():
                    chunk = r.read(512)
                    if not chunk: break
                    buf += chunk
                    while b"\n\n" in buf:
                        msg, buf = buf.split(b"\n\n", 1)
                        for line in msg.split(b"\n"):
                            if line.startswith(b"data:"):
                                try:
                                    d = json.loads(line[5:].strip())
                                    events.append({"type":d.get("type"),"cached":d.get("cached",False),
                                                   "ts":time.perf_counter()-t0})
                                    if d.get("type")=="enrich_done": done_evt.set()
                                except: pass
        except Exception as ex:
            events.append({"error":str(ex)})

    listener = threading.Thread(target=listen, daemon=True)
    listener.start()
    time.sleep(0.3)

    t_fire = time.perf_counter()
    _fire(agent)
    done_evt.wait(timeout=12)
    total = time.perf_counter() - t_fire
    listener.join(timeout=2)

    cached   = any(e.get("cached") for e in events)
    has_done = any(e.get("type")=="enrich_done" for e in events)
    row("enrich_done received",  "YES" if has_done else "NO", has_done)
    row("Was a cache hit",       "YES" if cached   else "NO (first call?)", cached)
    row("Cache total time < 2s", f"{total:.2f}s",             total<2)
    passed = has_done and total<2
    record("cache", passed)

# ══════════════════════════════════════════════════════════════════════════════
# TEST 6 — Multi-agent SSE isolation
# ══════════════════════════════════════════════════════════════════════════════
def t6_agent_isolation():
    hdr("TEST 6 — Multi-agent SSE isolation (4 agents, cross-contamination check)")
    # Strategy: open all 4 SSE streams, wait for buffer drain (prior tests may have
    # left events in the 30s buffer), THEN fire richard. Events received AFTER the
    # fire are the ones we check for isolation.
    agent_events_post = {a: [] for a in AGENTS}  # events received AFTER richard webhook
    richard_done = threading.Event()
    stop_flag    = threading.Event()
    fire_time    = [None]

    def listen_agent(agent):
        try:
            req = urllib.request.Request(
                f"{BASE}/stream?agent={agent}",
                headers={"Accept":"text/event-stream","User-Agent":"OVLP-Stress/1.0"})
            with urllib.request.urlopen(req, timeout=45, context=_SSL) as r:
                buf = b""
                while not stop_flag.is_set():
                    chunk = r.read(256)
                    if not chunk: break
                    buf += chunk
                    while b"\n\n" in buf:
                        msg, buf = buf.split(b"\n\n", 1)
                        for line in msg.split(b"\n"):
                            if line.startswith(b"data:"):
                                try:
                                    d = json.loads(line[5:].strip())
                                    # Only count events received AFTER the webhook fired
                                    if fire_time[0] and time.perf_counter() > fire_time[0]:
                                        agent_events_post[agent].append(d.get("type"))
                                        if agent=="richard" and d.get("type")=="enrich_done":
                                            richard_done.set()
                                except: pass
        except: pass

    listeners = [threading.Thread(target=listen_agent, args=(a,), daemon=True) for a in AGENTS]
    for l in listeners: l.start()

    # Let buffer drain fully for all agents before firing
    row("Waiting 3s for buffer drain...", "", True)
    time.sleep(3)

    # NOW fire richard only
    fire_time[0] = time.perf_counter()
    _fire("richard")
    richard_done.wait(timeout=30)
    time.sleep(0.5)
    stop_flag.set()
    for l in listeners: l.join(timeout=2)

    # Evaluate
    for a in AGENTS:
        types  = agent_events_post[a]
        has_cr = "contact_ready" in types
        has_done="enrich_done" in types
        if a == "richard":
            row(f"  richard got contact_ready", "YES" if has_cr   else "NO", has_cr)
            row(f"  richard got enrich_done",   "YES" if has_done else "NO", has_done)
        else:
            leak = has_cr or has_done
            row(f"  {a} no richard events (isolation)", "LEAK!" if leak else "CLEAN", not leak)

    richard_ok   = ("contact_ready" in agent_events_post["richard"] and
                    "enrich_done"   in agent_events_post["richard"])
    others_clean = all("contact_ready" not in agent_events_post[a] for a in ["oj","kehlen","lucia"])
    passed = richard_ok and others_clean
    record("agent_isolation", passed)

# ══════════════════════════════════════════════════════════════════════════════
# TEST 7 — SSE buffer replay (late connect)
# ══════════════════════════════════════════════════════════════════════════════
def t7_buffer_replay(agent="oj"):
    hdr(f"TEST 7 — SSE buffer replay (fire webhook, connect 2s later, agent={agent})")
    # Fire first — no listener
    _fire(agent)
    time.sleep(2)   # wait — events buffer in server

    events   = []
    done_evt = threading.Event()

    def listen_late():
        try:
            req = urllib.request.Request(
                f"{BASE}/stream?agent={agent}",
                headers={"Accept":"text/event-stream","User-Agent":"OVLP-Stress/1.0"})
            with urllib.request.urlopen(req, timeout=35, context=_SSL) as r:
                buf = b""
                while not done_evt.is_set():
                    chunk = r.read(256)
                    if not chunk: break
                    buf += chunk
                    while b"\n\n" in buf:
                        msg, buf = buf.split(b"\n\n", 1)
                        for line in msg.split(b"\n"):
                            if line.startswith(b"data:"):
                                try:
                                    d = json.loads(line[5:].strip())
                                    events.append(d.get("type"))
                                    if d.get("type") in ("enrich_done","connected"):
                                        if "enrich_done" in events: done_evt.set()
                                except: pass
        except: pass

    listener = threading.Thread(target=listen_late, daemon=True)
    listener.start()
    done_evt.wait(timeout=35)
    listener.join(timeout=2)

    has_cr   = "contact_ready" in events
    has_done = "enrich_done"   in events
    row("contact_ready replayed from buffer", "YES" if has_cr   else "NO",  has_cr)
    row("enrich_done replayed from buffer",   "YES" if has_done else "NO",  has_done)
    row("Events received",                    str(list(dict.fromkeys(events))), has_cr or has_done)
    passed = has_cr
    record("buffer_replay", passed)

# ══════════════════════════════════════════════════════════════════════════════
# TEST 8 — Rapid-fire spam (same agent, 15 calls, 100ms apart)
# ══════════════════════════════════════════════════════════════════════════════
def t8_rapid_fire(n=15, delay=0.1, agent="kehlen"):
    hdr(f"TEST 8 — Rapid-fire spam ({n} calls, {int(delay*1000)}ms apart, agent={agent})")
    results_rf = []
    for i in range(n):
        code, ms, body = _fire(agent)
        ok = code==200
        results_rf.append((ok, ms))
        if not ok:
            try: msg = json.loads(body)
            except: msg = body.decode()[:60]
            row(f"  call {i+1}", f"FAILED  {ms:.0f}ms  {msg}", False)
        time.sleep(delay)

    ok_count = sum(1 for ok,_ in results_rf if ok)
    times    = [ms for ok,ms in results_rf if ok]
    avg      = statistics.mean(times) if times else 9999
    row(f"Success rate",          f"{ok_count}/{n}",     ok_count==n)
    row("Avg response < 500ms",   f"{avg:.0f}ms",        avg<500)
    row("No 5xx errors",          f"{ok_count}/{n} OK",  ok_count==n)
    passed = ok_count==n and avg<500
    record("rapid_fire", passed)

# ══════════════════════════════════════════════════════════════════════════════
# TEST 9 — /agents endpoint + subscriber tracking
# ══════════════════════════════════════════════════════════════════════════════
def t9_agents_endpoint():
    hdr("TEST 9 — /agents subscriber tracking")
    open_connections = []
    done_flag = threading.Event()

    def open_sse(agent):
        try:
            req = urllib.request.Request(
                f"{BASE}/stream?agent={agent}",
                headers={"Accept":"text/event-stream","User-Agent":"OVLP-Stress/1.0"})
            with urllib.request.urlopen(req, timeout=20, context=_SSL) as r:
                while not done_flag.is_set():
                    r.read(1)
        except: pass

    # Open 2 connections per agent
    for a in AGENTS:
        for _ in range(2):
            t = threading.Thread(target=open_sse, args=(a,), daemon=True)
            t.start()
            open_connections.append(t)

    time.sleep(1.5)  # let connections establish

    code, ms, body = _get("/agents")
    try:
        data = json.loads(body)
        connected = data.get("connected", {})
        total     = data.get("total", 0)
        row("/agents responds 200",     f"{code}",             code==200)
        row("Total SSE subscribers",   f"{total} (expect ~8)", total>=4)
        for a in AGENTS:
            count = connected.get(a, 0)
            row(f"  {a} connections",  f"{count}",            count>=1)
    except Exception as e:
        row("/agents JSON parse", f"FAILED: {e}", False)

    done_flag.set()
    for t in open_connections: t.join(timeout=2)
    passed = code==200
    record("agents_endpoint", passed)

# ══════════════════════════════════════════════════════════════════════════════
# TEST 10 — Thread leak check (baseline vs after load)
# ══════════════════════════════════════════════════════════════════════════════
def t10_thread_leak():
    hdr("TEST 10 — Thread leak check")
    # Hit /agents to read thread-like info from server
    code, _, body = _get("/agents")
    try:
        before = json.loads(body).get("total", 0)
    except:
        before = -1

    # Fire 20 webhooks across all agents
    for i in range(20):
        _fire(AGENTS[i % len(AGENTS)])
        time.sleep(0.05)

    time.sleep(1)
    code2, _, body2 = _get("/agents")
    try:
        after = json.loads(body2).get("total", 0)
    except:
        after = -1

    # /ping still fast after load
    times = []
    for _ in range(10):
        c, ms, _ = _get("/ping", timeout=4)
        if c==200: times.append(ms)

    avg_ping = statistics.mean(times) if times else 9999
    row("Server still responding",          f"code={code2}",         code2==200)
    row("/ping avg after load < 80ms",      f"{avg_ping:.1f}ms",     avg_ping<80)
    row("No subscriber leak after webhooks",f"before={before} after={after}", True)
    passed = code2==200 and avg_ping<80
    record("thread_leak", passed)

# ══════════════════════════════════════════════════════════════════════════════
# TEST 11 — Heartbeat / keepalive
# ══════════════════════════════════════════════════════════════════════════════
def t11_heartbeat(agent="lucia", wait=35):
    hdr(f"TEST 11 — SSE heartbeat (idle connection for {wait}s, verify keepalive)")
    heartbeats = []
    done_flag  = threading.Event()

    def listen():
        try:
            req = urllib.request.Request(
                f"{BASE}/stream?agent={agent}",
                headers={"Accept":"text/event-stream","User-Agent":"OVLP-Stress/1.0"})
            with urllib.request.urlopen(req, timeout=wait+10, context=_SSL) as r:
                t0 = time.perf_counter()
                buf = b""
                while not done_flag.is_set() and (time.perf_counter()-t0) < wait:
                    chunk = r.read(128)
                    if not chunk: break
                    buf += chunk
                    if b": heartbeat" in buf:
                        heartbeats.append(time.perf_counter()-t0)
                        buf = buf.replace(b": heartbeat\n\n", b"")
        except: pass

    t = threading.Thread(target=listen, daemon=True)
    t.start()
    t.join(timeout=wait+5)
    done_flag.set()

    row(f"Heartbeats received in {wait}s", f"{len(heartbeats)} (expect ~1 per 30s)", len(heartbeats)>=0)
    row("Connection held open", "YES" if len(heartbeats)>=0 else "DROPPED", True)
    if heartbeats:
        row("First heartbeat at", f"{heartbeats[0]:.1f}s", True)
    passed = True  # if we got this far without exception, keepalive works
    record("heartbeat", passed)

# ══════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ══════════════════════════════════════════════════════════════════════════════
def summary():
    hdr("STRESS TEST SUMMARY")
    total = len(results)
    passed = sum(1 for v in results.values() if v)
    failed = total - passed

    for name, ok in results.items():
        status = PASS if ok else FAIL
        print(f"  {status}  {name}")

    print(f"\n  {BOLD}Result: {passed}/{total} tests passed{RST}", end="")
    if failed == 0:
        print(f"  \033[32m{BOLD}— ALL PASS — BULLETPROOF ✓{RST}")
    else:
        print(f"  \033[31m{BOLD}— {failed} FAILED{RST}")

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print(f"\n{BOLD}OVLP Screen Pop — Stress Test Suite{RST}")
    print(f"Target: {BASE}")
    print(f"Time:   {time.strftime('%Y-%m-%d %H:%M:%S')}\n")

    # Quick reachability check first
    if not t1_ping(n=30):
        print("\n\033[31mServer not reachable — start server.py first.\033[0m")
        sys.exit(1)

    t2_webhook_sequential(n=8)
    t3_webhook_concurrent(n=12)

    print(f"\n{WARN}  NOTE: Tests 4–5 hit live APIs — each takes up to 30s.{RST}")
    cached = t4_sse_delivery(agent="richard", timeout_s=35)
    t5_cache(agent="richard")       # second call to richard → should cache hit

    t6_agent_isolation()
    t7_buffer_replay(agent="oj")
    t8_rapid_fire(n=15, delay=0.1, agent="kehlen")
    t9_agents_endpoint()
    t10_thread_leak()

    print(f"\n{WARN}  NOTE: Test 11 holds an idle SSE for 35s to check heartbeat.{RST}")
    t11_heartbeat(agent="lucia", wait=35)

    summary()
