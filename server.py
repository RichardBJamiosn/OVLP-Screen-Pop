"""
OVLP Screen Pop Server — ASSESS Framework (Maximum Free API Build)
All data sources are free / no API key required.
"""

import json, os, queue, re, threading, time, urllib.parse, urllib.request, urllib.error, math, ssl, sys
from flask import Flask, Response, request, jsonify, send_from_directory

VERSION = "LEGACY"
COMP_PROMPT_VERSION = "LEGACY"
# Update sources: LAN (office) checked first, GitHub fallback for remote staff
_UPDATE_LAN    = "http://192.168.0.103:5050"
_UPDATE_GITHUB = "https://raw.githubusercontent.com/RichardBJamiosn/OVLP-Screen-Pop/main"


def _auto_update():
    """On startup: check for a newer version — LAN first, GitHub fallback.
    If newer version found, replace local files and restart."""
    this_dir = os.path.dirname(os.path.abspath(__file__))

    # Try LAN first (fast, always current), then GitHub (works from home)
    sources = [
        ("lan",    _UPDATE_LAN + "/update/version", _UPDATE_LAN + "/update"),
        ("github", _UPDATE_GITHUB + "/server.py",   _UPDATE_GITHUB),
    ]

    for label, check_url, base_url in sources:
        try:
            if label == "lan":
                resp = urllib.request.urlopen(check_url, timeout=3)
                remote = json.loads(resp.read()).get("version", "")
                if not remote or remote == VERSION:
                    return   # LAN reachable, already up to date
                print(f"[update] v{remote} available via LAN (current {VERSION}) — updating...")
            else:
                # GitHub: parse VERSION from raw server.py header
                resp = urllib.request.urlopen(check_url, timeout=10, context=_ssl())
                first_chunk = resp.read(500).decode("utf-8", errors="replace")
                import re as _re
                m = _re.search(r'VERSION\s*=\s*["\']([^"\']+)["\']', first_chunk)
                remote = m.group(1) if m else ""
                if not remote or remote == VERSION:
                    print(f"[update] GitHub v{remote} matches local v{VERSION} — skipping")
                    return
                print(f"[update] v{remote} available via GitHub (current {VERSION}) — updating...")

            for fname in ("server.py", "dashboard.html", "comp_prompt.md", "comp_report.html"):
                url = f"{base_url}/{fname}"
                ctx = _ssl() if "github" in base_url else None
                data = urllib.request.urlopen(url, timeout=20, context=ctx).read() if ctx else urllib.request.urlopen(url, timeout=20).read()
                dest = os.path.join(this_dir, fname)
                with open(dest + ".new", "wb") as f:
                    f.write(data)
                os.replace(dest + ".new", dest)
                print(f"[update] ✓ {fname}")
            print("[update] Restarting with updated files...")
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            print(f"[update] {label} failed: {e}")
            continue  # Try next source

# ─────────────────────────────────────────────
# CONFIG  (ovlp_config.json in same folder)
# ─────────────────────────────────────────────
_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ovlp_config.json")

def _load_cfg():
    try:
        with open(_CONFIG_FILE) as f: return json.load(f)
    except: return {}

def _save_cfg(d):
    cfg = _load_cfg(); cfg.update(d)
    with open(_CONFIG_FILE, "w") as f: json.dump(cfg, f, indent=2)

def _ghl_key():  return os.environ.get("GHL_API_KEY")  or _load_cfg().get("ghl_api_key",  "")
def _server_url():return os.environ.get("OVLP_URL")     or _load_cfg().get("server_url",   "http://localhost:5050")

# Fix macOS SSL — Big Sur (11.x) ships ancient LibreSSL that fails TLS 1.3 handshakes.
# Strategy: use certifi CA bundle if available, but always relax hostname/cipher constraints
# so old LibreSSL can still negotiate with modern servers (FEMA, USDA, OSRM, etc.).
def _ssl():
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # Relax hostname + verification — old LibreSSL fails even with correct certs
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    # Cap at TLS 1.2 — Big Sur's LibreSSL chokes on TLS 1.3 negotiation
    try:
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    except (AttributeError, ValueError):
        pass
    # Broadest cipher set — let the server pick what works
    try:
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
    except ssl.SSLError:
        pass
    return ctx

_SSL = _ssl()

app = Flask(__name__, static_folder=".")

# Per-agent SSE queues: slug -> [Queue, ...]
agent_subscribers = {}
agent_lock = threading.Lock()

# Last known state per agent — replayed on reconnect so reps never see a blank screen
_last_state = {}   # slug -> {"contact": contact_dict, "enrichment": enrich_dict}

# Recent event buffer — holds last 30s of events per agent so late-connecting
# clients get events that fired before their SSE stream was established
_event_buffer = {}  # slug -> [(ts, event), ...]
_BUFFER_TTL   = 300  # seconds — 5 min window so late-opening dashboard still replays

# ─────────────────────────────────────────────
# POP HISTORY — last 5 pops, persisted to disk
# ─────────────────────────────────────────────
_POP_HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pop_history.json")
_pop_history      = []   # list of dicts, newest first, max 5
_pop_history_lock = threading.Lock()

def _load_pop_history():
    try:
        with open(_POP_HISTORY_FILE) as f:
            return json.load(f)
    except:
        return []

def _save_pop_history():
    try:
        with open(_POP_HISTORY_FILE, "w") as f:
            json.dump(_pop_history, f, indent=2)
    except Exception as e:
        print(f"[history] save failed: {e}")

def _record_pop(contact, geo_src, store, errors):
    """Build a pop history entry and prepend to _pop_history (max 5)."""
    comps_data   = store.get("comps", {})
    api_summary  = {}
    for key in ("flood","soil","wetlands","slope","zoning","drive","epa_echo","superfund","streams","roads","power","dams","census"):
        d = store.get(key, {})
        api_summary[key] = d.get("cls", "grey") if isinstance(d, dict) else "grey"

    entry = {
        "ts":          time.strftime("%Y-%m-%d %H:%M:%S"),
        "agent":       contact.get("agent", "—"),
        "contact":     contact.get("owner_name", "—"),
        "address":     contact.get("property_address", "—"),
        "parcel_id":   contact.get("parcel_id", "—"),
        "acreage":     contact.get("acreage", "—"),
        "geocode":     geo_src,
        "comps":       len(comps_data.get("comps", [])),
        "comp_avg":    comps_data.get("avg_per_acre", 0),
        "score":       store.get("assess", {}).get("score", "—") if isinstance(store.get("assess"), dict) else "—",
        "apis":        api_summary,
        "errors":      errors,
    }
    with _pop_history_lock:
        _pop_history.insert(0, entry)
        del _pop_history[5:]
    _save_pop_history()

def _buffer_event(slug, event):
    now = time.time()
    with agent_lock:
        buf = _event_buffer.setdefault(slug, [])
        buf.append((now, event))
        # Trim stale entries
        _event_buffer[slug] = [(t, e) for t, e in buf if now - t < _BUFFER_TTL]

def _drain_buffer(slug):
    """Return events buffered for this agent in the last BUFFER_TTL seconds."""
    now = time.time()
    with agent_lock:
        buf = _event_buffer.get(slug, [])
        return [e for t, e in buf if now - t < _BUFFER_TTL]

def _agent_slug(name):
    """'OJ' → 'oj',  'John Smith' → 'john-smith'"""
    return re.sub(r'[^a-z0-9]+', '-', name.strip().lower()).strip('-') or 'unknown'

def _push_to_agent(slug, event):
    """Put event into all queues registered for this agent slug.
    If slug is 'broadcast', push to every connected agent."""
    _buffer_event(slug, event)
    with agent_lock:
        if slug == "broadcast":
            targets = [q for qs in agent_subscribers.values() for q in qs]
        else:
            targets = list(agent_subscribers.get(slug, []))
    for q in targets:
        q.put(event)
    if not targets:
        print(f"[push] No subscribers for agent '{slug}' — buffered for late connect")

# ─────────────────────────────────────────────
# HTTP HELPERS
# ─────────────────────────────────────────────

def http_get(url, timeout=8, headers=None):
    h = {"User-Agent": "OVLP-ScreenPop/1.0"}
    if headers: h.update(headers)
    try:
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
            return json.loads(r.read())
    except (ssl.SSLError, urllib.error.URLError) as e:
        if "SSL" in str(e) or "Connection reset" in str(e) or "nodename" in str(e):
            try:
                import requests as _req
                r = _req.get(url, headers=h, timeout=timeout, verify=False)
                return r.json()
            except Exception:
                pass
        raise

