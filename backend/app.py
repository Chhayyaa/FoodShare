"""Food Waste Management API.

AWS Lambda handler for an API Gateway HTTP API (payload format 2.0).
Authentication is done by API Gateway (Cognito JWT authorizer); this code only
reads the verified claims from the event.
"""
import base64
import json
import logging
import math
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import db
import geo
import notify
import storage

log = logging.getLogger()
log.setLevel(logging.INFO)

ROLES = ("provider", "receiver")
FOOD_TYPES = ("cooked", "raw", "bakery", "packaged", "fruits_veg", "beverages", "other")
NOTIFY_RADIUS_KM = float(os.environ.get("NOTIFY_RADIUS_KM", "10"))
MAX_NOTIFICATIONS_PER_LISTING = 50
TRAVEL_SPEED_KMH = 25.0   # listings that cannot be reached before expiry are hidden
DISTANCE_WEIGHT = 0.1     # ranking: hours_left + 0.1 * distance_km (lower is better)
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


# ---------------------------------------------------------------- utilities
class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime(TS_FORMAT)


def from_iso(s):
    return datetime.strptime(s, TS_FORMAT).replace(tzinfo=timezone.utc)


def parse_time(value, field):
    if not isinstance(value, str):
        raise ApiError(400, f"{field} must be an ISO-8601 timestamp")
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ApiError(400, f"{field} must be an ISO-8601 timestamp")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def text(data, key, required=True, max_len=200):
    v = data.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            raise ApiError(400, f"{key} is required")
        return None
    if not isinstance(v, str):
        raise ApiError(400, f"{key} must be text")
    v = v.strip()
    if len(v) > max_len:
        raise ApiError(400, f"{key} must be at most {max_len} characters")
    return v


def number(data, key, lo, hi, required=True, default=None, integer=False):
    v = data.get(key, default)
    if v is None or v == "":
        if required:
            raise ApiError(400, f"{key} is required")
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        raise ApiError(400, f"{key} must be a number")
    if not (lo <= n <= hi):  # also rejects NaN
        raise ApiError(400, f"{key} must be between {lo} and {hi}")
    return int(n) if integer else n


def json_default(o):
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    raise TypeError(f"Cannot serialise {type(o)}")


def respond(status, body, headers=None):
    response_headers = {
        "Content-Type": "application/json",
        "Cache-Control": "no-store",
        # API Gateway's default authorizer would otherwise reject the CORS
        # preflight before the browser can send its Bearer token.
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
        "Access-Control-Allow-Headers": "Authorization,Content-Type",
    }
    if headers:
        response_headers.update(headers)
    return {
        "statusCode": status,
        "headers": response_headers,
        "body": json.dumps(body, default=json_default),
    }


class Ctx:
    def __init__(self, event, params):
        self.event = event
        self.params = params
        self.query = event.get("queryStringParameters") or {}
        self._body = None
        self._identity = None

    @property
    def body(self):
        if self._body is None:
            raw = self.event.get("body") or "{}"
            if self.event.get("isBase64Encoded"):
                raw = base64.b64decode(raw).decode("utf-8")
            try:
                data = json.loads(raw)
            except ValueError:
                raise ApiError(400, "Body must be valid JSON")
            if not isinstance(data, dict):
                raise ApiError(400, "Body must be a JSON object")
            self._body = data
        return self._body

    @property
    def identity(self):
        if self._identity is None:
            auth = (self.event.get("requestContext") or {}).get("authorizer") or {}
            claims = (auth.get("jwt") or {}).get("claims") or {}
            sub = claims.get("sub")
            if not sub:
                raise ApiError(401, "Authentication required")
            groups = re.split(r"[\s,\[\]]+", str(claims.get("cognito:groups") or ""))
            role = claims.get("custom:role")
            self._identity = {
                "id": sub,
                "email": claims.get("email") or "",
                "role": role if role in ROLES else None,
                # Admin comes ONLY from the Cognito group, never from a user-editable attribute.
                "is_admin": "admin" in groups,
            }
        return self._identity


ROUTES = []


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile(pattern), fn))
        return fn

    return deco


def find_user(user_id):
    rows = db.query("SELECT * FROM users WHERE id = %s", (user_id,))
    return rows[0] if rows else None


def current_user(ctx):
    user = find_user(ctx.identity["id"])
    if not user:
        raise ApiError(403, "Complete your profile first")
    return user


