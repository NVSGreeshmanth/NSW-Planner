"""
NSW Journey Planner - API Proxy
Uses TfNSW Trip Planner APIs:
  /stops?q=NAME          -> Stop Finder API
  /trip?from=ID&to=ID    -> Trip Planner API
  /depart?stop=ID        -> Departure API
  /alerts                -> Service Alert API
"""
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.request, urllib.error, urllib.parse, os, sys

PORT = int(os.environ.get("PORT", 3001))
API_KEY = os.environ.get("NSW_API_KEY", "")
BASE = "https://api.transport.nsw.gov.au"

def call(path, params):
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    req.add_header("Authorization", API_KEY)
    req.add_header("Accept", "application/json")
    print("-> " + url[:100])
    try:
        with urllib.request.urlopen(req) as r:
            body = r.read()
            print("<- " + str(r.status) + " OK (" + str(len(body)) + " bytes)")
            return r.status, body
    except urllib.error.HTTPError as e:
        body = e.read()
        print("<- " + str(e.code) + " ERR: " + body[:120].decode("utf-8", "ignore"))
        return e.code, body

class H(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = dict(urllib.parse.parse_qsl(parsed.query))
        path = parsed.path

        # ── Stop Finder API ──
        if path == "/stops":
            q = qs.get("q", "")
            if not q:
                self.respond(400, b'{"error":"missing q"}'); return
            code, body = call("/v1/tp/stop_finder", {
                "outputFormat":       "rapidJSON",
                "coordOutputFormat":  "EPSG:4326",
                "type_sf":            "any",
                "name_sf":            q,
                "TfNSWSFStopsOnly":   "true",
                "version":            "10.6.21.17"
            })
            self.respond(code, body)

        # ── Trip Planner API ──
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
                "version":           "10.6.21.17"
            }
            # Mode exclusions: excl=5,7,9,4 etc.
            excl = qs.get("excl", "")
            if excl:
                params["excludedMeans"] = "checkbox"
                for m in excl.split(","):
                    m = m.strip()
                    if m:
                        params["exclMOT_" + m] = "1"
            code, body = call("/v1/tp/trip", params)
            self.respond(code, body)

        # ── Departure API ──
        elif path == "/depart":
            stop = qs.get("stop", "")
            if not stop:
                self.respond(400, b'{"error":"missing stop"}'); return
            params = {
                "outputFormat":          "rapidJSON",
                "coordOutputFormat":     "EPSG:4326",
                "mode":                  "direct",
                "type_dm":               "stop",
                "name_dm":               stop,
                "departureMonitorMacro": "true",
                "TfNSWDM":               "true",
                "version":               "10.6.21.17"
            }
            excl = qs.get("excl", "")
            if excl:
                params["excludedMeans"] = "checkbox"
                for m in excl.split(","):
                    m = m.strip()
                    if m:
                        params["exclMOT_" + m] = "1"
            code, body = call("/v1/tp/departure_mon", params)
            self.respond(code, body)

        # ── Service Alert API ──
        elif path == "/alerts":
            code, body = call("/v1/tp/add_info", {
                "outputFormat":      "rapidJSON",
                "coordOutputFormat": "EPSG:4326",
                "version":           "10.6.21.17"
            })
            self.respond(code, body)

        # ── Health check ──
        elif path == "/":
            self.respond(200, b'{"status":"NSW Journey Planner proxy running"}')

        else:
            self.respond(404, b'{"error":"not found"}')

    def respond(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_): pass

if not API_KEY:
    print("WARNING: NSW_API_KEY environment variable not set!")

print("NSW Journey Planner proxy on port " + str(PORT))
print("Endpoints: /stops /trip /depart /alerts")
try:
    HTTPServer(("0.0.0.0", PORT), H).serve_forever()
except KeyboardInterrupt:
    sys.exit(0)
