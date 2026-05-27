"""
NSW Journey Planner — API Proxy
Endpoints:
  /stops?q=NAME          → Stop Finder API   (in-memory cache, 5 min TTL)
  /trip?from=ID&to=ID    → Trip Planner API
  /depart?stop=ID        → Departure Monitor API
  /alerts                → Service Alert API
  /                      → Health check
"""
import datetime
import os
import sys
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer

# ── Config ────────────────────────────────────────────────────────────────────
PORT    = int(os.environ.get("PORT", 3001))
API_KEY = os.environ.get("NSW_API_KEY", "")
BASE    = "https://api.transport.nsw.gov.au"
TIMEOUT = 12  # seconds per upstream request

# ── Thread-pool server ────────────────────────────────────────────────────────
# Render free tier: 0.1 CPU, 512 MB RAM.
# ThreadingMixIn spawns unlimited threads — bad for 0.1 CPU.
# A pool of 4 workers is right-sized: the bottleneck is TfNSW I/O wait (not CPU),
# so 4 concurrent in-flight requests is plenty without overloading the scheduler.
class PooledHTTPServer(HTTPServer):
    def __init__(self, *args, max_workers: int = 4, **kwargs):
        super().__init__(*args, **kwargs)
        self._pool = ThreadPoolExecutor(max_workers=max_workers)

    def process_request(self, request, client_address):
        # Hand off to pool instead of spawning a fresh thread every time
        self._pool.submit(self._process, request, client_address)

    def _process(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)

    def server_close(self):
        self._pool.shutdown(wait=False)
        super().server_close()

# ── In-memory stop-search cache ───────────────────────────────────────────────
# Keyed by lower-cased query; entries expire after STOP_TTL seconds.
# Capped at MAX_CACHE_ENTRIES to bound memory use on the 512 MB instance:
#   150 entries × ~40 KB average payload ≈ 6 MB max — negligible.
# Thread-safe via a lock (multiple pool workers may write concurrently).
_stop_cache: dict = {}
_stop_lock        = threading.Lock()
STOP_TTL          = 300  # seconds (5 min)
MAX_CACHE_ENTRIES = 150

def _cache_get(q: str):
    with _stop_lock:
        entry = _stop_cache.get(q)
        if entry and time.monotonic() - entry["ts"] < STOP_TTL:
            return entry["code"], entry["body"]
    return None, None

def _cache_set(q: str, code: int, body: bytes):
    if code != 200:
        return
    with _stop_lock:
        if len(_stop_cache) >= MAX_CACHE_ENTRIES:
            # Evict the single oldest entry (simple FIFO — avoids full LRU overhead)
            oldest = min(_stop_cache, key=lambda k: _stop_cache[k]["ts"])
            del _stop_cache[oldest]
        _stop_cache[q] = {"code": code, "body": body, "ts": time.monotonic()}

# ── Upstream call ─────────────────────────────────────────────────────────────
def call(path: str, params: dict):
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    req.add_header("Authorization", API_KEY)
    req.add_header("Accept", "application/json")
    print(f"→ {url[:120]}")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read()
            print(f"← {r.status} OK ({len(body)} bytes)")
            return r.status, body
    except urllib.error.HTTPError as e:
        body = e.read()
        print(f"← {e.code} ERR: {body[:120].decode('utf-8', 'ignore')}")
        return e.code, body
    except Exception as e:
        print(f"← ERR: {e}")
        return 502, b'{"error":"upstream timeout or connection error"}'