def require_role(ctx, role):
    user = current_user(ctx)
    if user["role"] != role:
        raise ApiError(403, f"Only {role}s can do this")
    return user


def require_admin(ctx):
    if not ctx.identity["is_admin"]:
        raise ApiError(403, "Admin access required")


def listing_out(row):
    out = dict(row)
    key = out.pop("image_key", None)
    out["image_url"] = storage.image_url(key) if key else None
    if out.get("status") == "available" and out["expiry_time"] <= iso(utcnow()):
        out["status"] = "expired"
    return out


def request_out(row):
    out = dict(row)
    out["pickup_code"] = out["id"][:6].upper()
    key = out.pop("image_key", None)
    if "title" in out:
        out["image_url"] = storage.image_url(key) if key else None
    return out


# ------------------------------------------------------------------- routes
@route("GET", r"/health")
def health(ctx):
    return {"ok": True}


@route("GET", r"/me")
def get_me(ctx):
    ident = ctx.identity
    return {"profile": find_user(ident["id"]), "is_admin": ident["is_admin"], "role": ident["role"]}


@route("POST", r"/me")
def save_profile(ctx):
    ident, b = ctx.identity, ctx.body
    name = text(b, "name", max_len=100)
    phone = text(b, "phone", required=False, max_len=30)
    address = text(b, "address", required=False, max_len=300)
    lat = number(b, "latitude", -90, 90)
    lng = number(b, "longitude", -180, 180)
    if find_user(ident["id"]):
        db.execute(
            "UPDATE users SET name=%s, phone=%s, address=%s, latitude=%s, longitude=%s WHERE id=%s",
            (name, phone, address, lat, lng, ident["id"]),
        )
        status = 200
    else:
        if ident["role"] is None:
            raise ApiError(400, "This account has no valid role (provider or receiver)")
        db.execute(
            "INSERT INTO users (id, email, name, role, phone, address, latitude, longitude, created_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (ident["id"], ident["email"], name, ident["role"], phone, address, lat, lng, iso(utcnow())),
        )
        notify.subscribe_user(ident["id"], ident["email"])
        status = 201
    return status, {"profile": find_user(ident["id"])}


@route("POST", r"/uploads/presign")
def presign(ctx):
    current_user(ctx)
    try:
        return storage.presign_upload(text(ctx.body, "content_type", max_len=50))
    except ValueError as e:
        raise ApiError(400, str(e))


@route("GET", r"/listings")
def browse_listings(ctx):
    """Food available near a point, hiding unreachable items, best match first."""
    user = current_user(ctx)
    lat = number(ctx.query, "lat", -90, 90, required=False, default=user["latitude"])
    lng = number(ctx.query, "lng", -180, 180, required=False, default=user["longitude"])
    if lat is None or lng is None:
        raise ApiError(400, "Location is required")
    radius = number(ctx.query, "radius_km", 0.5, 100, required=False, default=10)
    now = utcnow()
    min_lat, max_lat, min_lng, max_lng = geo.bounding_box(lat, lng, radius)
    rows = db.query(
        "SELECT l.*, u.name AS provider_name FROM listings l JOIN users u ON u.id = l.provider_id"
        " WHERE l.status = 'available' AND l.expiry_time > %s"
        " AND l.latitude BETWEEN %s AND %s AND l.longitude BETWEEN %s AND %s"
        " ORDER BY l.expiry_time LIMIT 500",
        (iso(now), min_lat, max_lat, min_lng, max_lng),
    )
    ranked = []
    for r in rows:
        dist = geo.haversine_km(lat, lng, r["latitude"], r["longitude"])
        hours_left = (from_iso(r["expiry_time"]) - now).total_seconds() / 3600
        if dist > radius or dist / TRAVEL_SPEED_KMH >= hours_left:
            continue
        out = listing_out(r)
        out["distance_km"] = round(dist, 1)
        out["hours_left"] = round(hours_left, 1)
        ranked.append((hours_left + DISTANCE_WEIGHT * dist, out))
    ranked.sort(key=lambda t: t[0])
    listings = [o for _, o in ranked]
    return {"listings": listings, "count": len(listings)}


