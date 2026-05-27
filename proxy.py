"""
NSW Journey Planner - API Proxy
Deploy to Render as a Web Service.
Set environment variable: API_KEY = apikey <your_token>
"""
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.request, urllib.error, urllib.parse, os, sys

PORT = int(os.environ.get("PORT", 3001))
API_KEY = os.environ.get("API_KEY", "")
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

        if path == "/stops":
            q = qs.get("q", "")
            if not q:
                self.respond(400, b'{"error":"missing q"}'); return
            code, body = call("/v1/tp/stop_finder", {
                "outputFormat": "rapidJSON",
                "coordOutputFormat": "EPSG:4326",
                "type_sf": "any",
                "name_sf": q,
                "TfNSWSFStopsOnly": "true",
                "version": "10.6.21.17"
            })
            self.respond(code, body)

        elif path == "/trip":
            frm = qs.get("from", "")
            to = qs.get("to", "")
            if not frm or not to:
                self.respond(400, b'{"error":"missing from/to"}'); return
            code, body = call("/v1/tp/trip", {
                "outputFormat": "rapidJSON",
                "coordOutputFormat": "EPSG:4326",
                "type_origin": "stop",
                "name_origin": frm,
                "type_destination": "stop",
                "name_destination": to,
                "TfNSWTR": "true",
                "version": "10.6.21.17"
            })
            self.respond(code, body)

        elif path == "/":
            self.respond(200, b'{"status":"NSW Journey Planner proxy running"}')

        else:
            self.respond(404, b'{"error":"not found"}')

    def respond(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_): pass

if not API_KEY:
    print("WARNING: API_KEY environment variable not set!")

print("NSW Journey Planner proxy starting on port " + str(PORT))
try:
    HTTPServer(("0.0.0.0", PORT), H).serve_forever()
except KeyboardInterrupt:
    sys.exit(0)