def http_post(url, data, timeout=12):
    encoded = urllib.parse.urlencode(data).encode()
    try:
        req = urllib.request.Request(url, data=encoded,
              headers={"User-Agent": "OVLP-ScreenPop/1.0", "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
            return json.loads(r.read())
    except (ssl.SSLError, urllib.error.URLError) as e:
        if "SSL" in str(e) or "Connection reset" in str(e):
            try:
                import requests as _req
                r = _req.post(url, data=data, timeout=timeout, verify=False)
                return r.json()
            except Exception:
                pass
        raise

# ─────────────────────────────────────────────
# GHL API — fetch contact by ID
# ─────────────────────────────────────────────

_cf_cache = {}          # locationId -> {fieldId: shortKey}
_cf_lock  = threading.Lock()

def _ghl_headers():
    return {"Authorization": f"Bearer {_ghl_key()}", "Version": "2021-07-28", "Accept": "application/json"}

def _ghl_field_map(location_id):
    """Fetch + cache custom field ID → short key for a GHL location."""
    with _cf_lock:
        if location_id in _cf_cache:
            return _cf_cache[location_id]
    try:
        url  = f"https://services.leadconnectorhq.com/locations/{location_id}/customFields"
        data = http_get(url, timeout=8, headers=_ghl_headers())
        m = {}
        for f in data.get("customFields", []):
            fk = f.get("fieldKey", "")            # e.g. "contact.property_address"
            short = fk.split(".", 1)[1] if "." in fk else fk
            if short: m[f["id"]] = short
        with _cf_lock:
            _cf_cache[location_id] = m
        print(f"[ghl_cf] cached {len(m)} fields for location {location_id}")
        return m
    except Exception as e:
        print(f"[ghl_cf] {e}"); return {}

def fetch_ghl_contact(contact_id):
    """Pull a contact from GHL API and return a build_contact-compatible dict."""
    key = _ghl_key()
    if not key:
        return None, "GHL API key not configured"
    try:
        url  = f"https://services.leadconnectorhq.com/contacts/{contact_id}"
        data = http_get(url, timeout=8, headers=_ghl_headers())
        c    = data.get("contact", data)

        # Field ID → name: try API first, fall back to config
        loc_id  = c.get("locationId", "")
        cf_map  = _ghl_field_map(loc_id) if loc_id else {}
        if not cf_map:
            cf_map = _load_cfg().get("field_map", {})

        # Resolve custom fields
        cf = {}
        for field in c.get("customFields", []):
            fid = field.get("id", "")
            val = field.get("value")
            if val is None: val = ""
            short = cf_map.get(fid, fid)
            cf[short] = val

        # Parse GHL property_address: "Street, City, State, ZIP" → split out zip
        prop_raw = str(cf.get("property_address", ""))
        zip_code = c.get("postalCode", "")
        if prop_raw:
            if "," in prop_raw:
                parts = [p.strip() for p in prop_raw.split(",")]
                last = parts[-1].strip()
                # Handle bare ZIP or ZIP+4 format
                if re.match(r'^\d{5}(-\d{4})?$', last):
                    zip_code = zip_code or last[:5]
                prop_street = parts[0]
                cf["property_address"] = prop_street
            # Fallback: regex scan for any 5-digit ZIP in the full address string
            if not zip_code:
                m = re.search(r'\b(\d{5})(?:-\d{4})?\b', prop_raw)
                if m: zip_code = m.group(1)
        # Also scan contact address1 as fallback
        if not zip_code:
            m = re.search(r'\b(\d{5})(?:-\d{4})?\b', c.get("address1", ""))
            if m: zip_code = m.group(1)

        # Handle array flags → flat strings for build_contact
        absentee_flags = cf.get("absentee_flags", [])
        long_term_flags= cf.get("long_term_flags", [])
        absentee_str   = "Yes" if isinstance(absentee_flags, list) and absentee_flags else (absentee_flags or "")
        long_term_str  = "True" if isinstance(long_term_flags, list) and long_term_flags else (long_term_flags or "")

        first = c.get("firstName", "")
        last  = c.get("lastName",  "")
        raw   = {
            "full_name":        f"{first} {last}".strip(),
            "address1":         c.get("address1", ""),
            "postalCode":       zip_code,
            "assignedToName":   c.get("assignedToName", "") or c.get("assignedTo", ""),
            "Absentee Owner":   absentee_str,
            "Long Term Ownership": long_term_str,
            # Bridge GHL custom field keys → raw column names build_contact expects
            "Last Sale Date":   str(cf.get("last_sale_date", "")),
            "Last Sale Price":  str(cf.get("last_sale_price", "")),
            "Annual Tax":       str(cf.get("annual_tax", "")),
            "Land Use Desc":    str(cf.get("land_use", "")),
            "customField":      cf,
        }
        return raw, None
    except Exception as e:
        print(f"[fetch_ghl] {e}")
        return None, str(e)

# ─────────────────────────────────────────────
# PHONE INDEX — reverse lookup: any phone → contact ID + name + property
# ─────────────────────────────────────────────

_phone_index = {}        # normalized_phone -> {contact_id, contact_name, property_address, phone_type}
_phone_index_lock = threading.Lock()
_phone_index_ts   = 0    # last rebuild timestamp

def _normalize_phone(raw):
    """Strip to digits only. If 11 digits starting with 1, drop the 1."""
    digits = re.sub(r'\D', '', str(raw))
    if len(digits) == 11 and digits.startswith('1'):
        digits = digits[1:]
    return digits if len(digits) >= 7 else ""

def _build_phone_index():
    """Paginate all GHL contacts, extract every phone number, build reverse map."""
    global _phone_index_ts
    key = _ghl_key()
    if not key:
        print("[phone_index] No GHL API key — skipping")
        return
    loc = _load_cfg().get("ghl_location_id", "")
    if not loc:
        print("[phone_index] No location ID — skipping")
        return

    idx = {}
    total = 0
    start_after = None
    start_after_id = None

    while True:
        url = f"https://services.leadconnectorhq.com/contacts/?locationId={loc}&limit=100"
        if start_after and start_after_id:
            url += f"&startAfter={start_after}&startAfterId={start_after_id}"
        try:
            data = http_get(url, timeout=15, headers=_ghl_headers())
        except Exception as e:
            print(f"[phone_index] API error on page: {e}")
            break

        contacts = data.get("contacts", [])
        if not contacts:
            break

        for c in contacts:
            cid  = c.get("id", "")
            name = c.get("contactName") or f"{c.get('firstName','')} {c.get('lastName','')}".strip()
            # We don't have property_address from list endpoint — store what we have
            entry_base = {"contact_id": cid, "contact_name": name}

            # 1. Primary phone
            primary = _normalize_phone(c.get("phone", ""))
            if primary:
                idx[primary] = {**entry_base, "phone_type": "primary"}

            # 2. Additional phones
            for ap in c.get("additionalPhones", []):
                num = _normalize_phone(ap if isinstance(ap, str) else ap.get("phone", ""))
                if num and num != primary:
                    idx[num] = {**entry_base, "phone_type": "additional"}

            # 3. Custom fields that look like phone numbers
            for cf in c.get("customFields", []):
                val = str(cf.get("value", ""))
                digits = re.sub(r'\D', '', val)
                if 10 <= len(digits) <= 11:
                    num = _normalize_phone(val)
                    if num and num not in idx:
                        idx[num] = {**entry_base, "phone_type": "custom_field"}

        total += len(contacts)
        meta = data.get("meta", {})
        start_after = meta.get("startAfter")
        start_after_id = meta.get("startAfterId")
        if not meta.get("nextPage"):
            break

    with _phone_index_lock:
        _phone_index.clear()
        _phone_index.update(idx)
        _phone_index_ts = time.time()

    print(f"[phone_index] Built: {len(idx)} numbers from {total} contacts")

def _ensure_phone_index(max_age=3600):
    """Rebuild index if stale (default: older than 1 hour)."""
    if time.time() - _phone_index_ts > max_age:
        _build_phone_index()

def phone_lookup(raw_number):
    """Look up a phone number. Returns match dict or None."""
    _ensure_phone_index()
    norm = _normalize_phone(raw_number)
    if not norm:
        return None
    with _phone_index_lock:
        return _phone_index.get(norm)

# ─────────────────────────────────────────────

def bbox(lat, lon, miles=1.0):
    """Return (south, west, north, east) bounding box."""
    dlat = miles / 69.0
    dlon = miles / (69.0 * math.cos(math.radians(lat)))
    return lat-dlat, lon-dlon, lat+dlat, lon+dlon

# ─────────────────────────────────────────────
# GEOCODE — OSM Nominatim (free)
# ─────────────────────────────────────────────

def geocode(address):
    try:
        params = urllib.parse.urlencode({"q": address, "format": "json", "limit": 1, "countrycodes": "us"})
        data = http_get(f"https://nominatim.openstreetmap.org/search?{params}", timeout=6)
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as e:
        print(f"[geocode] {e}")
    return None, None


def parcel_centroid(parcel_id):
    """Look up exact parcel polygon from Franklin County GIS by parcel ID and return centroid.
    Works even when the address has no street number (road-name-only properties)."""
    if not parcel_id:
        return None, None
    try:
        pid = parcel_id.strip()
        url = (f"https://gis.franklincountyohio.gov/hosting/rest/services/ParcelFeatures"
               f"/Parcel_Features/MapServer/0/query"
               f"?where=PARCELID%3D%27{urllib.parse.quote(pid)}%27"
               f"&outFields=PARCELID,SITEADDRESS&returnGeometry=true&outSR=4326&f=json")
        data = http_get(url, timeout=10)
        features = data.get("features", [])
        if not features:
            return None, None
        rings = features[0].get("geometry", {}).get("rings", [])
        if not rings:
            return None, None
        pts = rings[0]
        lon = sum(p[0] for p in pts) / len(pts)
        lat = sum(p[1] for p in pts) / len(pts)
        print(f"[parcel_centroid] {pid} → lat={lat:.6f}, lon={lon:.6f}")
        return round(lat, 6), round(lon, 6)
    except Exception as e:
        print(f"[parcel_centroid] {e}")
        return None, None


def get_parcel_record(parcel_id, lat=None, lon=None):
    """Subject-parcel sale + value from Franklin County Auditor GIS.
    Fills the POP financial boxes (last sold date/amount, market value) when the
    GHL contact carries no sale data of its own. Tax bill / delinquent balance are
    NOT published in this layer, so those boxes still rely on GHL list data."""
    base = ("https://gis.franklincountyohio.gov/hosting/rest/services/ParcelFeatures"
            "/Parcel_Features/MapServer/0/query")
    fields = "PARCELID,SALEDATE,SALEPRICE,TOTVALUEBASE"
    url = None
    if parcel_id:
        pid = parcel_id.strip()
        url = (f"{base}?where=PARCELID%3D%27{urllib.parse.quote(pid)}%27"
               f"&outFields={fields}&returnGeometry=false&f=json")
    elif lat and lon:
        pt = json.dumps({"x": lon, "y": lat, "spatialReference": {"wkid": 4326}})
        url = (f"{base}?geometry={urllib.parse.quote(pt)}"
               f"&geometryType=esriGeometryPoint&inSR=4326"
               f"&spatialRel=esriSpatialRelIntersects&outFields={fields}"
               f"&returnGeometry=false&f=json")
    if not url:
        return {}
    try:
        data = http_get(url, timeout=10)
        feats = data.get("features", [])
        if not feats:
            return {}
        a = feats[0].get("attributes", {})
        out = {"source": "Franklin County Auditor"}
        sale_ts = a.get("SALEDATE")
        if sale_ts:
            try:
                import datetime
                out["last_sale_date"] = datetime.datetime.utcfromtimestamp(
                    sale_ts / 1000).strftime("%Y-%m-%d")
            except Exception:
                pass
        price = a.get("SALEPRICE") or 0
        try:
            if float(price) > 0:
                out["last_sale_amount"] = int(float(price))
        except Exception:
            pass
        mv = a.get("TOTVALUEBASE") or 0   # TOTVALUEBASE is full appraised (market) value
        try:
            if float(mv) > 0:
                out["market_value"] = int(float(mv))
        except Exception:
            pass
        print(f"[parcel_record] {parcel_id or (lat, lon)} → "
              f"sale={out.get('last_sale_amount')} date={out.get('last_sale_date')} "
              f"mkt={out.get('market_value')}")
        return out
    except Exception as e:
        print(f"[parcel_record] {e}")
        return {}

# ─────────────────────────────────────────────
# 1. ACCESS — Road type + landlocked (OSM Overpass)
# ─────────────────────────────────────────────

_OVERPASS_SERVERS = [
    "http://overpass-api.de/api/interpreter",       # HTTP — works on Big Sur LibreSSL
    "https://overpass.private.coffee/api/interpreter",
]

def _overpass_bulk(lat, lon):
    """Single combined Overpass query — fires all servers IN PARALLEL, uses first response.
    Worst-case wait = 12s (one timeout). Best-case = whichever server responds first."""
    query = (
        f"[out:json][timeout:10];"
        f"("
        f"way(around:150,{lat},{lon})[highway];"
        f"way(around:800,{lat},{lon})[power~\"line|minor_line\"];"
        f"way(around:400,{lat},{lon})[waterway~\"stream|river|canal\"];"
        f"node(around:400,{lat},{lon})[waterway~\"stream|river|canal\"];"
        f"way(around:200,{lat},{lon})[natural~\"wetland|water\"];"
        f"node(around:200,{lat},{lon})[natural=wetland];"
        f"way(around:1600,{lat},{lon})[landuse~\"industrial|landfill|quarry|brownfield|contaminated\"];"
        f"node(around:1600,{lat},{lon})[landuse~\"industrial|landfill|quarry|brownfield|contaminated\"];"
        f"node(around:1600,{lat},{lon})[amenity=waste_disposal];"
        f"way(around:1600,{lat},{lon})[amenity=waste_disposal];"
        f"node(around:2000,{lat},{lon})[waterway~\"dam|weir|lock_gate\"];"
        f"way(around:2000,{lat},{lon})[waterway~\"dam|weir\"];"
        f"way(around:2000,{lat},{lon})[man_made=dam];"
        f");"
        f"out tags center 80;"
    )

    winner   = [None]   # first successful result
    errors   = []
    done_evt = threading.Event()
    lock     = threading.Lock()

    def try_server(server):
        try:
            print(f"[overpass] trying {server}")
            result   = http_post(server, {"data": query}, timeout=8)
            elements = result.get("elements", [])
            with lock:
                if winner[0] is None:          # first response wins
                    winner[0] = elements
                    print(f"[overpass] {server} WON — {len(elements)} elements")
                    done_evt.set()
                else:
                    print(f"[overpass] {server} OK (late, discarded)")
        except Exception as e:
            print(f"[overpass] {server} failed: {e}")
            with lock:
                errors.append(e)
                if len(errors) == len(_OVERPASS_SERVERS):
                    done_evt.set()   # all failed — unblock

    threads = [threading.Thread(target=try_server, args=(s,), daemon=True)
               for s in _OVERPASS_SERVERS]
    for t in threads: t.start()

    done_evt.wait(timeout=10)   # 1s grace beyond per-server timeout

    if winner[0] is not None:
        return winner[0]
    raise RuntimeError(f"All {len(_OVERPASS_SERVERS)} Overpass servers failed")


def _parse_overpass(elements, lat, lon):
    """Parse combined Overpass result into per-category dicts."""
    roads, power, wetlands_els, streams_els, industrial_els, brownfield_els, dam_els = [], [], [], [], [], [], []

    for el in elements:
        tags = el.get("tags", {})
        hw   = tags.get("highway", "")
        pw   = tags.get("power", "")
        nat  = tags.get("natural", "")
        ww   = tags.get("waterway", "")
        lu   = tags.get("landuse", "")
        am   = tags.get("amenity", "")
        mm   = tags.get("man_made", "")
        # classify
        center = el.get("center", {})
        elat = center.get("lat") or el.get("lat")
        elon = center.get("lon") or el.get("lon")

        def dist_m(la, lo):
            if la is None or lo is None: return 9999
            R = 6371000
            dlat = math.radians(la - lat); dlon = math.radians(lo - lon)
            a = math.sin(dlat/2)**2 + math.cos(math.radians(lat))*math.cos(math.radians(la))*math.sin(dlon/2)**2
            return R * 2 * math.asin(math.sqrt(a))

        d = dist_m(elat, elon)
        if hw: roads.append((d, tags))
        if pw in ("line","minor_line"): power.append((d, tags))
        if nat in ("wetland","water") or (lu == "wetland"): wetlands_els.append((d, tags))
        if ww in ("stream","river","canal") and d <= 400: streams_els.append((d, tags))
        if lu in ("industrial","landfill","quarry") or am == "waste_disposal": industrial_els.append((d, tags))
        if lu in ("brownfield","contaminated") or am == "waste_disposal": brownfield_els.append((d, tags))
        if ww in ("dam","weir","lock_gate") or mm == "dam": dam_els.append((d, tags))

    # Roads
    if roads:
        roads.sort(key=lambda x: x[0])
        hw = roads[0][1].get("highway","unknown")
        name = roads[0][1].get("name", hw)
        if hw in ("primary","secondary","tertiary","residential","trunk","living_street","unclassified"):
            roads_r = {"access": True, "flag": "ROAD ACCESS", "cls": "green",
                       "label": f"{name} ({hw})", "road_type": hw, "count": len(roads)}
        elif hw in ("track","path","service"):
            roads_r = {"access": True, "flag": "UNIMPROVED ROAD", "cls": "yellow",
                       "label": f"{name} ({hw})", "road_type": hw, "count": len(roads)}
        else:
            roads_r = {"access": True, "flag": "ROAD ACCESS", "cls": "green",
                       "label": f"{name} ({hw})", "road_type": hw, "count": len(roads)}
    else:
        roads_r = {"access": False, "flag": "POSSIBLE LANDLOCKED", "cls": "red",
                   "label": "No roads within 150m — verify easement", "road_type": None, "count": 0}

    # Power
    if power:
        power_r = {"available": True, "flag": "POWER LINE NEARBY", "cls": "green",
                   "label": f"{len(power)} power line(s) within 800m"}
    else:
        power_r = {"available": False, "flag": "NO POWER NEARBY", "cls": "red",
                   "label": "No power infrastructure within 800m — estimate extension cost"}

    # Wetlands
    if wetlands_els:
        types = list({t.get("wetland","wetland") or t.get("natural","wetland") for _,t in wetlands_els})
        high = any(x in str(types).lower() for x in ["bog","marsh","fen"])
        wetlands_r = {"present": True, "count": len(wetlands_els), "types": types[:3],
                      "flag": "WETLANDS PRESENT", "cls": "red" if high else "yellow",
                      "label": f"{len(wetlands_els)} wetland(s) within 200m — delineation required"}
    else:
        wetlands_r = {"present": False, "flag": "CLEAR", "cls": "green",
                      "label": "No wetlands within 200m", "types": [], "count": 0}

    # Streams
    if streams_els:
        names = [t.get("name","") for _,t in streams_els if t.get("name")]
        label = names[0] if names else f"{len(streams_els)} waterway(s)"
        streams_r = {"present": True, "count": len(streams_els),
                     "flag": "STREAM NEARBY", "cls": "yellow",
                     "label": f"{label} within 400m — verify jurisdiction"}
    else:
        streams_r = {"present": False, "flag": "CLEAR", "cls": "green",
                     "label": "No streams within 400m", "count": 0}

    # EPA / industrial
    bad_kw = ["landfill","quarry","brownfield","waste","dump"]
    bad_ind = [t.get("name","Unnamed industrial") for _,t in industrial_els
               if any(k in t.get("landuse","").lower() for k in bad_kw)]
    if bad_ind:
        epa_r = {"count": len(industrial_els), "flag": "HIGH-RISK SITE NEARBY", "cls": "red",
                 "label": f"{bad_ind[0]} within 1 mi — verify", "items": bad_ind[:5]}
    elif industrial_els:
        names_i = [t.get("name","Industrial") for _,t in industrial_els[:5]]
        epa_r = {"count": len(industrial_els), "flag": f"{len(industrial_els)} INDUSTRIAL NEARBY", "cls": "yellow",
                 "label": f"{len(industrial_els)} industrial site(s) within 1 mi — review", "items": names_i}
    else:
        epa_r = {"count": 0, "flag": "CLEAR", "cls": "green",
                 "label": "No industrial/hazardous land within 1 mile", "items": []}

    # Superfund / brownfield
    if brownfield_els:
        names_b = [t.get("name","Contaminated site") for _,t in brownfield_els[:3]]
        super_r = {"count": len(brownfield_els), "flag": f"{len(brownfield_els)} CONTAMINATED SITE(S)", "cls": "red",
                   "label": f"{names_b[0]} within 2 miles — check EPA NPL", "sites": names_b}
    else:
        super_r = {"count": 0, "flag": "CLEAR", "cls": "green",
                   "label": "No known brownfields within 2 miles — verify EPA NPL", "sites": []}

    # Dams
    if dam_els:
        name_d = dam_els[0][1].get("name","Unnamed dam")
        wtype  = dam_els[0][1].get("waterway","dam")
        dams_r = {"count": len(dam_els), "flag": "DAM/WEIR WITHIN 2MI", "cls": "yellow",
                  "label": f"{name_d} ({wtype}) — verify flood impact", "hazard": "U"}
    else:
        dams_r = {"count": 0, "flag": "CLEAR", "cls": "green",
                  "label": "No dams within 2 miles", "hazard": None}

    return roads_r, power_r, wetlands_r, streams_r, epa_r, super_r, dams_r


def get_road_access(lat, lon):
    # Stub — real call goes through get_all_overpass
    return {"access": True, "flag": "CHECK MANUALLY", "cls": "grey", "label": "Use get_all_overpass"}


def get_all_overpass(lat, lon):
    """Fetch all OSM data in one request, return (roads, power, wetlands, streams, epa, superfund, dams)."""
    try:
        elements = _overpass_bulk(lat, lon)
        return _parse_overpass(elements, lat, lon)
    except Exception as e:
        print(f"[overpass_bulk] {e}")
        err = {"flag": "CHECK MANUALLY", "cls": "grey", "label": str(e)[:60]}
        return (
            {**err, "access": True, "road_type": None, "count": 0},
            {**err, "available": None},
            {**err, "present": False, "types": [], "count": 0},
            {**err, "present": False, "count": 0},
            {**err, "count": 0, "items": []},
            {**err, "count": 0, "sites": []},
            {**err, "count": 0, "hazard": None},
        )

# Power/wetlands/streams/epa/superfund/dams are all handled by get_all_overpass() below.
# These stubs exist only in case something calls them directly.
def get_power_lines(lat, lon):
    return {"available": None, "flag": "CHECK MANUALLY", "cls": "grey", "label": "Use get_all_overpass"}

# ─────────────────────────────────────────────
# 2. SOIL — USDA Web Soil Survey (free)
# ─────────────────────────────────────────────

def get_soil(lat, lon):
    try:
        # Step 1: get mukey for the point (retry once on empty response)
        q1 = f"SELECT mukey FROM SDA_Get_Mukey_from_intersection_with_WktWgs84('POINT({lon} {lat})')"
        r1 = None
        for attempt in range(2):
            try:
                r1 = http_post("https://sdmdataaccess.nrcs.usda.gov/Tabular/SDMTabularService/post.rest",
                               {"query": q1, "format": "JSON"}, timeout=10)
                if r1.get("Table"): break
            except Exception:
                if attempt == 0: time.sleep(1)
        rows1 = (r1 or {}).get("Table", [])
        if not rows1:
            return {"flag": "NO SOIL DATA", "cls": "grey", "label": "No WSS data for location",
                    "muname": "", "drainage": "", "septic_rating": "Unknown", "hydric": False}
        mukey = rows1[0][0]
        # Step 2: get muname + drainage for that mukey
        q2 = (f"SELECT TOP 1 mu.muname, c.drainagecl, c.comppct_r "
              f"FROM mapunit mu JOIN component c ON c.mukey=mu.mukey AND c.majcompflag='Yes' "
              f"WHERE mu.mukey='{mukey}' ORDER BY c.comppct_r DESC")
        r2 = http_post("https://sdmdataaccess.nrcs.usda.gov/Tabular/SDMTabularService/post.rest",
                       {"query": q2, "format": "JSON"}, timeout=8)
        rows2 = r2.get("Table", [])
        if rows2:
            # pick first row that has drainage data
            row = next((r for r in rows2 if r[1]), rows2[0])
            muname   = row[0] or "Unknown"
            drainage = row[1] or "Unknown"
            dl = (drainage or "").lower()
            # True hydric = "poorly drained" or "very poorly drained" ONLY
            # "Somewhat poorly drained" is NOT hydric — separate check
            hydric = ("very poorly" in dl) or (("poorly drained" in dl) and ("somewhat" not in dl))
            if hydric:
                flag, cls, rating = "HYDRIC SOIL", "red", "Very Limited"
            elif "somewhat poorly" in dl:
                flag, cls, rating = "SOMEWHAT LIMITED", "yellow", "Somewhat Limited"
            elif "somewhat" in dl:
                flag, cls, rating = "SOMEWHAT LIMITED", "yellow", "Somewhat Limited"
            elif dl and dl not in ["unknown", "well drained", "moderately well drained"]:
                flag, cls, rating = "CHECK DRAINAGE", "yellow", "Review"
            else:
                flag, cls, rating = "WELL DRAINED", "green", "Not Limited"
            return {"muname": muname, "drainage": drainage, "septic_rating": rating,
                    "flag": flag, "cls": cls,
                    "label": f"{muname} — {drainage}",
                    "hydric": hydric, "hydric_flag": "HYDRIC SOIL DETECTED" if hydric else None}
        return {"flag": "NO SOIL DATA", "cls": "grey", "label": "No WSS data for location",
                "muname": "", "drainage": "", "septic_rating": "Unknown", "hydric": False}
    except Exception as e:
        print(f"[soil] {e}")
        return {"flag": "CHECK MANUALLY", "cls": "grey", "label": str(e)[:60],
                "muname": "", "drainage": "", "septic_rating": "Unknown", "hydric": False}

# ─────────────────────────────────────────────
# 3. SLOPE — USGS 3DEP 4-point elevation (free)
# ─────────────────────────────────────────────

def _elev(lat, lon):
    try:
        url = f"https://epqs.nationalmap.gov/v1/json?x={lon}&y={lat}&wkid=4326&units=Feet&includeDate=false"
        for attempt in range(2):
            r = http_get(url, timeout=8)
            v = r.get("value")
            if v and v != "-1000000":
                return float(v)
            if attempt == 0: time.sleep(1)
        return None
    except: return None

def get_slope(lat, lon):
    try:
        offset = 0.0014
        coords = [(lat, lon), (lat+offset,lon), (lat-offset,lon),
                  (lat,lon+offset), (lat,lon-offset)]
        results = [None] * len(coords)
        def _fetch(i, la, lo):
            results[i] = _elev(la, lo)
        ts = [threading.Thread(target=_fetch, args=(i, la, lo), daemon=True)
              for i, (la, lo) in enumerate(coords)]
        for t in ts: t.start()
        for t in ts: t.join(timeout=7)
        center = results[0]
        if center is None:
            return {"flag": "NO DATA", "cls": "grey", "label": "Elevation data unavailable",
                    "elevation_ft": None, "slope_pct": None}
        valid = [p for p in results[1:] if p is not None]
        slope_pct = (max(abs(center-p) for p in valid) / 492 * 100) if valid else 0
        if slope_pct < 10:   flag, cls, terrain = "FLAT — BUILDABLE", "green", "Flat to gently rolling"
        elif slope_pct < 20: flag, cls, terrain = "ROLLING TERRAIN", "yellow", "Rolling — moderate site prep"
        elif slope_pct < 30: flag, cls, terrain = "STEEP TERRAIN", "red", "Steep — significant grading"
        else:                flag, cls, terrain = "VERY STEEP", "red", "Very steep — likely unbuildable"
        return {"flag": flag, "cls": cls, "label": f"{terrain} | {int(center)}ft elevation",
                "elevation_ft": int(center), "slope_pct": round(slope_pct, 1)}
    except Exception as e:
        print(f"[slope] {e}")
        return {"flag": "CHECK MANUALLY", "cls": "grey", "label": str(e)[:60],
                "elevation_ft": None, "slope_pct": None}

# ─────────────────────────────────────────────
# 4. ENVIRONMENTAL — Flood + Wetlands + Streams (all free)
# ─────────────────────────────────────────────

def get_flood_zone(lat, lon):
    try:
        url = (f"https://msc.fema.gov/arcgis/rest/services/NFHL_Print/NFHL/MapServer/14/query"
               f"?geometry={lon},{lat}&geometryType=esriGeometryPoint"
               f"&spatialRel=esriSpatialRelIntersects&outFields=FLD_ZONE,SFHA_TF&f=json&inSR=4326")
        data = http_get(url, timeout=10)
        features = data.get("features", [])
        if features:
            attrs = features[0].get("attributes", {})
            zone = attrs.get("FLD_ZONE", "X")
            # Zone is the authority — SFHA_TF can be T on Zone X-500 (0.2% chance), ignore it
            high = zone in ("A","AE","AO","AH","AR","A99","V","VE","VO")
            return {"zone": zone, "high_risk": high,
                    "flag": f"ZONE {zone} — HIGH RISK" if high else f"ZONE {zone} — LOW RISK",
                    "cls": "red" if high else "green",
                    "label": f"FEMA Zone {zone}" + (" — call floodplain admin" if high else "")}
        return {"zone": "X", "high_risk": False, "flag": "ZONE X — MINIMAL", "cls": "green",
                "label": "Zone X — minimal flood hazard"}
    except Exception as e:
        print(f"[flood] {e}")
        return {"zone": "ERR", "high_risk": False, "flag": "CHECK MANUALLY", "cls": "grey", "label": str(e)[:60]}

def get_wetlands(lat, lon):
    """USFWS National Wetlands Inventory — ArcGIS REST, no key, very reliable."""
    try:
        url = (
            "https://fwsprimary.wim.usgs.gov/server/rest/services/wetlands/MapServer/0/query"
            f"?geometry={lon},{lat}&geometryType=esriGeometryPoint&inSR=4326"
            f"&spatialRel=esriSpatialRelIntersects&distance=200&units=esriSRUnit_Meter"
            f"&outFields=WETLAND_TYPE,ACRES&returnGeometry=false&f=json"
        )
        data = http_get(url, timeout=8)
        features = data.get("features", [])
        if features:
            types = list({f["attributes"].get("WETLAND_TYPE", "Wetland") for f in features})
            # Palustrine/Emergent = high concern for delineation
            high = any(x in " ".join(types).upper() for x in ["PALUSTRINE","EMERGENT","FORESTED","SCRUB"])
            return {
                "present": True, "count": len(features), "types": types[:3],
                "flag": "WETLANDS PRESENT", "cls": "red" if high else "yellow",
                "label": f"{len(features)} NWI wetland(s) within 200m — delineation required"
            }
        return {"present": False, "flag": "CLEAR", "cls": "green",
                "label": "No NWI wetlands within 200m", "types": [], "count": 0}
    except Exception as e:
        print(f"[nwi] {e}")
        return {"present": False, "flag": "CHECK NWI", "cls": "grey",
                "label": "NWI service unavailable — verify manually", "types": [], "count": 0}

def get_streams(lat, lon):
    """USGS NHD via ArcGIS REST — streams/rivers within 400m."""
    try:
        url = (
            "https://hydro.nationalmap.gov/arcgis/rest/services/nhd/MapServer/6/query"
            f"?geometry={lon},{lat}&geometryType=esriGeometryPoint&inSR=4326"
            f"&spatialRel=esriSpatialRelIntersects&distance=400&units=esriSRUnit_Meter"
            f"&outFields=GNIS_NAME,FTYPE&returnGeometry=false&f=json"
        )
        data = http_get(url, timeout=8)
        features = data.get("features", [])
        if features:
            names = [f["attributes"].get("GNIS_NAME","") for f in features if f["attributes"].get("GNIS_NAME")]
            label = names[0] if names else f"{len(features)} waterway(s)"
            return {"present": True, "count": len(features),
                    "flag": "STREAM NEARBY", "cls": "yellow",
                    "label": f"{label} within 400m — verify jurisdiction"}
        return {"present": False, "flag": "CLEAR", "cls": "green",
                "label": "No streams within 400m", "count": 0}
    except Exception as e:
        print(f"[nhd] {e}")
        return {"present": False, "flag": "CHECK NHD", "cls": "grey",
                "label": "Stream data unavailable", "count": 0}

# ─────────────────────────────────────────────
# EPA ECHO — CAFOs / industrial facilities (free REST API)
# ─────────────────────────────────────────────

def get_epa_echo(lat, lon):
    """EPA ECHO facility search — no API key, official EPA REST endpoint."""
    try:
        url = (
            "https://echodata.epa.gov/echo/echo_rest_services.get_facilities"
            f"?output=JSON&p_lat={lat}&p_long={lon}&p_radius=1&p_act=Y"
        )
        data = http_get(url, timeout=10)
        facilities = data.get("Results", {}).get("Facilities", [])
        if not facilities:
            return {"count": 0, "flag": "CLEAR", "cls": "green",
                    "label": "No active EPA facilities within 1 mile", "items": []}
        # Classify by severity
        high_kw = ["landfill","superfund","cafo","hazardous","waste","sewer","treatment"]
        names   = [f.get("FacName","Facility") for f in facilities[:8]]
        high    = any(any(k in n.lower() for k in high_kw) for n in names)
        cls     = "red" if high else "yellow"
        flag    = "HIGH-RISK EPA FACILITY" if high else f"{len(facilities)} EPA FACILITIES"
        return {"count": len(facilities), "flag": flag, "cls": cls,
                "label": f"{names[0]} within 1 mi — verify", "items": names[:5]}
    except Exception as e:
        print(f"[echo] {e}")
        return {"count": 0, "flag": "CHECK EPA ECHO", "cls": "grey",
                "label": "EPA ECHO unavailable — check manually", "items": []}

# ─────────────────────────────────────────────
# EPA CERCLIS — Superfund sites (free, no key)
# ─────────────────────────────────────────────

def get_superfund(lat, lon):
    """EPA ECHO Superfund/CERCLIS query — within 2 miles."""
    try:
        url = (
            "https://echodata.epa.gov/echo/echo_rest_services.get_facilities"
            f"?output=JSON&p_lat={lat}&p_long={lon}&p_radius=2"
            f"&p_act=Y&p_pca=Y"   # p_pca = CERCLA/Superfund flag
        )
        data = http_get(url, timeout=10)
        facilities = data.get("Results", {}).get("Facilities", [])
        # Filter to superfund/brownfield designations
        sf = [f for f in facilities if f.get("CerclisFlag","") == "Y"
              or "superfund" in f.get("FacName","").lower()
              or f.get("RcraFlag","") == "Y"]
        if sf:
            names = [f.get("FacName","Contaminated site") for f in sf[:3]]
            return {"count": len(sf), "flag": f"{len(sf)} SUPERFUND/RCRA SITE(S)", "cls": "red",
                    "label": f"{names[0]} within 2 miles — check EPA NPL", "sites": names}
        return {"count": 0, "flag": "CLEAR", "cls": "green",
                "label": "No Superfund/RCRA sites within 2 miles — verified EPA ECHO", "sites": []}
    except Exception as e:
        print(f"[superfund] {e}")
        return {"count": 0, "flag": "CHECK EPA", "cls": "grey",
                "label": "Superfund data unavailable — verify EPA NPL", "sites": []}

# ─────────────────────────────────────────────
# USACE NID — Dams within 2 miles (free)
# ─────────────────────────────────────────────

def get_dams(lat, lon):
    return {"count": 0, "flag": "CHECK MANUALLY", "cls": "grey", "label": "Use get_all_overpass", "hazard": None}

# ─────────────────────────────────────────────
# ZONING — Franklin County Auditor GIS + Columbus City GIS (free!)
# ─────────────────────────────────────────────

def get_zoning(lat, lon):
    """Try multiple GIS sources for Columbus/Franklin County zoning."""
    point_geom = json.dumps({"x": lon, "y": lat, "spatialReference": {"wkid": 4326}})
    base_params = (f"?geometry={urllib.parse.quote(point_geom)}"
                   f"&geometryType=esriGeometryPoint&inSR=4326"
                   f"&spatialRel=esriSpatialRelIntersects&outFields=*&returnGeometry=false&f=json")

    sources = [
        # Columbus City Base Zoning — Layer 20 (confirmed correct layer)
        ("Columbus Base Zoning",
         "https://maps2.columbus.gov/arcgis/rest/services/Applications/Zoning/MapServer/20/query",
         "columbus"),
        # Franklin County parcel (fallback for non-Columbus addresses)
        ("Franklin County Auditor",
         "https://gis.franklincountyohio.gov/hosting/rest/services/ParcelFeatures/Parcel_Features/MapServer/0/query",
         "franklin"),
    ]

    for source_name, base_url, src_type in sources:
        try:
            url = base_url + base_params
            data = http_get(url, timeout=10)
            features = data.get("features", [])
            if not features:
                continue
            attrs = features[0].get("attributes", {})

            if src_type == "columbus":
                # Layer 20 fields: CLASSIFICATION (code), GENERAL_ZONING_CATEGORY (desc), HEIGHT_DISTRICT
                zone = str(attrs.get("CLASSIFICATION", "") or "").strip()
                desc = str(attrs.get("GENERAL_ZONING_CATEGORY", "") or "").strip()
                height = str(attrs.get("HEIGHT_DISTRICT", "") or "").strip()
            else:
                # Franklin County parcel — use CLASSDSCRP as fallback description
                zone = str(attrs.get("CLASSCD", "") or "").strip()
                desc = str(attrs.get("CLASSDSCRP", "") or "").strip()
                height = ""

            if zone and zone not in ("None", "0", ""):
                residential = any(x in zone.upper() for x in
                    ["R","ARLD","AR","SR","RR","AG","SFR","MFR","RESI"])
                residential = residential or any(x in desc.upper() for x in
                    ["RESID","SINGLE","MULTI","FAMILY","VACANT LAND"])
                flag = zone.upper()
                cls  = "green" if residential else "yellow"
                full_desc = desc
                if height: full_desc += f" | {height}"
                label = f"{full_desc} — {source_name}" if full_desc else source_name
                # Pull county assessed values — convert to true (100%) market value
                county_land_value = float(attrs.get("LNDVALUEBASE") or attrs.get("LNDVALUE") or 0)
                county_total_assessed = float(attrs.get("TOTVALUEBASE") or county_land_value or 0)
                county_true_value = round(county_total_assessed / 0.35)
                return {"zone": zone, "description": full_desc, "flag": flag, "cls": cls,
                        "label": label, "source": source_name,
                        "county_land_value": county_land_value,
                        "county_true_value": county_true_value}
            elif desc and src_type == "franklin":
                # No zone code but has classification description
                residential = any(x in desc.upper() for x in ["RESID","VACANT LAND","LOT"])
                county_land_value = float(attrs.get("LNDVALUEBASE") or attrs.get("LNDVALUE") or 0)
                county_total_assessed = float(attrs.get("TOTVALUEBASE") or county_land_value or 0)
                county_true_value = round(county_total_assessed / 0.35)
                return {"zone": desc, "description": desc, "flag": desc.upper()[:30], "cls": "green" if residential else "yellow",
                        "label": f"{desc} — {source_name}", "source": source_name,
                        "county_land_value": county_land_value, "county_true_value": county_true_value}
        except Exception as e:
            print(f"[zoning:{source_name}] {e}")
            continue

    return {"zone": None, "flag": "ZONING UNAVAILABLE", "cls": "grey",
            "label": "Could not retrieve from county/city GIS", "source": None}

# ─────────────────────────────────────────────
# DRIVE TIME — OSRM to Columbus downtown (free)
# ─────────────────────────────────────────────

COLUMBUS_DOWNTOWN = (-83.0007, 39.9612)  # Broad & High
COLUMBUS_WALMART  = (-83.0458, 39.9882)  # nearest major store

def get_drive_time(lat, lon):
    try:
        # Drive time to Columbus downtown + nearest Walmart
        coords = f"{lon},{lat};{COLUMBUS_DOWNTOWN[0]},{COLUMBUS_DOWNTOWN[1]};{COLUMBUS_WALMART[0]},{COLUMBUS_WALMART[1]}"
        url = f"http://router.project-osrm.org/table/v1/driving/{coords}?annotations=duration"
        data = http_get(url, timeout=15)
        durations = data.get("durations", [[]])[0]
        dt_downtown = int(durations[1] / 60) if len(durations) > 1 else None
        dt_walmart  = int(durations[2] / 60) if len(durations) > 2 else None
        if dt_downtown is not None:
            cls = "green" if dt_downtown < 20 else "yellow" if dt_downtown < 45 else "red"
            return {"minutes_downtown": dt_downtown, "minutes_walmart": dt_walmart,
                    "flag": f"{dt_downtown} MIN TO COLUMBUS", "cls": cls,
                    "label": f"{dt_downtown} min downtown | {dt_walmart} min to Walmart"}
        return {"flag": "NO DATA", "cls": "grey", "label": "OSRM routing unavailable"}
    except Exception as e:
        print(f"[drive_time] {e}")
        return {"flag": "CHECK MANUALLY", "cls": "grey", "label": str(e)[:60]}

# ─────────────────────────────────────────────
# COMPS — Franklin County Auditor recent land sales (free, no key)
# Falls back to GHL contact comps if live query fails or returns < 2 results
# ─────────────────────────────────────────────

def _arcgis_comps_query(lat, lon, miles, cutoff_dt, classcd_filter=True):
    s, w, n, e = bbox(lat, lon, miles)
    where = f"SALEDATE >= timestamp '{cutoff_dt}' AND SALEPRICE>500 AND STATEDAREA>0"
    if classcd_filter:
        # Only pull vacant land parcels — Franklin County uses:
        #   500 = vacant residential land, 501 = vacant residential w/ minor use
        #   400 = vacant commercial land
        where += " AND CLASSCD IN (400, 500, 501)"
    params = urllib.parse.urlencode({
        "geometry": f"{w},{s},{e},{n}",
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": "4326",
        "where": where,
        "outFields": "SITEADDRESS,ZIPCD,STATEDAREA,SALEPRICE,SALEDATE,CLASSCD,PARCELID",
        "returnGeometry": "false",
        "orderByFields": "SALEDATE DESC",
        "resultRecordCount": 100,
        "f": "json"
    })
    url = ("https://gis.franklincountyohio.gov/hosting/rest/services"
           "/ParcelFeatures/Parcel_Features/MapServer/0/query?" + params)
    return http_get(url, timeout=10).get("features", [])

def get_comps(lat, lon, acreage, contact_comps=None):
    try:
        import datetime
        cutoff_dt = (datetime.datetime.utcnow() - datetime.timedelta(days=3*365)).strftime("%Y-%m-%d %H:%M:%S")
        # 1) Vacant land only (CLASSCD 400/500/501), 5mi then 15mi
        broadened = False
        features = _arcgis_comps_query(lat, lon, 5.0, cutoff_dt, classcd_filter=True)
        if not features:
            features = _arcgis_comps_query(lat, lon, 15.0, cutoff_dt, classcd_filter=True)
            if features:
                print(f"[comps] vacant 5mi dry — expanded to 15mi, got {len(features)} features")
        # 2) If still empty, broaden to ALL class codes with expanding radius
        for broad_mi in (3.0, 8.0):
            if not features:
                features = _arcgis_comps_query(lat, lon, broad_mi, cutoff_dt, classcd_filter=False)
                if features:
                    broadened = True
                    print(f"[comps] no vacant land sales — broadened to all classes {broad_mi}mi, got {len(features)} features")
                    break
        print(f"[comps] raw query returned {len(features)} features at ({lat:.4f},{lon:.4f})")

        results = []
        subject_acres = float(acreage) if acreage else 0
        skip_basic = skip_classcd = skip_ratio = 0
        # Full scan for diagnostics — see distribution across all 50 raw features
        classcd_counts = {}
        for feat in features:
            cd = str(feat.get("attributes", {}).get("CLASSCD") or "")
            classcd_counts[cd] = classcd_counts.get(cd, 0) + 1
        for feat in features:
            a = feat.get("attributes", {})
            raw_area = a.get("STATEDAREA") or 0
            # STATEDAREA is sq ft when > 100, acres when <= 100
            acres = raw_area / 43560.0 if raw_area > 100 else float(raw_area)
            price = a.get("SALEPRICE") or 0
            address = (a.get("SITEADDRESS") or "").strip()
            zipcd = (a.get("ZIPCD") or "").strip()
            sale_ts = a.get("SALEDATE")  # epoch ms from response
            classcd = str(a.get("CLASSCD") or "")
            # Tiny lot mode: relax acre floor for sub-0.1 acre subjects
            tiny_lot = subject_acres > 0 and subject_acres < 0.1
            acre_floor = 0.01 if tiny_lot else 0.05
            if not address or price < 500 or acres < acre_floor:
                skip_basic += 1; continue
            # Classcd filtering depends on search mode:
            # Strict (vacant land found): only keep 400/500/501
            # Broadened (no vacant land nearby): only exclude houses/condos (510+)
            #   — keep commercial codes (400s) since $/ac cap guards inflated values
            if classcd and classcd.isdigit():
                cd = int(classcd)
                if broadened:
                    # Only exclude residential improved: houses, duplexes, condos
                    if cd >= 510:
                        skip_classcd += 1; continue
                else:
                    # Strict: exclude everything except vacant land
                    if (100 <= cd <= 199) or (401 <= cd <= 499) or (510 <= cd <= 599):
                        skip_classcd += 1; continue
            # Filter: acreage ratio — widen for tiny lots (<0.1ac) to 10x/0.1x
            if subject_acres > 0:
                ratio = acres / subject_acres
                max_ratio = 10 if tiny_lot else 5
                min_ratio = 0.1 if tiny_lot else 0.2
                if ratio > max_ratio or ratio < min_ratio:
                    skip_ratio += 1; continue
            per_acre = int(price / acres) if acres else 0
            # Sanity cap for non-vacant codes that slipped past classcd filter.
            # Known vacant land (400/500/501) is exempt — urban vacant lots
            # legitimately sell above $200k/ac in Columbus metro.
            is_vacant_code = classcd.isdigit() and int(classcd) in (400, 500, 501)
            if not is_vacant_code and per_acre > 200000:
                skip_basic += 1; continue
            sale_date = ""
            if sale_ts:
                sale_date = datetime.datetime.utcfromtimestamp(sale_ts / 1000).strftime("%Y-%m-%d")
            # Zillow search link — use address + zip
            addr_slug = address.replace(" ", "-").replace(",", "").replace(".", "")
            zillow_url = f"https://www.zillow.com/homes/{addr_slug}-Columbus-OH-{zipcd}_rb/"
            google_url = f"https://maps.google.com/?q={urllib.parse.quote(address + ' Columbus OH ' + zipcd)}"
            results.append({
                "address": address,
                "acres": round(float(acres), 2),
                "date": sale_date,
                "price": price,
                "per_acre": per_acre,
                "zillow": zillow_url,
                "google": google_url,
            })

        # Last resort for tiny lots: if 0 comps, allow houses (510) as land proxies
        # Use sale price directly — these are lot-value indicators for urban parcels
        if not results and tiny_lot and features:
            print(f"[comps] tiny lot fallback — allowing houses (510) as land proxies")
            for feat in features:
                a = feat.get("attributes", {})
                raw_area = a.get("STATEDAREA") or 0
                acres_f = raw_area / 43560.0 if raw_area > 100 else float(raw_area)
                price_f = a.get("SALEPRICE") or 0
                address_f = (a.get("SITEADDRESS") or "").strip()
                zipcd_f = (a.get("ZIPCD") or "").strip()
                sale_ts_f = a.get("SALEDATE")
                classcd_f = str(a.get("CLASSCD") or "")
                if not address_f or price_f < 500 or acres_f < 0.01:
                    continue
                # Only take 510 (houses) — closest proxy for urban lot value
                if classcd_f != "510":
                    continue
                # Acreage ratio: within 3x of subject for tighter relevance
                if subject_acres > 0:
                    r = acres_f / subject_acres
                    if r > 3 or r < 0.33:
                        continue
                per_acre_f = int(price_f / acres_f) if acres_f else 0
                sale_date_f = ""
                if sale_ts_f:
                    sale_date_f = datetime.datetime.utcfromtimestamp(sale_ts_f / 1000).strftime("%Y-%m-%d")
                addr_slug_f = address_f.replace(" ", "-").replace(",", "").replace(".", "")
                results.append({
                    "address": address_f,
                    "acres": round(float(acres_f), 2),
                    "date": sale_date_f,
                    "price": price_f,
                    "per_acre": per_acre_f,
                    "zillow": f"https://www.zillow.com/homes/{addr_slug_f}-Columbus-OH-{zipcd_f}_rb/",
                    "google": f"https://maps.google.com/?q={urllib.parse.quote(address_f + ' Columbus OH ' + zipcd_f)}",
                    "source": "house_proxy",
                })
            # Sort by price (lowest = most conservative land value estimate)
            results = sorted(results, key=lambda c: c["price"])[:5]
            if results:
                print(f"[comps] house proxy: {len(results)} comps from 510 sales, prices: {[c['price'] for c in results]}")

        # Select 2 below avg + 1 above avg (bracketing: conservative bias)
        extra = []
        if len(results) >= 3:
            avg = sum(c["per_acre"] for c in results) / len(results)
            below = sorted([c for c in results if c["per_acre"] <= avg], key=lambda c: c["per_acre"], reverse=True)
            above = sorted([c for c in results if c["per_acre"] >  avg], key=lambda c: c["per_acre"])
            picked = below[:2] + above[:1]
            # Fill if one side is thin
            if len(picked) < 3:
                remaining = [c for c in results if c not in picked]
                picked += sorted(remaining, key=lambda c: abs(c["per_acre"] - avg))[:3 - len(picked)]
            # Remaining comps sorted by per_acre — sent after enrich_done as comps_extra
            extra = sorted([c for c in results if c not in picked], key=lambda c: c["per_acre"])[:10]
            results = sorted(picked, key=lambda c: c["per_acre"])
            print(f"[comps] bracket: pool={len(below)+len(above)} below={len(below)} above={len(above)} → picked ${'/'.join(str(c['per_acre']) for c in results)}/ac, extra={len(extra)}")

        # Fallback to GHL contact comps if live query came up short
        if len(results) < 2 and contact_comps:
            ghl = []
            for c in (contact_comps or []):
                addr = c.get("address","").strip()
                if not addr: continue
                try: acres = float(c.get("acres",0))
                except: acres = 0
                try: price = float(str(c.get("price","0")).replace("$","").replace(",",""))
                except: price = 0
                try: per_acre = float(str(c.get("per_acre","0")).replace("$","").replace(",",""))
                except: per_acre = 0
                addr_slug = addr.replace(" ", "-").replace(",","").replace(".","")
                ghl.append({
                    "address": addr,
                    "acres": round(acres, 3),
                    "date": c.get("date",""),
                    "price": int(price),
                    "per_acre": int(per_acre),
                    "zillow": f"https://www.zillow.com/homes/{addr_slug}-Columbus-OH_rb/",
                    "google": f"https://maps.google.com/?q={urllib.parse.quote(addr + ' Columbus OH')}",
                    "source": "ghl"
                })
            results = (results + ghl)[:3]

        print(f"[comps] filters: basic={skip_basic} classcd={skip_classcd} ratio={skip_ratio} → {len(results)} comps (subject={subject_acres}ac) classcd_dist={dict(sorted(classcd_counts.items()))}")
        if not results:
            return {"comps": [], "flag": "NO COMPS", "cls": "grey", "label": "No recent vacant land sales found nearby"}

        avg_per_acre = int(sum(c["per_acre"] for c in results) / len(results)) if results else 0
        avg_price    = int(sum(c["price"]    for c in results) / len(results)) if results else 0
        print(f"[comps] returning {len(results)} comps (+{len(extra)} extra), avg_per_acre=${avg_per_acre:,}, avg_price=${avg_price:,}")
        return {
            "comps": results,
            "extra": extra,
            "avg_per_acre": avg_per_acre,
            "avg_price": avg_price,
            "flag": f"${avg_per_acre:,}/ACRE AVG",
            "cls": "green",
            "label": f"{len(results)} comps — avg ${avg_per_acre:,}/acre"
        }
    except Exception as e:
        print(f"[comps] ERROR: {e}")
        return {"comps": [], "flag": "CHECK MANUALLY", "cls": "grey", "label": str(e)[:60]}


# ─────────────────────────────────────────────
# CENSUS ACS — ZIP vacancy rate + median income (free key required)
# ─────────────────────────────────────────────
_CENSUS_KEY = os.environ.get("CENSUS_API_KEY", "")

def get_census(zip_code):
    try:
        zip_clean = str(zip_code).strip()[:5]
        if not zip_clean.isdigit() or len(zip_clean) != 5:
            return {"flag": "NO DATA", "cls": "grey", "label": "Invalid ZIP code"}
        # B25002: Housing occupancy | B19013: Median household income
        fields = "B25002_001E,B25002_002E,B25002_003E,B19013_001E"
        key_param = f"&key={_CENSUS_KEY}" if _CENSUS_KEY else ""
        # Try 2022 ACS first, fall back to 2021
        for year in ("2022", "2021"):
            try:
                url = (f"https://api.census.gov/data/{year}/acs/acs5"
                       f"?get={fields}&for=zip%20code%20tabulation%20area:{zip_clean}{key_param}")
                data = http_get(url, timeout=10)
                if data and len(data) > 1:
                    row = data[1]
                    total    = int(row[0]) if row[0] and row[0] != "-666666666" else 0
                    occupied = int(row[1]) if row[1] and row[1] != "-666666666" else 0
                    vacant   = int(row[2]) if row[2] and row[2] != "-666666666" else 0
                    income   = int(row[3]) if row[3] and row[3] != "-666666666" else 0
                    vac_rate = round(vacant / total * 100, 1) if total > 0 else 0
                    cls = "red" if vac_rate > 15 else "yellow" if vac_rate > 8 else "green"
                    return {
                        "vacancy_rate": vac_rate, "median_income": income,
                        "total_units": total, "vacant_units": vacant,
                        "flag": f"{vac_rate}% VACANCY RATE", "cls": cls,
                        "income_fmt": f"${income:,}" if income > 0 else "N/A",
                        "label": f"ZIP {zip_clean}: {vac_rate}% vacancy | Median ${income:,}" if income else f"ZIP {zip_clean}: {vac_rate}% vacancy"
                    }
            except Exception as ye:
                print(f"[census:{year}] {ye}")
                continue
        return {"flag": "NO DATA", "cls": "grey", "label": f"No Census data for ZIP {zip_clean}"}
    except Exception as e:
        print(f"[census] {e}")
        return {"flag": "CHECK MANUALLY", "cls": "grey", "label": str(e)[:60]}

# ─────────────────────────────────────────────
# OFFER RANGE CALCULATOR
# ─────────────────────────────────────────────

def calc_offer_range(cma_value):
    try:
        cma = float(str(cma_value).replace(",","").replace("$",""))
        if cma <= 0: return None
        return {
            "cma": int(cma),
            "low": int(cma * 0.45), "mid": int(cma * 0.55),
            "high": int(cma * 0.65), "mao": int(cma * 0.65),
            "low_fmt":  f"${int(cma*0.45):,}", "mid_fmt":  f"${int(cma*0.55):,}",
            "high_fmt": f"${int(cma*0.65):,}", "mao_fmt":  f"${int(cma*0.65):,}",
            "range_fmt": f"${int(cma*0.45):,} – ${int(cma*0.65):,}"
        }
    except: return None

# ─────────────────────────────────────────────
# ASSESS SCORE (0–100)
# ─────────────────────────────────────────────

def calc_assess_score(e, contact):
    score = 100
    deductions = []

    flood_zone = e.get("flood",{}).get("zone","X")
    if e.get("flood",{}).get("high_risk"):
        score -= 25; deductions.append(f"Flood Zone {flood_zone} (-25)")
    if e.get("wetlands",{}).get("present"):         score -= 20; deductions.append("Wetlands present (-20)")
    # True hydric = poorly/very poorly drained — NOT "somewhat poorly drained"
    if e.get("soil",{}).get("hydric"):              score -= 15; deductions.append("Hydric soils (-15)")
    elif e.get("soil",{}).get("septic_rating") == "Somewhat Limited":
                                                    score -= 5;  deductions.append("Soil somewhat limited (-5)")
    if not e.get("roads",{}).get("access"):         score -= 20; deductions.append("Possible landlocked (-20)")
    elif e.get("roads",{}).get("cls") == "yellow":  score -= 5;  deductions.append("Unimproved road (-5)")
    slp = e.get("slope",{}).get("slope_pct")
    if slp and slp > 25:                            score -= 15; deductions.append(f"Steep slope {slp}% (-15)")
    elif slp and slp > 15:                          score -= 5;  deductions.append("Rolling terrain (-5)")
    if e.get("epa_echo",{}).get("cls") == "red":    score -= 15; deductions.append("High-risk EPA facility <1mi (-15)")
    if e.get("superfund",{}).get("count", 0) > 0:  score -= 10; deductions.append("Superfund site <2mi (-10)")
    if e.get("dams",{}).get("hazard") == "H":       score -= 10; deductions.append("High-hazard dam nearby (-10)")
    if e.get("streams",{}).get("present"):          score -= 5;  deductions.append("Stream nearby (-5)")
    if not e.get("power",{}).get("available"):      score -= 5;  deductions.append("No power infrastructure (-5)")

    score = max(0, score)
    if score >= 80:   grade, gc = "A", "green"
    elif score >= 65: grade, gc = "B", "green"
    elif score >= 50: grade, gc = "C", "yellow"
    elif score >= 35: grade, gc = "D", "red"
    else:             grade, gc = "F", "red"

    auto_dq = score < 35 or (e.get("flood",{}).get("high_risk") and e.get("wetlands",{}).get("present"))
    return {"score": score, "grade": grade, "grade_cls": gc,
            "deductions": deductions, "auto_disqualify": auto_dq}

# ─────────────────────────────────────────────
# COUNTY ESCALATIONS — auto-generated from flags
# ─────────────────────────────────────────────

def build_escalations(e, contact):
    calls = []
    if e.get("flood",{}).get("high_risk"):
        calls.append({"office":"Floodplain Administrator","when":"Zone A/AE detected",
                      "ask":"What's the Base Flood Elevation? Any LOMA on file? Freeboard req?"})
    if e.get("wetlands",{}).get("present"):
        calls.append({"office":"Army Corps / County EH","when":"Wetlands within 200m",
                      "ask":"Jurisdictional? Delineation on file? Any 404 permits issued?"})
    if e.get("soil",{}).get("septic_rating") in ("Somewhat Limited","Very Limited"):
        calls.append({"office":"Environmental Health","when":f"Soil {e['soil']['septic_rating']} for septic",
                      "ask":"Prior perc tests? Failed permits? Approved septic on file?"})
    if not e.get("roads",{}).get("access"):
        calls.append({"office":"Register of Deeds","when":"No road frontage detected",
                      "ask":"Recorded access easement? Book/page? Title insurable?"})
    if e.get("superfund",{}).get("count",0) > 0:
        calls.append({"office":"County EH / OEPA","when":"Superfund site within 2 miles",
                      "ask":"Does contamination plume affect this parcel? Vapor intrusion risk?"})
    if e.get("epa_echo",{}).get("cls") == "red":
        calls.append({"office":"County Zoning","when":"High-risk EPA facility nearby",
                      "ask":"DEQ buffer requirements? Odor/contamination complaints on file?"})
    if e.get("dams",{}).get("hazard") == "H":
        calls.append({"office":"USACE / County Engineer","when":"High-hazard dam within 2 miles",
                      "ask":"Inundation zone map? Emergency action plan affect this parcel?"})
    calls.append({"office":"Central Permitting","when":"Always",
                  "ask":"Open, expired, or denied permits on this parcel? Code violations?"})
    return calls

# ─────────────────────────────────────────────
# MASTER ENRICHMENT — runs all APIs in parallel
# ─────────────────────────────────────────────

def enrich_property(prop_address, city_state, contact):
    # 1. Prefer parcel-level centroid (exact polygon) — works for road-name-only addresses
    parcel_id = contact.get("parcel_id", "")
    lat, lon = parcel_centroid(parcel_id)
    source = "parcel_gis"
    # 2. Fall back to address geocoding
    if not (lat and lon):
        full = f"{prop_address}, {city_state}"
        lat, lon = geocode(full)
        source = "geocode"
    if not (lat and lon):
        print(f"[enrich] GEOCODE FAILED — parcel_id={parcel_id!r} addr={prop_address!r}")
        return {"error": "Geocode failed", "geocode": {"success": False}}

    store = {}
    zip_code = contact.get("zip_code","43215")

    def run(key, fn, args):
        try: store[key] = fn(*args)
        except Exception as ex: store[key] = {"flag":"ERROR","cls":"grey","label":str(ex)[:60]}

    # Single Overpass request for all OSM data (roads, power, wetlands, streams, EPA, superfund, dams)
    def run_overpass():
        try:
            roads, power, wetlands, streams, epa, superfund, dams = get_all_overpass(lat, lon)
            store["roads"]    = roads
            store["power"]    = power
            store["wetlands"] = wetlands
            store["streams"]  = streams
            store["epa_echo"] = epa
            store["superfund"]= superfund
            store["dams"]     = dams
        except Exception as ex:
            err = {"flag":"ERROR","cls":"grey","label":str(ex)[:60]}
            for k in ("roads","power","wetlands","streams","epa_echo","superfund","dams"):
                store[k] = err

    # Non-Overpass tasks run in parallel
    acreage = contact.get("acreage", 0)
    contact_comps = contact.get("comps", [])
    tasks = [
        ("soil",   get_soil,       (lat, lon)),
        ("slope",  get_slope,      (lat, lon)),
        ("flood",  get_flood_zone, (lat, lon)),
        ("zoning", get_zoning,     (lat, lon)),
        ("drive",  get_drive_time, (lat, lon)),
        ("census", get_census,     (zip_code,)),
        ("comps",  get_comps,      (lat, lon, acreage, contact_comps)),
    ]

    threads = [threading.Thread(target=run, args=(k,f,a), daemon=True) for k,f,a in tasks]
    overpass_thread = threading.Thread(target=run_overpass, daemon=True)
    for t in threads: t.start()
    overpass_thread.start()
    for t in threads: t.join(timeout=25)
    overpass_thread.join(timeout=50)

    store["geocode"] = {"lat": lat, "lon": lon, "success": True, "source": source}
    store["assess"]  = calc_assess_score(store, contact)
    store["escalations"] = build_escalations(store, contact)
    # Offer range: prefer GHL CMA value, fall back to live comp avg_per_acre * acreage
    cma = contact.get("cma_value")
    if not cma:
        comp_data = store.get("comps", {})
        avg_per_acre = comp_data.get("avg_per_acre", 0)
        try:
            acres = float(acreage) if acreage else 0
        except (ValueError, TypeError):
            acres = 0
        if avg_per_acre and acres:
            cma = avg_per_acre * acres
    # Sanity guardrail: if county market value exists, cap comp-derived CMA
    # at 3× market value. Prevents tiny-lot $/ac from inflating large parcels.
    # Sources: GHL custom field → county GIS LNDVALUEBASE (always available)
    try:
        mv = float(str(contact.get("market_value") or "0").replace(",","").replace("$",""))
    except (ValueError, TypeError):
        mv = 0
    if not mv:
        mv = float(store.get("zoning", {}).get("county_land_value", 0) or 0)
    if mv > 0 and cma and float(cma) > mv * 3:
        print(f"[offer] CMA ${float(cma):,.0f} exceeds 3× market value ${mv:,.0f} — capping at ${mv*2:,.0f}")
        cma = mv * 2
    store["offer"] = calc_offer_range(cma)
    return store

# ─────────────────────────────────────────────
# CONTACT NORMALIZER
# ─────────────────────────────────────────────

def _ohio_true_value(raw_val):
    """Format county market value for display. GHL/REISkip already stores 100% appraised value."""
    try:
        v = float(str(raw_val or "0").replace(",","").replace("$","").strip())
        if v > 0:
            return f"${int(v):,}"
    except Exception:
        pass
    return str(raw_val or "")

def build_contact(raw):
    cf = raw.get("customField", {}) or {}
    return {
        "owner_name":       raw.get("full_name") or cf.get("owner_name") or raw.get("Owner Name",""),
        "owner_address":    raw.get("address1")  or cf.get("owner_address") or raw.get("Owner Mailing Address",""),
        "property_address": cf.get("property_address") or raw.get("Property Address",""),
        "zip_code":         raw.get("postalCode") or cf.get("zip_code") or raw.get("ZIP Code",""),
        "acreage":          cf.get("acreage")    or raw.get("Acreage",""),
        "market_value":     _ohio_true_value(cf.get("total_market_value") or raw.get("Total Market Value","")),
        "cma_value":        cf.get("cma_comp_value") or raw.get("CMA Comp Value",""),
        "cma_low":          cf.get("cma_low")  or raw.get("CMA Low",""),
        "cma_high":         cf.get("cma_high") or raw.get("CMA High",""),
        "lead_score":       cf.get("lead_score") or raw.get("Lead Score",""),
        "priority":         cf.get("priority")   or raw.get("Priority",""),
        "last_sale_date":   cf.get("last_sale_date") or raw.get("Last Sale Date",""),
        "last_sale_price":  cf.get("last_sale_price") or raw.get("Last Sale Price",""),
        "annual_tax":       raw.get("Annual Tax",""),
        "absentee_owner":   raw.get("Absentee Owner",""),
        "long_term":        raw.get("Long Term Ownership",""),
        "land_use":         raw.get("Land Use Desc",""),
        "parcel_id":        cf.get("parcel_id") or raw.get("Parcel ID",""),
        "agent":            (cf.get("agent") or raw.get("Agent")
                            or raw.get("assignedToName") or raw.get("assigned_to_name")
                            or raw.get("assignedTo") or ""),
        "comps": [
            {"address":raw.get("Comp 1 Address",""),"acres":raw.get("Comp 1 Acres",""),
             "date":raw.get("Comp 1 Sale Date",""),"price":raw.get("Comp 1 Sale Price",""),"per_acre":raw.get("Comp 1 $/Acre","")},
            {"address":raw.get("Comp 2 Address",""),"acres":raw.get("Comp 2 Acres",""),
             "date":raw.get("Comp 2 Sale Date",""),"price":raw.get("Comp 2 Sale Price",""),"per_acre":raw.get("Comp 2 $/Acre","")},
            {"address":raw.get("Comp 3 Address",""),"acres":raw.get("Comp 3 Acres",""),
             "date":raw.get("Comp 3 Sale Date",""),"price":raw.get("Comp 3 Sale Price",""),"per_acre":raw.get("Comp 3 $/Acre","")},
        ]
    }

# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────

@app.route("/")
def dashboard():
    from flask import redirect
    agent = request.args.get("agent", "").strip()
    if not agent:
        return redirect("/?agent=richard")
    # Normalize agent slug — strip trailing port numbers / garbage
    # so bookmarks like "richard5050" resolve to "richard"
    cfg = _load_cfg()
    known_slugs = {a["slug"] for a in cfg.get("agents", [])}
    if agent not in known_slugs:
        # Try matching by prefix — "richard5050" → "richard"
        for slug in known_slugs:
            if agent.startswith(slug):
                return redirect(f"/?agent={slug}")
    resp = send_from_directory(".", "dashboard.html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp

@app.route("/stream")
def stream():
    slug = _agent_slug(request.args.get("agent", "broadcast"))
    q = queue.Queue()
    with agent_lock:
        agent_subscribers.setdefault(slug, []).append(q)

    def gen():
        try:
            yield f'data: {{"type":"connected","agent":"{slug}"}}\n\n'
            # Replay buffered events from the last 30s (catches late-connecting clients)
            # Fall back to last-state replay for older data
            buffered = _drain_buffer(slug)
            if buffered:
                for evt in buffered:
                    yield f"data: {json.dumps(evt)}\n\n"
            else:
                state = _last_state.get(slug) or {}
                if state.get("contact"):
                    yield f"data: {json.dumps(state['contact'])}\n\n"
                if state.get("enrichment"):
                    yield f"data: {json.dumps(state['enrichment'])}\n\n"
            while True:
                try:
                    event = q.get(timeout=30)
                    yield f"data: {json.dumps(event)}\n\n"
                except queue.Empty:
                    yield ": heartbeat\n\n"
        except GeneratorExit:
            pass
        finally:
            with agent_lock:
                lst = agent_subscribers.get(slug, [])
                if q in lst:
                    lst.remove(q)
                if not lst and slug in agent_subscribers:
                    del agent_subscribers[slug]

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

@app.route("/agents")
def agents_status():
    with agent_lock:
        connected = {k: len(v) for k, v in agent_subscribers.items()}
    return jsonify({"connected": connected, "total": sum(connected.values())})

# ── Enrichment cache: parcel_id → {result, ts} ───────────────────────────────
_ecache      = {}
_ecache_lock = threading.Lock()
_ECACHE_TTL  = 86400  # 24 hours

def _cache_get(parcel_id):
    if not parcel_id: return None
    with _ecache_lock:
        e = _ecache.get(parcel_id)
        if e and (time.time() - e["ts"]) < _ECACHE_TTL:
            print(f"[cache] HIT {parcel_id}")
            return e["data"]
    return None

def _cache_set(parcel_id, data):
    if not parcel_id: return
    with _ecache_lock:
        _ecache[parcel_id] = {"data": data, "ts": time.time()}

def _enrich_and_push(contact):
    slug   = _agent_slug(contact["agent"]) if contact.get("agent") else "broadcast"
    errors = []

    def push(event):
        _push_to_agent(slug, event)
        # Save contact_ready and enrich_done for replay on reconnect
        if event.get("type") == "contact_ready":
            _last_state.setdefault(slug, {})["contact"] = event
        elif event.get("type") == "enrich_done":
            _last_state.setdefault(slug, {})["enrichment"] = event

    # ── 1. Push contact data immediately (reps see name/financials in ~1s) ──
    push({"type": "contact_ready", "data": contact})

    prop      = contact.get("property_address", "")
    parcel_id = contact.get("parcel_id", "")
    if not prop:
        errors.append("no property_address")
        _record_pop(contact, "none", {}, errors)
        push({"type": "enrich_done", "data": {}})
        return

    city     = f"Columbus OH {contact.get('zip_code','43215')}"
    zip_code = contact.get("zip_code", "43215")

    # ── 2. Check cache — skip all API calls if same parcel seen today ────────
    cached = _cache_get(parcel_id)
    if cached:
        _record_pop(contact, "cache", cached, [])
        push({"type": "enrich_done", "data": cached, "cached": True})
        return  # contact_ready was already pushed above in step 1

    # ── 3. Geocode ───────────────────────────────────────────────────────────
    lat, lon = parcel_centroid(parcel_id)
    geo_src  = "parcel_gis"
    if not (lat and lon):
        lat, lon = geocode(f"{prop}, {city}")
        geo_src  = "geocode"
    if not (lat and lon):
        errors.append(f"geocode_failed: parcel={parcel_id!r} addr={prop!r}")
        _record_pop(contact, "FAILED", {}, errors)
        push({"type": "enrich_done", "data": {"error": "Geocode failed"}})
        return

    store = {"geocode": {"lat": lat, "lon": lon, "success": True, "source": geo_src}}

    # ── 4. Run each API and stream results as they land ──────────────────────
    def run_and_push(key, fn, args):
        try:
            result = fn(*args)
        except Exception as ex:
            result = {"flag": "CHECK MANUALLY", "cls": "grey", "label": str(ex)[:80]}
        store[key] = result
        push({"type": "enrich_update", "key": key, "data": result})

    def run_overpass():
        """Overpass handles roads/power/streams/dams only.
        Wetlands, EPA, Superfund now use dedicated APIs (faster, more reliable)."""
        try:
            roads, power, wetlands_ovp, streams_ovp, epa_ovp, superfund_ovp, dams = get_all_overpass(lat, lon)
            for k, v in [("roads", roads), ("power", power), ("dams", dams)]:
                store[k] = v
                push({"type": "enrich_update", "key": k, "data": v})
            # Only use Overpass streams if direct NHD hasn't run yet
            if "streams" not in store:
                store["streams"] = streams_ovp
                push({"type": "enrich_update", "key": "streams", "data": streams_ovp})
        except Exception as ex:
            err = {"flag": "CHECK MANUALLY", "cls": "grey", "label": str(ex)[:80]}
            for k in ("roads", "power", "dams"):
                if k not in store:
                    store[k] = err
                    push({"type": "enrich_update", "key": k, "data": err})

    # ── Staged launch — most important data fires first ──────────────────────
    #
    # Wave 1 — Instant DQ checkers
    wave1 = [
        ("flood",     get_flood_zone, (lat, lon)),  # FEMA NFHL      ~1-2s
        ("soil",      get_soil,       (lat, lon)),  # USDA WSS       ~3-5s
        ("wetlands",  get_wetlands,   (lat, lon)),  # USFWS NWI      ~2-4s
    ]
    # Wave 2 — Property value + environmental
    wave2 = [
        ("slope",     get_slope,      (lat, lon)),  # USGS 3DEP      ~2-4s (parallel elev)
        ("zoning",    get_zoning,     (lat, lon)),  # Columbus GIS   ~2-4s
        ("drive",     get_drive_time, (lat, lon)),  # OSRM           ~2-3s
        ("epa_echo",  get_epa_echo,   (lat, lon)),  # EPA ECHO REST  ~3-5s
        ("superfund", get_superfund,  (lat, lon)),  # EPA ECHO CERCLA~3-5s
        ("streams",   get_streams,    (lat, lon)),  # USGS NHD       ~2-4s
    ]
    # Wave 3 — Context
    acreage      = contact.get("acreage", 0)
    contact_comps= contact.get("comps", [])
    wave3 = [
        ("census", get_census, (zip_code,)),        # Census ACS     ~3-5s
        ("comps",  get_comps,  (lat, lon, acreage, contact_comps)),  # Franklin County ArcGIS ~3-6s
    ]
    # Overpass — roads/power/dams only (less critical, can fail gracefully)
    ovp_thread = threading.Thread(target=run_overpass, daemon=True)

    # Subject-parcel sale/value lookup — fills financial boxes from county GIS
    # when GHL has no sale data. Runs in parallel; merged into enrich_done store.
    def run_parcel():
        rec = get_parcel_record(parcel_id, lat, lon)
        if rec:
            store["parcel"] = rec
            # Push immediately so financial boxes fill in ~1s, not gated
            # behind the slow Overpass join at enrich_done.
            push({"type": "enrich_update", "key": "parcel", "data": rec})
    parcel_thread = threading.Thread(target=run_parcel, daemon=True)

    def _launch_wave(wave):
        ts = [threading.Thread(target=run_and_push, args=t, daemon=True) for t in wave]
        for t in ts: t.start()
        return ts

    w1 = _launch_wave(wave1)
    # Brief pause — lets wave 1 get a head start through the network stack
    # before wave 2 floods the thread pool
    time.sleep(0.15)
    w2 = _launch_wave(wave2)
    time.sleep(0.15)
    w3 = _launch_wave(wave3)
    ovp_thread.start()
    parcel_thread.start()

    # Wait for all waves — hard caps, APIs should finish well inside these
    for t in w1: t.join(timeout=12)
    for t in w2: t.join(timeout=12)
    for t in w3: t.join(timeout=12)

    # ── Tax estimation from county assessed value ──────────────────────────
    if not contact.get("annual_tax"):
        zoning_data = store.get("zoning", {})
        if isinstance(zoning_data, dict):
            true_val = float(zoning_data.get("county_true_value", 0) or 0)
            if true_val > 0:
                assessed = true_val * 0.35
                est_tax = round(assessed * 0.032, 2)
                contact["annual_tax"] = f"~${int(est_tax):,}"
                push({"type": "enrich_update", "key": "est_annual_tax",
                      "data": {"annual_tax": contact["annual_tax"]}})
                print(f"[tax_est] assessed=${assessed:,.0f} -> est annual tax=${est_tax:,.0f}")

    # ── 5a. Push preliminary score immediately after waves 1-3 ──────────────
    # Rep sees score ring + offer range without waiting for Overpass (OSM data)
    assess_pre     = calc_assess_score(store, contact)
    escalations_pre= build_escalations(store, contact)
    # Offer range: GHL CMA first, fall back to live comps
    cma = contact.get("cma_value")
    if not cma:
        comp_data    = store.get("comps", {})
        avg_per_acre = comp_data.get("avg_per_acre", 0)
        avg_price    = comp_data.get("avg_price", 0)
        try: ac = float(acreage) if acreage else 0
        except: ac = 0
        if ac > 0 and ac < 0.2 and avg_price:
            # Tiny lots: $/acre × acreage is meaningless — a 0.05ac lot ≠ half a 0.10ac lot.
            # Use average comp PRICE directly (all comps are similarly small urban lots).
            cma = avg_price
            print(f"[offer] tiny lot ({ac}ac) — using avg comp price ${avg_price:,} instead of $/acre")
        elif avg_per_acre and ac:
            cma = avg_per_acre * ac
        elif avg_price and not ac:
            cma = avg_price
            print(f"[offer] no acreage — using avg comp price ${avg_price:,}")
    # Guardrail: cap CMA at 3× true market value — prevents tiny-lot $/ac inflation
    if cma:
        mv = 0.0
        try:
            mv = float(str(contact.get("market_value") or "0").replace(",","").replace("$",""))
        except Exception:
            pass
        if not mv:
            mv = float(store.get("zoning", {}).get("county_true_value", 0) or 0)
        if mv > 0 and float(cma) > mv * 3:
            print(f"[offer] CMA ${float(cma):,.0f} > 3× market value ${mv:,.0f} — capping at ${mv*2:,.0f}")
            cma = mv * 2
    offer          = calc_offer_range(cma)
    store["assess"]      = assess_pre
    store["escalations"] = escalations_pre
    store["offer"]       = offer
    push({"type": "enrich_update", "key": "assess",      "data": assess_pre})
    push({"type": "enrich_update", "key": "escalations", "data": escalations_pre})
    push({"type": "enrich_update", "key": "offer",       "data": offer})

    # ── 5b. Wait for Overpass then push final score update ───────────────────
    # Overpass now runs all 4 servers in parallel — worst case ~10s
    ovp_thread.join(timeout=12)
    parcel_thread.join(timeout=10)

    assess     = calc_assess_score(store, contact)
    escalations= build_escalations(store, contact)
    store["assess"]      = assess
    store["escalations"] = escalations

    # Collect any API errors for history
    for k, v in store.items():
        if isinstance(v, dict) and v.get("cls") == "grey" and v.get("flag") == "CHECK MANUALLY":
            errors.append(f"{k}: {v.get('label','error')[:60]}")

    # Include full store so _last_state replay can repopulate all cards
    push({"type": "enrich_done", "data": {**store}})

    # Stream extra comps after the pop is fully rendered
    extra_comps = (store.get("comps") or {}).get("extra", [])
    if extra_comps:
        push({"type": "enrich_update", "key": "comps_extra", "data": extra_comps})

    # Record in pop history
    _record_pop(contact, geo_src, store, errors)

    # Cache the full enrichment for 24h
    _cache_set(parcel_id, {**store})

@app.route("/webhook/ghl", methods=["POST"])
def ghl_webhook():
    try: raw = request.get_json(force=True) or {}
    except: raw = {}
    contact = build_contact(raw.get("contact", raw))
    threading.Thread(target=_enrich_and_push, args=(contact,), daemon=True).start()
    return jsonify({"status":"ok"}), 200

@app.route("/webhook/test", methods=["POST","GET"])
def test_webhook():
    agent_override = request.args.get("agent", "richard")
    sample = {
        "Owner Name":"FITZ KENNETH A",
        "Owner Mailing Address":"3745 FLORIAN DR COLUMBUS OH 43219",
        "Property Address":"MONTCLAIR DR",
        "ZIP Code":"43219",
        "Acreage":"0.577",
        "Total Market Value":"62400",
        "CMA Comp Value":"128039",
        "CMA Low":"27923", "CMA High":"252185",
        "Lead Score":"97",
        "Priority":"TIER 1 - URGENT",
        "Last Sale Date":"2019-06-14",
        "Last Sale Price":"45000",
        "Annual Tax":"1000.88",
        "Absentee Owner":"Yes",
        "Long Term Ownership":"True",
        "Land Use Desc":"RESIDENTIAL, VACANT LAND, LOT",
        "Parcel ID":"010-146645",
        "Agent": agent_override,
        "Comp 1 Address":"LEONARD AVE",    "Comp 1 Acres":"0.5918",
        "Comp 1 Sale Date":"2024-01-19",   "Comp 1 Sale Price":"26900",  "Comp 1 $/Acre":"48393",
        "Comp 2 Address":"2006 E FIFTH AVE","Comp 2 Acres":"0.4471",
        "Comp 2 Sale Date":"2024-09-23",   "Comp 2 Sale Price":"90000",  "Comp 2 $/Acre":"210056",
        "Comp 3 Address":"E FIFTH AVE",    "Comp 3 Acres":"0.7077",
        "Comp 3 Sale Date":"2022-10-19",   "Comp 3 Sale Price":"280000", "Comp 3 $/Acre":"437063",
    }
    contact = build_contact(sample)
    threading.Thread(target=_enrich_and_push, args=(contact,), daemon=True).start()
    return jsonify({"status":"ok","contact":contact["owner_name"],
                    "note":"13 APIs running in parallel — results push to dashboard via SSE"}), 200

@app.route("/pop", methods=["GET", "OPTIONS"])
def pop_contact():
    """One-click bookmarklet endpoint — fetches GHL contact and fires screen pop."""
    cors = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "*",
        "Cache-Control": "no-store",
    }
    if request.method == "OPTIONS":
        return Response("", status=204, headers=cors)
    contact_id    = request.args.get("contact_id", "").strip()
    agent_override= request.args.get("agent", "").strip()

    if not contact_id:
        return Response("missing contact_id", status=400, headers=cors)

    raw, err = fetch_ghl_contact(contact_id)
    if raw is None:
        return Response(err or "error", status=503, headers=cors)

    if agent_override:
        raw["Agent"] = agent_override

    contact = build_contact(raw)
    threading.Thread(target=_enrich_and_push, args=(contact,), daemon=True).start()
    # Return self-closing HTML so the tiny popup window disappears immediately
    return Response("<script>window.close()</script>", status=200,
                    mimetype="text/html", headers=cors)

@app.route("/setup", methods=["GET","POST"])
def setup():
    """Setup page — config, bookmarklets for all agents."""
    if request.method == "POST":
        d = request.get_json(force=True) or {}
        update = {}
        if d.get("ghl_api_key"): update["ghl_api_key"] = d["ghl_api_key"]
        if d.get("server_url"):  update["server_url"]  = d["server_url"]
        if update: _save_cfg(update)
        with _cf_lock: _cf_cache.clear()
        return jsonify({"status": "saved"})

    cfg     = _load_cfg()
    srv_url = cfg.get("server_url", "http://localhost:5050").rstrip("/")
    key_set = bool(cfg.get("ghl_api_key") or os.environ.get("GHL_API_KEY"))
    agents  = cfg.get("agents", [
        {"name":"Richard","slug":"richard","role":"admin"},
        {"name":"OJ",     "slug":"oj",     "role":"employee"},
        {"name":"Kehlen", "slug":"kehlen", "role":"employee"},
        {"name":"Lucia",  "slug":"lucia",  "role":"employee"},
    ])

    def bm(ag_name, ag_slug):
        return (
            f"javascript:(function(){{"
            f"var m=location.href.match(/contacts\\/(?:detail\\/)?([A-Za-z0-9]{{15,}})/);"
            f"if(!m){{alert('Open a GHL contact first');return;}}"
            f"window.open('{srv_url}/pop?contact_id='+m[1]+'&agent={urllib.parse.quote(ag_slug)}&_='+Date.now(),'_ovlp','width=1,height=1');"
            f"}})();"
        )

    rows = ""
    for ag in agents:
        name = ag["name"]; slug = ag["slug"]; role = ag.get("role","employee")
        badge = " 👑" if role == "admin" else ""
        js    = bm(name, slug)
        dash  = f"{srv_url}/?agent={slug}"
        rows += (
            f'<div class="agent-row">'
            f'<div class="ag-name">{name}{badge} <span class="role">{role}</span></div>'
            f'<div class="bm"><a href="{js}" style="color:#a78bfa;text-decoration:none" '
            f'title="Drag to Chrome bookmarks bar">⚡ OVLP POP — {name}</a></div>'
            f'<div class="dash-link">Dashboard: <a href="{dash}" target="_blank" style="color:#4a9eff">{dash}</a></div>'
            f'</div>'
        )

    status_color = "ok" if key_set else "warn"
    status_text  = "API KEY SET ✓" if key_set else "API KEY NOT SET ✗"
    srv_display  = srv_url if "ngrok" in srv_url or "localhost" not in srv_url else ""

    html = f"""<!DOCTYPE html><html><head><title>OVLP Setup</title>
<style>
*{{box-sizing:border-box}}
body{{font-family:monospace;background:#0d0d0d;color:#eee;padding:32px;max-width:860px;margin:0 auto}}
h2{{color:#4a9eff;margin-bottom:4px}}
.subtitle{{color:#555;font-size:12px;margin-bottom:28px}}
label{{color:#aaa;font-size:11px;display:block;margin-bottom:4px}}
input{{width:100%;padding:9px;background:#1a1a1a;border:1px solid #333;color:#eee;border-radius:4px;margin-bottom:16px;font-family:monospace}}
.save-btn{{padding:9px 24px;background:#4a9eff;color:#000;border:none;border-radius:4px;cursor:pointer;font-weight:700;font-size:13px}}
.ok{{color:#2ecc71;font-weight:700}}.warn{{color:#e74c3c;font-weight:700}}
hr{{border:none;border-top:1px solid #222;margin:28px 0}}
h3{{color:#eee;margin-bottom:16px}}
.agent-row{{background:#111;border:1px solid #222;border-radius:8px;padding:16px;margin-bottom:12px}}
.ag-name{{font-size:15px;font-weight:700;margin-bottom:8px}}
.role{{font-size:10px;color:#555;font-weight:400;text-transform:uppercase;letter-spacing:1px;margin-left:6px}}
.bm{{background:#0a0a0a;border:1px solid #2a2a2a;border-radius:5px;padding:10px 14px;margin-bottom:8px;font-size:12px}}
.dash-link{{font-size:11px;color:#555}}
.instructions{{background:#0f1a2e;border:1px solid #1e3a5f;border-radius:6px;padding:14px;margin-top:20px;font-size:12px;color:#aac4e8;line-height:1.7}}
</style></head><body>
<h2>OVLP Screen Pop — Team Setup</h2>
<div class="subtitle">Ohio Valley Land Partners · Richard B. Jamison</div>

<p>GHL API: <span class="{status_color}">{status_text}</span></p>

<label>GHL API Key</label>
<input id="k" type="password" placeholder="pit-xxxx..." value="{'*****' if key_set else ''}">
<label>Public Server URL (ngrok or VPS — needed for employee bookmarklets)</label>
<input id="u" placeholder="https://xxxx.ngrok-free.app" value="{srv_display}">
<button class="save-btn" onclick="save()">Save Config</button>

<hr>
<h3>Team Bookmarklets</h3>
<div class="instructions">
  <b>How to install:</b> Drag the purple ⚡ link to Chrome's bookmarks bar.<br>
  <b>How to use:</b> Open any GHL contact → click your bookmark → screen pops in ~5 sec.<br>
  <b>Each person gets their own</b> — pops only appear on their dashboard tab.
</div>
<br>
{rows}

<br>
<a href="/recent" style="display:inline-block;padding:9px 20px;background:rgba(124,123,255,.12);border:1px solid rgba(124,123,255,.35);border-radius:8px;color:#a0a8ff;font-weight:700;font-size:12px;text-decoration:none;letter-spacing:.06em">
  ▶ VIEW RECENT POPS →
</a>
<span style="color:#555;font-size:11px;margin-left:12px">Last 5 pops — geocode source, comps, API results, errors</span>
<br><br>
<a href="/lookup" style="display:inline-block;padding:9px 20px;background:rgba(46,204,113,.12);border:1px solid rgba(46,204,113,.35);border-radius:8px;color:#2ecc71;font-weight:700;font-size:12px;text-decoration:none;letter-spacing:.06em">
  ▶ PHONE LOOKUP →
</a>
<span style="color:#555;font-size:11px;margin-left:12px">Reverse search — find which property a phone number belongs to</span>

<script>
function save(){{
  var k=document.getElementById('k').value;
  var u=document.getElementById('u').value;
  var body={{}};
  if(k&&k!=='*****') body.ghl_api_key=k;
  if(u) body.server_url=u;
  fetch('/setup',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body)}})
    .then(r=>r.json()).then(()=>{{alert('Saved!');location.reload();}});
}}
</script></body></html>"""
    return html

@app.route("/lookup", methods=["GET"])
def lookup_page():
    """Reverse phone lookup — paste a number, find which contact/property it belongs to."""
    phone_q  = request.args.get("phone", "").strip()
    agent    = request.args.get("agent", "richard").strip()
    cfg      = _load_cfg()
    srv_url  = cfg.get("server_url", "http://localhost:5050").rstrip("/")

    result_html = ""
    if phone_q:
        match = phone_lookup(phone_q)
        if match:
            cid   = match["contact_id"]
            cname = match["contact_name"] or "(no name)"
            ptype = match["phone_type"]
            pop_url = f"{srv_url}/pop?contact_id={cid}&agent={urllib.parse.quote(agent)}"
            result_html = f'''
            <div class="result match">
                <div class="match-label">MATCH FOUND</div>
                <div class="match-name">{cname}</div>
                <div class="match-detail">Phone type: <b>{ptype}</b></div>
                <div class="match-detail">Contact ID: <code>{cid}</code></div>
                <div class="match-actions">
                    <a href="{pop_url}" target="_blank" class="pop-btn">POP THIS CONTACT</a>
                </div>
            </div>'''
        else:
            # Fallback: try GHL search API directly
            fb_html = ""
            try:
                body = json.dumps({
                    "locationId": cfg.get("ghl_location_id", ""),
                    "page": 1, "pageLimit": 5,
                    "query": _normalize_phone(phone_q)
                }).encode()
                req_url = "https://services.leadconnectorhq.com/contacts/search"
                r = urllib.request.Request(req_url, data=body, method="POST", headers={
                    **_ghl_headers(),
                    "Content-Type": "application/json",
                    "User-Agent": "OVLP-ScreenPop/2.0"
                })
                resp = urllib.request.urlopen(r, timeout=10, context=_SSL)
                sdata = json.loads(resp.read())
                hits = sdata.get("contacts", [])
                if hits:
                    fb_html = '<div class="fb-label">GHL search found:</div>'
                    for h in hits[:5]:
                        hname = h.get("contactName") or f"{h.get('firstName','')} {h.get('lastName','')}".strip()
                        hid   = h.get("id","")
                        hph   = h.get("phone","")
                        pop_url = f"{srv_url}/pop?contact_id={hid}&agent={urllib.parse.quote(agent)}"
                        fb_html += f'''
                        <div class="fb-row">
                            <span class="fb-name">{hname or "(no name)"}</span>
                            <span class="fb-phone">{hph}</span>
                            <a href="{pop_url}" target="_blank" class="pop-btn-sm">POP</a>
                        </div>'''
            except Exception as e:
                fb_html = f'<div class="fb-err">GHL search failed: {e}</div>'

            result_html = f'''
            <div class="result no-match">
                <div class="match-label" style="color:#ff4757">NO MATCH IN INDEX</div>
                <div class="match-detail">Searched: {_normalize_phone(phone_q)}</div>
                <div class="match-detail" style="color:#888">Index has {len(_phone_index)} numbers from GHL</div>
                {fb_html}
            </div>'''

    html = f'''<!DOCTYPE html><html><head><title>OVLP Phone Lookup</title>
<style>
*{{box-sizing:border-box}}
body{{font-family:monospace;background:#0d0d0d;color:#eee;padding:32px;max-width:700px;margin:0 auto}}
h2{{color:#4a9eff;margin-bottom:4px}}
.subtitle{{color:#555;font-size:12px;margin-bottom:24px}}
.search-box{{display:flex;gap:8px;margin-bottom:24px}}
input[type=text]{{flex:1;padding:12px;background:#1a1a1a;border:1px solid #333;color:#eee;border-radius:6px;font-family:monospace;font-size:16px}}
input[type=text]:focus{{border-color:#4a9eff;outline:none}}
.search-btn{{padding:12px 24px;background:#4a9eff;color:#000;border:none;border-radius:6px;cursor:pointer;font-weight:700;font-size:14px}}
.search-btn:hover{{background:#6ab4ff}}
.result{{background:#111;border:1px solid #222;border-radius:8px;padding:20px;margin-bottom:16px}}
.match{{border-color:#2ecc71}}
.no-match{{border-color:#ff4757}}
.match-label{{font-size:11px;font-weight:700;color:#2ecc71;letter-spacing:.1em;text-transform:uppercase;margin-bottom:8px}}
.match-name{{font-size:20px;font-weight:700;margin-bottom:8px}}
.match-detail{{color:#888;font-size:12px;margin-bottom:4px}}
.match-detail b{{color:#ccc}}
.match-actions{{margin-top:14px}}
.pop-btn{{display:inline-block;padding:10px 28px;background:#2ecc71;color:#000;font-weight:700;border-radius:6px;text-decoration:none;font-size:13px;letter-spacing:.05em}}
.pop-btn:hover{{background:#27ae60}}
.pop-btn-sm{{display:inline-block;padding:5px 14px;background:#4a9eff;color:#000;font-weight:700;border-radius:4px;text-decoration:none;font-size:11px;margin-left:8px}}
.fb-label{{color:#ffb300;font-size:11px;font-weight:700;margin:12px 0 8px;text-transform:uppercase;letter-spacing:.08em}}
.fb-row{{background:#0a0a0a;border:1px solid #1e1e1e;border-radius:4px;padding:8px 12px;margin-bottom:4px;display:flex;align-items:center;gap:12px;font-size:12px}}
.fb-name{{color:#ccc;font-weight:700;min-width:120px}}
.fb-phone{{color:#888;flex:1}}
.fb-err{{color:#ff4757;font-size:11px;margin-top:8px}}
.stats{{color:#555;font-size:11px;margin-top:16px}}
.stats b{{color:#888}}
.nav{{margin-top:24px;font-size:11px}}
.nav a{{color:#4a9eff;text-decoration:none;margin-right:16px}}
code{{background:#1a1a1a;padding:2px 6px;border-radius:3px;font-size:11px;color:#a78bfa}}
.refresh-btn{{padding:6px 16px;background:#222;color:#888;border:1px solid #333;border-radius:4px;cursor:pointer;font-size:11px;margin-left:8px}}
.refresh-btn:hover{{color:#eee;border-color:#4a9eff}}
</style></head><body>
<h2>OVLP Phone Lookup</h2>
<div class="subtitle">Reverse search — find which property contact a phone number belongs to</div>
<form method="GET" action="/lookup">
<input type="hidden" name="agent" value="{agent}">
<div class="search-box">
    <input type="text" name="phone" placeholder="Enter phone number..." value="{phone_q}" autofocus>
    <button type="submit" class="search-btn">SEARCH</button>
</div>
</form>
{result_html}
<div class="stats">
    Index: <b>{len(_phone_index)}</b> phone numbers mapped &nbsp;|&nbsp;
    Last built: <b>{time.strftime("%H:%M:%S", time.localtime(_phone_index_ts)) if _phone_index_ts else "never"}</b>
    <button class="refresh-btn" onclick="fetch('/lookup/rebuild').then(r=>r.json()).then(d=>{{alert('Rebuilt: '+d.count+' numbers');location.reload();}})">Rebuild Index</button>
</div>
<div class="nav">
    <a href="/setup">Setup</a>
    <a href="/recent">Recent Pops</a>
    <a href="/?agent={agent}">Dashboard</a>
</div>
</body></html>'''
    return html

@app.route("/lookup/rebuild")
def lookup_rebuild():
    """Force rebuild the phone index."""
    _build_phone_index()
    return jsonify({"status": "rebuilt", "count": len(_phone_index), "ts": _phone_index_ts})

@app.route("/lookup/api")
def lookup_api():
    """JSON API for phone lookup — for programmatic use."""
    phone = request.args.get("phone", "").strip()
    if not phone:
        return jsonify({"error": "missing phone parameter"}), 400
    match = phone_lookup(phone)
    if match:
        return jsonify({"match": True, **match})
    return jsonify({"match": False, "searched": _normalize_phone(phone)})

def _dash_build():
    try: return int(os.path.getmtime(os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")))
    except Exception: return 0

@app.route("/ping")
def ping(): return jsonify({"status":"running","apis":13,"time":time.time(),"build":_dash_build()})

@app.route("/debug/last-contact")
def debug_last_contact():
    slug = request.args.get("agent", "broadcast")
    state = _last_state.get(slug, {})
    contact_evt = state.get("contact", {})
    data = contact_evt.get("data", {}) if isinstance(contact_evt, dict) else {}
    keys = ["owner_name","last_sale_date","last_sale_price","annual_tax","market_value","parcel_id"]
    return jsonify({"fields": {k: data.get(k, "NOT PRESENT") for k in keys}, "all_data": data})

@app.route("/debug/fetch-contact")
def debug_fetch_contact():
    """Fetch a GHL contact and return the raw + build_contact output for debugging."""
    cid = request.args.get("contact_id", "").strip()
    if not cid: return jsonify({"error": "missing contact_id"}), 400
    raw, err = fetch_ghl_contact(cid)
    if raw is None: return jsonify({"error": err}), 503
    contact = build_contact(raw)
    return jsonify({
        "raw_customField": raw.get("customField", {}),
        "raw_bridged": {k: raw.get(k,"") for k in ["Last Sale Date","Last Sale Price","Annual Tax"]},
        "build_contact_output": {k: contact.get(k,"") for k in ["last_sale_date","last_sale_price","annual_tax","market_value","parcel_id","owner_name"]},
    })

@app.route("/recent")
def recent():
    """Last 5 pops — diagnostic endpoint. Hit this after SSHing in to see what happened."""
    with _pop_history_lock:
        history = list(_pop_history)
    # HTML view for browser, JSON for curl/scripts
    if "text/html" in request.headers.get("Accept",""):
        rows = ""
        api_keys = ["flood","soil","wetlands","slope","zoning","drive","epa_echo","superfund","streams","roads","power","dams","census","comps"]
        cls_color = {"green":"#23d160","yellow":"#ffb300","red":"#ff4757","grey":"#555"}
        for e in history:
            api_dots = ""
            for k in api_keys:
                c = e["apis"].get(k,"grey") if k != "comps" else ("green" if e["comps"]>0 else "red")
                col = cls_color.get(c,"#555")
                api_dots += f'<span title="{k}" style="display:inline-block;width:10px;height:10px;border-radius:50%;background:{col};margin:1px"></span>'
            err_html = ""
            if e.get("errors"):
                err_html = f'<div style="color:#ff4757;font-size:10px;margin-top:3px">{"; ".join(e["errors"])}</div>'
            geo_color = "#23d160" if e["geocode"]=="parcel_gis" else ("#ffb300" if e["geocode"]=="geocode" else "#ff4757")
            rows += f"""
            <tr>
              <td style="color:#888;white-space:nowrap">{e["ts"]}</td>
              <td style="color:#b59eff">{e["agent"]}</td>
              <td><b>{e["contact"]}</b><div style="color:#888;font-size:10px">{e["address"]}</div><div style="color:#666;font-size:9px">{e["parcel_id"]} · {e["acreage"]} ac</div></td>
              <td><span style="color:{geo_color};font-size:10px;font-weight:700">{e["geocode"]}</span></td>
              <td style="color:{'#23d160' if e['comps']>0 else '#ff4757'};font-weight:700">{e["comps"]} comps{f"<div style='color:#888;font-size:10px'>${e['comp_avg']:,}/ac</div>" if e["comp_avg"] else ""}</td>
              <td style="font-size:11px;font-weight:700">{e["score"]}</td>
              <td>{api_dots}{err_html}</td>
            </tr>"""
        body_content = '<p style="color:#555">No pops recorded yet.</p>' if not history else (
            "<table>"
            "<thead><tr><th>Time</th><th>Agent</th><th>Contact</th><th>Geocode</th><th>Comps</th><th>Score</th>"
            "<th>APIs (flood soil wet slope zone drive epa sup str rds pwr dam cen cmp)</th></tr></thead>"
            "<tbody>" + rows + "</tbody></table>"
        )
        html = (
            '<!DOCTYPE html><html><head><meta charset="UTF-8">'
            '<title>OVLP \u00b7 Recent Pops</title><style>'
            'body{background:#0e0f1a;color:#c8cde8;font-family:-apple-system,sans-serif;padding:24px;font-size:12px}'
            'h2{color:#7c7bff;font-size:16px;margin-bottom:16px;letter-spacing:.1em;text-transform:uppercase}'
            'table{width:100%;border-collapse:collapse}'
            'th{text-align:left;color:#555;font-size:9px;text-transform:uppercase;letter-spacing:.1em;padding:6px 10px;border-bottom:1px solid #1e2032}'
            'td{padding:8px 10px;border-bottom:1px solid #151621;vertical-align:top}'
            'tr:hover td{background:#131422}'
            'a{color:#7c7bff;text-decoration:none} a:hover{text-decoration:underline}'
            '</style></head><body>'
            '<h2>OVLP \u00b7 Last ' + str(len(history)) + ' Pops</h2>'
            + body_content +
            '<p style="margin-top:20px"><a href="/setup">\u2190 Setup</a> &nbsp; <a href="/recent">\u21bb Refresh</a></p>'
            '</body></html>'
        )
        return html, 200, {"Content-Type": "text/html; charset=utf-8"}
    return jsonify(history)

# ─────────────────────────────────────────────
# AUTO-UPDATE ROUTES  (Richard's machine serves these to staff)
# ─────────────────────────────────────────────

@app.route("/update/version")
def update_version():
    return jsonify({"version": VERSION, "comp_prompt_version": COMP_PROMPT_VERSION})

@app.route("/update/install")
def update_install():
    """Returns a shell one-liner staff can pipe to bash — bypasses macOS Gatekeeper entirely.
    Works from LAN or remote (tries LAN first, GitHub fallback).
    Usage (paste in Terminal): curl -s http://192.168.0.103:5050/update/install | bash
    """
    script = """#!/bin/bash
set -e
INSTALL_DIR="$HOME/ovlp-pop"
mkdir -p "$INSTALL_DIR"
LAN="http://192.168.0.103:5050/update"
GH="https://raw.githubusercontent.com/RichardBJamiosn/OVLP-Screen-Pop/main"

# Try LAN first, fall back to GitHub
if curl -s --connect-timeout 3 "$LAN/version" >/dev/null 2>&1; then
  BASE="$LAN"
  VER=$(curl -s $BASE/version | python3 -c 'import sys,json; print(json.load(sys.stdin)["version"])')
  echo "Downloading v${VER} from LAN..."
else
  BASE="$GH"
  echo "LAN unreachable — downloading from GitHub..."
fi

curl -s "$BASE/server.py"      -o "$INSTALL_DIR/server.py"
curl -s "$BASE/dashboard.html" -o "$INSTALL_DIR/dashboard.html"
curl -s "$BASE/comp_prompt.md" -o "$INSTALL_DIR/comp_prompt.md"
lsof -ti:5050 | xargs kill -9 2>/dev/null; sleep 1
cd "$INSTALL_DIR" && nohup python3 server.py >> server.log 2>&1 &
sleep 3
curl -s http://localhost:5050/update/version | python3 -c 'import sys,json; print("Server running v" + json.load(sys.stdin)["version"])' 2>/dev/null || echo 'Server started'
"""
    return script, 200, {"Content-Type": "text/plain"}

@app.route("/update/server.py")
def update_server_py():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "server.py",
                               mimetype="text/plain")

@app.route("/update/dashboard.html")
def update_dashboard_html():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "dashboard.html",
                               mimetype="text/html")

@app.route("/update/comp_prompt.md")
def update_comp_prompt():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "comp_prompt.md",
                               mimetype="text/plain")

@app.route("/comp-report")
def comp_report_page():
    this_dir = os.path.dirname(os.path.abspath(__file__))
    return send_from_directory(this_dir, "comp_report.html")

@app.route("/comp-prompt")
def comp_prompt_page():
    this_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        with open(os.path.join(this_dir, "comp_prompt.md")) as f:
            raw = f.read()
    except FileNotFoundError:
        raw = "comp_prompt.md not found — run Update.command to get the latest files."
    # Escape for HTML, preserve code blocks and line breaks
    import html as html_mod
    escaped = html_mod.escape(raw)
    # Style code fences as <pre> blocks
    import re as re_mod
    escaped = re_mod.sub(r'```(.*?)```', r'<pre>\1</pre>', escaped, flags=re_mod.DOTALL)
    escaped = escaped.replace('\n', '<br>')
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Land Comp Prompt v{COMP_PROMPT_VERSION} — OVLP</title>
<style>
  body{{background:#0f0f1a;color:#e0e0f0;font-family:'Segoe UI',system-ui,sans-serif;max-width:860px;margin:40px auto;padding:0 24px;line-height:1.7}}
  h1{{color:#a78bfa;font-size:1.4em;border-bottom:1px solid #2a2a40;padding-bottom:12px}}
  .version{{background:#1a1a2e;border:1px solid #a78bfa44;border-radius:8px;padding:8px 16px;display:inline-block;color:#a78bfa;font-weight:700;margin-bottom:24px}}
  pre{{background:#111122;border:1px solid #2a2a40;border-radius:10px;padding:20px;white-space:pre-wrap;word-break:break-word;font-size:.88em;color:#c8d0e0;overflow-x:auto;line-height:1.6}}
  .back{{color:#4a9eff;text-decoration:none;font-size:.9em}}
  .back:hover{{text-decoration:underline}}
</style>
</head>
<body>
<p><a class="back" href="javascript:history.back()">← Dashboard</a></p>
<h1>Land Comp Report Prompt</h1>
<div class="version">Version {COMP_PROMPT_VERSION}</div>
<div>{escaped}</div>
</body>
</html>"""
    return page, 200, {"Content-Type": "text/html"}

if __name__ == "__main__":
    # Skip auto-update on the canonical server (192.168.0.103) — this IS the source.
    # Employee machines auto-update from here or GitHub on every boot.
    import socket as _sock
    try:
        s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = ""
    if local_ip != "192.168.0.103":
        _auto_update()
    else:
        print("[update] canonical server — skipping self-update")
    _pop_history.extend(_load_pop_history())
    # Build phone index in background so startup isn't blocked
    threading.Thread(target=_build_phone_index, daemon=True).start()
    print("\n  OVLP Screen Pop — Ohio Valley Land Partners")
    print("  ──────────────────────────────────────────────────────")
    print("  Richard:  http://localhost:5050/?agent=richard")
    print("  OJ:       http://localhost:5050/?agent=oj")
    print("  Kehlen:   http://localhost:5050/?agent=kehlen")
    print("  Lucia:    http://localhost:5050/?agent=lucia")
    print("  ──────────────────────────────────────────────────────")
    print("  Lookup:   http://localhost:5050/lookup")
    print("  Setup:    http://localhost:5050/setup")
    print("  Agents:   http://localhost:5050/agents")
    print("  Test pop: curl 'http://localhost:5050/webhook/test?agent=richard'\n")
    cert = os.path.join(os.path.dirname(os.path.abspath(__file__)), "localhost+1.pem")
    key  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "localhost+1-key.pem")
    ssl_ctx = (cert, key) if os.path.exists(cert) else None
    if ssl_ctx:
        print("  HTTPS: using mkcert cert (trusted by Chrome) ✓\n")
    else:
        print("  WARNING: no cert found, running HTTP\n")
    app.run(host="0.0.0.0", port=5050, debug=False, threaded=True, ssl_context=ssl_ctx)