@route("POST", r"/listings")
def create_listing(ctx):
    user = require_role(ctx, "provider")
    b = ctx.body
    title = text(b, "title", max_len=120)
    food_type = text(b, "food_type", max_len=30)
    if food_type not in FOOD_TYPES:
        raise ApiError(400, "food_type must be one of: " + ", ".join(FOOD_TYPES))
    quantity = number(b, "quantity", 1, 100000, integer=True)
    expiry = parse_time(b.get("expiry_time"), "expiry_time")
    now = utcnow()
    if expiry < now + timedelta(minutes=5):
        raise ApiError(400, "Expiry time must be at least 5 minutes in the future")
    if expiry > now + timedelta(days=14):
        raise ApiError(400, "Expiry time must be within 14 days")
    lat = number(b, "latitude", -90, 90, required=False, default=user["latitude"])
    lng = number(b, "longitude", -180, 180, required=False, default=user["longitude"])
    if lat is None or lng is None:
        raise ApiError(400, "Location is required")
    address = text(b, "address", required=False, max_len=300) or user["address"]
    description = text(b, "description", required=False, max_len=500)
    image_key = b.get("image_key") or None
    if image_key and not storage.valid_key(image_key):
        raise ApiError(400, "Invalid image_key")

    lid = uuid.uuid4().hex
    db.execute(
        "INSERT INTO listings (id, provider_id, title, food_type, description, quantity, expiry_time,"
        " address, latitude, longitude, image_key, status, created_at)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'available',%s)",
        (lid, user["id"], title, food_type, description, quantity, iso(expiry),
         address, lat, lng, image_key, iso(now)),
    )

    # Match with nearby NGOs / volunteers and alert them.
    receivers = db.query(
        "SELECT id, latitude, longitude FROM users"
        " WHERE role = 'receiver' AND latitude IS NOT NULL AND longitude IS NOT NULL LIMIT 1000"
    )
    notified = 0
    for r in receivers:
        if notified >= MAX_NOTIFICATIONS_PER_LISTING:
            break
        d = geo.haversine_km(lat, lng, r["latitude"], r["longitude"])
        if d <= NOTIFY_RADIUS_KM:
            notify.send(
                r["id"],
                "New food available nearby",
                f"{title} ({quantity} servings) from {user['name']}, {d:.1f} km away. "
                f"Expires {iso(expiry)} (UTC). Open FoodShare to claim it.",
            )
            notified += 1
    row = db.query("SELECT * FROM listings WHERE id = %s", (lid,))[0]
    return 201, {"listing": listing_out(row), "notified": notified}


@route("GET", r"/listings/mine")
def my_listings(ctx):
    user = require_role(ctx, "provider")
    rows = db.query(
        "SELECT l.*, r.id AS request_id, r.status AS request_status, r.created_at AS requested_at,"
        " u.name AS receiver_name, u.phone AS receiver_phone"
        " FROM listings l"
        " LEFT JOIN requests r ON r.listing_id = l.id AND r.status IN ('confirmed','collected','completed')"
        " LEFT JOIN users u ON u.id = r.receiver_id"
        " WHERE l.provider_id = %s ORDER BY l.created_at DESC LIMIT 200",
        (user["id"],),
    )
    return {"listings": [listing_out(r) for r in rows]}


@route("POST", r"/listings/(?P<id>[0-9a-f]{32})/request")
def request_listing(ctx):
    user = require_role(ctx, "receiver")
    lid = ctx.params["id"]
    now = utcnow()
    # Atomic claim: only one receiver can flip available -> booked.
    claimed = db.execute(
        "UPDATE listings SET status = 'booked' WHERE id = %s AND status = 'available' AND expiry_time > %s",
        (lid, iso(now)),
    )
    if claimed != 1:
        row = db.query("SELECT status, expiry_time FROM listings WHERE id = %s", (lid,))
        if not row:
            raise ApiError(404, "Listing not found")
        if row[0]["status"] == "available":
            raise ApiError(410, "This listing has expired")
        raise ApiError(409, "Someone else has already requested this food")
    listing = db.query(
        "SELECT l.*, p.name AS provider_name, p.phone AS provider_phone FROM listings l"
        " JOIN users p ON p.id = l.provider_id WHERE l.id = %s",
        (lid,),
    )[0]
    rid = uuid.uuid4().hex
    code = rid[:6].upper()
    note = (
        f"Collect from {listing['provider_name']} at {listing['address'] or 'the listed location'} "
        f"before {listing['expiry_time']} (UTC). Quote pickup code {code}. Please bring clean containers."
    )
    db.execute(
        "INSERT INTO requests (id, listing_id, receiver_id, status, pickup_note, created_at)"
        " VALUES (%s,%s,%s,'confirmed',%s,%s)",
        (rid, lid, user["id"], note, iso(now)),
    )
    notify.send(
        listing["provider_id"],
        "Your food has been claimed",
        f"{user['name']} will collect '{listing['title']}'. Pickup code {code}. Contact: {user['phone'] or 'n/a'}.",
    )
    notify.send(user["id"], "Pickup confirmed", note)
    req = db.query("SELECT * FROM requests WHERE id = %s", (rid,))[0]
    return 201, {"request": request_out(req), "listing": listing_out(listing)}


