"""Local development server: runs the same API code against SQLite, serves the frontend.

    python backend/local_server.py --seed      # then open http://localhost:8000

Auth in local mode is a demo token "dev:<email>:<role>" (role = provider | receiver | admin).
It exists ONLY here; the deployed API uses Cognito JWTs verified by API Gateway.
"""
import argparse
import json
import logging
import mimetypes
import os
import sys
import uuid
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parent
FRONTEND = ROOT / "frontend"
os.environ.setdefault("SQLITE_PATH", str(ROOT / "local_data" / "foodshare.db"))
os.environ.setdefault("LOCAL_UPLOAD_DIR", str(ROOT / "local_data" / "uploads"))
sys.path.insert(0, str(BACKEND))

import app  # noqa: E402
import db  # noqa: E402
import storage  # noqa: E402


def build_event(handler, method, path, query, body):
    claims = {}
    auth = handler.headers.get("Authorization", "")
    if auth.startswith("Bearer dev:"):
        parts = auth[len("Bearer dev:"):].split(":")
        if len(parts) == 2 and "@" in parts[0] and parts[1] in ("provider", "receiver", "admin"):
            email, role = parts[0].lower(), parts[1]
            claims = {
                "sub": "dev-" + email,
                "email": email,
                "custom:role": role if role != "admin" else "",
                "cognito:groups": "[admin]" if role == "admin" else "",
            }
    return {
        "version": "2.0",
        "rawPath": path,
        "queryStringParameters": {k: v[0] for k, v in query.items()} or None,
        "headers": {},
        "requestContext": {
            "http": {"method": method, "path": path},
            "authorizer": {"jwt": {"claims": claims}} if claims else {},
        },
        "body": body,
        "isBase64Encoded": False,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "FoodShareLocal/1.0"

    def log_message(self, fmt, *args):
        logging.info("%s - %s", self.address_string(), fmt % args)

    def _send(self, status, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _api(self, method, parsed):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else None
        path = parsed.path[len("/api"):] or "/"
        event = build_event(self, method, path, parse_qs(parsed.query), body)
        res = app.lambda_handler(event)
        self._send(res["statusCode"], res["body"], extra={"Cache-Control": "no-store"})

    def _static(self, parsed):
        rel = parsed.path.lstrip("/") or "index.html"
        target = (FRONTEND / rel).resolve()
        if FRONTEND.resolve() not in target.parents and target != FRONTEND.resolve():
            return self._send(403, "Forbidden", "text/plain")
        if not target.is_file():
            return self._send(404, "Not found", "text/plain")
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), ctype)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            return self._api("GET", parsed)
        if parsed.path.startswith("/local-files/"):
            p = storage.local_path(parsed.path[len("/local-files/"):])
            if p is None or not p.is_file():
                return self._send(404, "Not found", "text/plain")
            return self._send(200, p.read_bytes(), mimetypes.guess_type(str(p))[0] or "image/jpeg")
        self._static(parsed)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            return self._api("POST", parsed)
        self._send(404, "Not found", "text/plain")

    def do_PUT(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/local-uploads/"):
            p = storage.local_path(parsed.path[len("/local-uploads/"):])
            length = int(self.headers.get("Content-Length") or 0)
            if p is None or length <= 0 or length > storage.MAX_BYTES:
                return self._send(400, json.dumps({"error": "Invalid upload"}))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(self.rfile.read(length))
            return self._send(200, "{}")
        self._send(404, "Not found", "text/plain")


def seed():
    """Demo data so every screen has something to show."""
    db.init()
    if db.query("SELECT id FROM users LIMIT 1"):
        print("Database already has data - skipping seed.")
        return
    now = app.utcnow()
    users = [
        ("dev-canteen@demo.test", "canteen@demo.test", "City College Canteen", "provider", "9000000001", "Main Campus Road", 28.6139, 77.2090),
        ("dev-bistro@demo.test", "bistro@demo.test", "Green Leaf Restaurant", "provider", "9000000002", "Market Street 12", 28.6280, 77.2190),
        ("dev-ngo@demo.test", "ngo@demo.test", "Helping Hands NGO", "receiver", "9000000003", "Community Centre, Sector 4", 28.6200, 77.2150),
        ("dev-volunteer@demo.test", "volunteer@demo.test", "Asha (Volunteer)", "receiver", "9000000004", "Lakeview Colony", 28.5700, 77.3200),
    ]
    for u in users:
        db.execute(
            "INSERT INTO users (id, email, name, role, phone, address, latitude, longitude, created_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            u + (app.iso(now),),
        )
    listings = [
        ("dev-canteen@demo.test", "Vegetable biryani", "cooked", 40, 2, "Main Campus Road", 28.6139, 77.2090),
        ("dev-canteen@demo.test", "Chapati and dal", "cooked", 25, 4, "Main Campus Road", 28.6139, 77.2090),
        ("dev-bistro@demo.test", "Bread loaves and buns", "bakery", 30, 6, "Market Street 12", 28.6280, 77.2190),
        ("dev-bistro@demo.test", "Fruit boxes", "fruits_veg", 15, 20, "Market Street 12", 28.6280, 77.2190),
    ]
    for pid, title, ftype, qty, hours, addr, lat, lng in listings:
        db.execute(
            "INSERT INTO listings (id, provider_id, title, food_type, description, quantity, expiry_time,"
            " address, latitude, longitude, image_key, status, created_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,'available',%s)",
            (uuid.uuid4().hex, pid, title, ftype, "Freshly prepared surplus.", qty,
             app.iso(now + timedelta(hours=hours)), addr, lat, lng, app.iso(now)),
        )
    db.commit()
    print("Seeded demo users: canteen@demo.test, bistro@demo.test (providers), ngo@demo.test, volunteer@demo.test (receivers).")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--seed", action="store_true", help="insert demo data if the database is empty")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.seed:
        seed()
        db.close()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"FoodShare running at http://localhost:{args.port}  (database: {os.environ['SQLITE_PATH']})")
    print("Sign in with any email + a role, e.g. ngo@demo.test as receiver, admin@demo.test as admin.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