# ── Request handler ───────────────────────────────────────────────────────────
class H(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs     = dict(urllib.parse.parse_qsl(parsed.query))
        path   = parsed.path

        # ── Stop Finder ──────────────────────────────────────────────────────
        if path == "/stops":
            q = qs.get("q", "").strip()
            if not q:
                self.respond(400, b'{"error":"missing q"}'); return

            # Serve from cache if available (makes typing feel instant)
            code, body = _cache_get(q.lower())
            if body is not None:
                print(f"← 200 CACHE ({len(body)} bytes)")
                self.respond(code, body); return

            code, body = call("/v1/tp/stop_finder", {
                "outputFormat":      "rapidJSON",
                "coordOutputFormat": "EPSG:4326",
                "type_sf":           "any",
                "name_sf":           q,
                "TfNSWSFStopsOnly":  "true",
                "version":           "10.6.21.17",
            })
            _cache_set(q.lower(), code, body)
            self.respond(code, body)

        # ── Trip Planner ─────────────────────────────────────────────────────
        elif path == "/trip":
            frm = qs.get("from", "")
            to  = qs.get("to",   "")
            if not frm or not to:
                self.respond(400, b'{"error":"missing from/to"}'); return

            params = {
                "outputFormat":      "rapidJSON",
                "coordOutputFormat": "EPSG:4326",
                "type_origin":       "stop",
                "name_origin":       frm,
                "type_destination":  "stop",
                "name_destination":  to,
                "TfNSWTR":           "true",
                "version":           "10.6.21.17",
            }

            # ── BUG FIX: pass through itdDate/itdTime the client already computed,
            #    OR fall back to server-side offset calculation using abs(offset).
            #    Previously: checked `if offset > 0` but client sent negative values → never ran.
            if qs.get("itdDate") and qs.get("itdTime"):
                params["itdDate"] = qs["itdDate"]
                params["itdTime"] = qs["itdTime"]
            else:
                raw_offset = qs.get("offset", "0")
                offset_mins = abs(int(raw_offset)) if raw_offset.lstrip("-").isdigit() else 0
                if offset_mins > 0:
                    t_offset = datetime.datetime.now() - datetime.timedelta(minutes=offset_mins)
                    params["itdDate"] = t_offset.strftime("%Y%m%d")
                    params["itdTime"] = t_offset.strftime("%H%M")

            # calcPrevious — ask TfNSW to also return services just before the requested time
            if qs.get("calcPrevious"):
                params["calcPrevious"] = qs["calcPrevious"]

            # Mode exclusions
            excl = qs.get("excl", "")
            if excl:
                params["excludedMeans"] = "checkbox"
                for m in excl.split(","):
                    m = m.strip()
                    if m:
                        params[f"exclMOT_{m}"] = "1"

            code, body = call("/v1/tp/trip", params)
            self.respond(code, body)

        # ── Departure Monitor ─────────────────────────────────────────────────
        elif path == "/depart":
            stop = qs.get("stop", "")
            if not stop:
                self.respond(400, b'{"error":"missing stop"}'); return

            offset_mins = int(qs.get("offset", "0"))
            now = datetime.datetime.now() - datetime.timedelta(minutes=offset_mins)
            params = {
                "outputFormat":          "rapidJSON",
                "coordOutputFormat":     "EPSG:4326",
                "mode":                  "direct",
                "type_dm":               "stop",
                "name_dm":               stop,
                "departureMonitorMacro": "true",
                "TfNSWDM":               "true",
                "itdDate":               now.strftime("%Y%m%d"),
                "itdTime":               now.strftime("%H%M"),
                "version":               "10.6.21.17",
            }
            excl = qs.get("excl", "")
            if excl:
                params["excludedMeans"] = "checkbox"
                for m in excl.split(","):
                    m = m.strip()
                    if m:
                        params[f"exclMOT_{m}"] = "1"

            code, body = call("/v1/tp/departure_mon", params)
            self.respond(code, body)

        # ── Coord / Nearby Stops ──────────────────────────────────────────────
        elif path == "/coord":
            lat = qs.get("lat", "")
            lng = qs.get("lng", "")
            if not lat or not lng:
                self.respond(400, b'{"error":"missing lat/lng"}'); return
            code, body = call("/v1/tp/coord", {
                "outputFormat":      "rapidJSON",
                "coordOutputFormat": "EPSG:4326",
                "type_1":            "GIS_POINT",
                "coord_1":           f"{lng}:{lat}:EPSG:4326",
                "inclFilter":        "1",
                "radius_1":          "500",
                "PoisOnMapMacro":    "true",
                "version":           "10.6.21.17",
            })
            self.respond(code, body)

        # ── Service Alerts ────────────────────────────────────────────────────
        elif path == "/alerts":
            code, body = call("/v1/tp/add_info", {
                "outputFormat":      "rapidJSON",
                "coordOutputFormat": "EPSG:4326",
                "version":           "10.6.21.17",
            })
            self.respond(code, body)

        # ── Health check ──────────────────────────────────────────────────────
        elif path == "/":
            self.respond(200, b'{"status":"NSW Journey Planner proxy running"}')

        else:
            self.respond(404, b'{"error":"not found"}')

    def respond(self, code: int, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Headers", "*")

    def log_message(self, *_):
        pass  # suppress default Apache-style request log (our own print() suffices)

# ── Entry point ───────────────────────────────────────────────────────────────
if not API_KEY:
    print("WARNING: NSW_API_KEY environment variable not set!")

print(f"NSW Journey Planner proxy  →  port {PORT}  (4-worker pool, 512 MB budget)")
print("Endpoints: /  /stops  /trip  /depart  /coord  /alerts")

try:
    PooledHTTPServer(("0.0.0.0", PORT), H, max_workers=4).serve_forever()
except KeyboardInterrupt:
    sys.exit(0)