@route("GET", r"/requests/mine")
def my_requests(ctx):
    user = require_role(ctx, "receiver")
    rows = db.query(
        "SELECT r.*, l.title, l.food_type, l.quantity, l.expiry_time, l.address, l.latitude, l.longitude,"
        " l.image_key, l.provider_id, p.name AS provider_name, p.phone AS provider_phone"
        " FROM requests r JOIN listings l ON l.id = r.listing_id JOIN users p ON p.id = l.provider_id"
        " WHERE r.receiver_id = %s ORDER BY r.created_at DESC LIMIT 200",
        (user["id"],),
    )
    return {"requests": [request_out(r) for r in rows]}


def load_request(rid):
    rows = db.query(
        "SELECT r.*, l.provider_id, l.title FROM requests r JOIN listings l ON l.id = r.listing_id"
        " WHERE r.id = %s",
        (rid,),
    )
    if not rows:
        raise ApiError(404, "Request not found")
    return rows[0]


@route("POST", r"/requests/(?P<id>[0-9a-f]{32})/collect")
def collect(ctx):
    user = current_user(ctx)
    req = load_request(ctx.params["id"])
    if req["receiver_id"] != user["id"]:
        raise ApiError(403, "Only the receiver can mark food as collected")
    n = db.execute(
        "UPDATE requests SET status = 'collected', collected_at = %s WHERE id = %s AND status = 'confirmed'",
        (iso(utcnow()), req["id"]),
    )
    if n != 1:
        raise ApiError(409, f"Request is already {req['status']}")
    notify.send(req["provider_id"], "Food collected", f"'{req['title']}' has been picked up by {user['name']}.")
    return {"request": request_out(db.query("SELECT * FROM requests WHERE id = %s", (req["id"],))[0])}


@route("POST", r"/requests/(?P<id>[0-9a-f]{32})/complete")
def complete(ctx):
    user = current_user(ctx)
    req = load_request(ctx.params["id"])
    is_provider = req["provider_id"] == user["id"]
    is_receiver = req["receiver_id"] == user["id"]
    if not (is_provider or is_receiver):
        raise ApiError(403, "Not your request")
    allowed = ["collected"] + (["confirmed"] if is_provider else [])
    if req["status"] not in allowed:
        raise ApiError(409, f"Cannot complete a request that is {req['status']}")
    now = iso(utcnow())
    n = db.execute(
        "UPDATE requests SET status = 'completed', collected_at = COALESCE(collected_at, %s),"
        " completed_at = %s WHERE id = %s AND status = %s",
        (now, now, req["id"], req["status"]),
    )
    if n != 1:
        raise ApiError(409, "Request changed, please refresh")
    db.execute("UPDATE listings SET status = 'completed' WHERE id = %s", (req["listing_id"],))
    other = req["receiver_id"] if is_provider else req["provider_id"]
    notify.send(other, "Handover completed", f"'{req['title']}' has been marked as completed. Thank you!")
    return {"request": request_out(db.query("SELECT * FROM requests WHERE id = %s", (req["id"],))[0])}


@route("POST", r"/requests/(?P<id>[0-9a-f]{32})/cancel")
def cancel(ctx):
    user = current_user(ctx)
    req = load_request(ctx.params["id"])
    is_provider = req["provider_id"] == user["id"]
    if not (is_provider or req["receiver_id"] == user["id"]):
        raise ApiError(403, "Not your request")
    n = db.execute("UPDATE requests SET status = 'cancelled' WHERE id = %s AND status = 'confirmed'", (req["id"],))
    if n != 1:
        raise ApiError(409, f"Cannot cancel a request that is {req['status']}")
    db.execute("UPDATE listings SET status = 'available' WHERE id = %s AND status = 'booked'", (req["listing_id"],))
    other = req["receiver_id"] if is_provider else req["provider_id"]
    notify.send(other, "Pickup cancelled", f"The pickup of '{req['title']}' was cancelled.")
    return {"request": request_out(db.query("SELECT * FROM requests WHERE id = %s", (req["id"],))[0])}


# ------------------------------------------------------------------- admin
@route("GET", r"/admin/stats")
def admin_stats(ctx):
    require_admin(ctx)
    now = iso(utcnow())
    users = {r["role"]: r["n"] for r in db.query("SELECT role, COUNT(*) AS n FROM users GROUP BY role")}
    t = db.query(
        "SELECT COUNT(*) AS total,"
        " COALESCE(SUM(CASE WHEN status='available' AND expiry_time > %s THEN 1 ELSE 0 END),0) AS available,"
        " COALESCE(SUM(CASE WHEN status='booked' THEN 1 ELSE 0 END),0) AS booked,"
        " COALESCE(SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END),0) AS completed,"
        " COALESCE(SUM(CASE WHEN status='available' AND expiry_time <= %s THEN 1 ELSE 0 END),0) AS expired,"
        " COALESCE(SUM(CASE WHEN status='completed' THEN quantity ELSE 0 END),0) AS rescued_servings,"
        " COALESCE(SUM(CASE WHEN status='available' AND expiry_time <= %s THEN quantity ELSE 0 END),0) AS wasted_servings"
        " FROM listings",
        (now, now, now),
    )[0]
    rescued, wasted = int(t["rescued_servings"]), int(t["wasted_servings"])
    since = iso(utcnow() - timedelta(days=14))
    daily = db.query(
        "SELECT substr(r.completed_at, 1, 10) AS day, COUNT(*) AS pickups, SUM(l.quantity) AS servings"
        " FROM requests r JOIN listings l ON l.id = r.listing_id"
        " WHERE r.status = 'completed' AND r.completed_at >= %s"
        " GROUP BY substr(r.completed_at, 1, 10) ORDER BY day",
        (since,),
    )
    top = db.query(
        "SELECT u.name AS name, COUNT(*) AS pickups, SUM(l.quantity) AS servings"
        " FROM listings l JOIN users u ON u.id = l.provider_id WHERE l.status = 'completed'"
        " GROUP BY u.id, u.name ORDER BY servings DESC LIMIT 5"
    )
    return {
        "users": {"providers": users.get("provider", 0), "receivers": users.get("receiver", 0)},
        "listings": {k: int(t[k]) for k in ("total", "available", "booked", "completed", "expired")},
        "rescued_servings": rescued,
        "wasted_servings": wasted,
        "rescue_rate": round(rescued / (rescued + wasted), 3) if (rescued + wasted) else None,
        "daily": daily,
        "top_providers": top,
    }


@route("GET", r"/admin/listings")
def admin_listings(ctx):
    require_admin(ctx)
    rows = db.query(
        "SELECT l.*, u.name AS provider_name FROM listings l JOIN users u ON u.id = l.provider_id"
        " ORDER BY l.created_at DESC LIMIT 500"
    )
    return {"listings": [listing_out(r) for r in rows]}


@route("GET", r"/admin/users")
def admin_users(ctx):
    require_admin(ctx)
    return {"users": db.query("SELECT id, email, name, role, phone, address, created_at FROM users ORDER BY created_at DESC LIMIT 500")}


# ----------------------------------------------------------------- handler
def lambda_handler(event, context=None):
    try:
        http = (event.get("requestContext") or {}).get("http") or {}
        method = http.get("method") or event.get("httpMethod") or "GET"
        path = (event.get("rawPath") or http.get("path") or "/").rstrip("/") or "/"
        # This route deliberately bypasses the JWT authorizer in the SAM
        # template. It has no application behavior; it only completes the
        # browser's CORS negotiation.
        if method == "OPTIONS":
            return respond(204, {})
        db.init()
        path_matched = False
        for m, pattern, fn in ROUTES:
            match = pattern.fullmatch(path)
            if not match:
                continue
            path_matched = True
            if m != method:
                continue
            result = fn(Ctx(event, match.groupdict()))
            db.commit()
            status, body = result if isinstance(result, tuple) else (200, result)
            return respond(status, body)
        raise ApiError(405 if path_matched else 404, "Method not allowed" if path_matched else "Not found")
    except ApiError as e:
        db.rollback()
        return respond(e.status, {"error": e.message})
    except Exception:
        db.rollback()
        log.exception("Unhandled error")
        return respond(500, {"error": "Internal server error"})
    finally:
        db.close()
