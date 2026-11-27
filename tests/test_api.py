import json
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import app  # noqa: E402
import db  # noqa: E402
import notify  # noqa: E402


def call(method, path, user=None, body=None, query=None):
    """user = (email, role) where role is provider | receiver | admin."""
    claims = {}
    if user:
        email, role = user
        claims = {
            "sub": "u-" + email,
            "email": email,
            "custom:role": "" if role == "admin" else role,
            "cognito:groups": "[admin]" if role == "admin" else "",
        }
    event = {
        "rawPath": path,
        "queryStringParameters": query,
        "requestContext": {
            "http": {"method": method},
            "authorizer": {"jwt": {"claims": claims}} if claims else {},
        },
        "body": json.dumps(body) if body is not None else None,
    }
    res = app.lambda_handler(event)
    return res["statusCode"], json.loads(res["body"])


PROVIDER = ("canteen@t.test", "provider")
NEAR = ("ngo@t.test", "receiver")
FAR = ("far@t.test", "receiver")
ADMIN = ("admin@t.test", "admin")


def expiry(hours):
    return app.iso(app.utcnow() + timedelta(hours=hours))


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SQLITE_PATH"] = os.path.join(self.tmp.name, "t.db")
        os.environ.pop("DB_HOST", None)
        os.environ.pop("SNS_TOPIC_ARN", None)
        os.environ.pop("UPLOAD_BUCKET", None)
        notify.OUTBOX.clear()
        for user, lat, lng in ((PROVIDER, 28.6139, 77.2090), (NEAR, 28.62, 77.215), (FAR, 28.20, 77.80)):
            s, _ = call("POST", "/me", user, {"name": user[0], "latitude": lat, "longitude": lng, "address": "x"})
            self.assertEqual(s, 201)
        notify.OUTBOX.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def make_listing(self, hours=3, **extra):
        body = {"title": "Biryani", "food_type": "cooked", "quantity": 20, "expiry_time": expiry(hours)}
        body.update(extra)
        s, data = call("POST", "/listings", PROVIDER, body)
        self.assertEqual(s, 201, data)
        return data["listing"]

    def test_requires_authentication(self):
        s, _ = call("GET", "/listings")
        self.assertEqual(s, 401)

    def test_unknown_route_and_method(self):
        self.assertEqual(call("GET", "/nope", PROVIDER)[0], 404)
        self.assertEqual(call("DELETE", "/listings", PROVIDER)[0], 405)

    def test_role_enforcement(self):
        body = {"title": "x", "food_type": "cooked", "quantity": 1, "expiry_time": expiry(2)}
        self.assertEqual(call("POST", "/listings", NEAR, body)[0], 403)
        listing = self.make_listing()
        self.assertEqual(call("POST", f"/listings/{listing['id']}/request", PROVIDER)[0], 403)
        self.assertEqual(call("GET", "/admin/stats", PROVIDER)[0], 403)

    def test_validation(self):
        past = {"title": "x", "food_type": "cooked", "quantity": 5, "expiry_time": expiry(-1)}
        self.assertEqual(call("POST", "/listings", PROVIDER, past)[0], 400)
        bad_type = {"title": "x", "food_type": "gold", "quantity": 5, "expiry_time": expiry(2)}
        self.assertEqual(call("POST", "/listings", PROVIDER, bad_type)[0], 400)
        bad_qty = {"title": "x", "food_type": "cooked", "quantity": 0, "expiry_time": expiry(2)}
        self.assertEqual(call("POST", "/listings", PROVIDER, bad_qty)[0], 400)

    def test_new_listing_notifies_nearby_receivers_only(self):
        s, data = call("POST", "/listings", PROVIDER,
                       {"title": "Dal", "food_type": "cooked", "quantity": 10, "expiry_time": expiry(3)})
        self.assertEqual(data["notified"], 1)
        self.assertEqual([n["user_id"] for n in notify.OUTBOX], ["u-" + NEAR[0]])

    def test_browse_filters_by_distance_and_ranks_by_urgency(self):
        later = self.make_listing(hours=10, title="Later")
        soon = self.make_listing(hours=2, title="Soon")
        s, data = call("GET", "/listings", NEAR)
        self.assertEqual([l["id"] for l in data["listings"]], [soon["id"], later["id"]])
        self.assertIn("distance_km", data["listings"][0])
        s, data = call("GET", "/listings", FAR)
        self.assertEqual(data["count"], 0)  # "No food available nearby"

    def test_unreachable_listing_is_hidden(self):
        self.make_listing(hours=0.1)  # 6 minutes left
        s, data = call("GET", "/listings", NEAR, query={"lat": "28.20", "lng": "77.20", "radius_km": "100"})
        self.assertEqual(data["count"], 0)

    def test_only_one_receiver_can_claim(self):
        listing = self.make_listing()
        s, data = call("POST", f"/listings/{listing['id']}/request", NEAR)
        self.assertEqual(s, 201)
        self.assertEqual(len(data["request"]["pickup_code"]), 6)
        s, data = call("POST", f"/listings/{listing['id']}/request", FAR)
        self.assertEqual(s, 409)
        s, data = call("GET", "/listings", NEAR)
        self.assertEqual(data["count"], 0)

    def test_full_pickup_flow_and_admin_stats(self):
        listing = self.make_listing(quantity=30)
        s, data = call("POST", f"/listings/{listing['id']}/request", NEAR)
        rid = data["request"]["id"]
        # receiver cannot complete before collecting
        self.assertEqual(call("POST", f"/requests/{rid}/complete", NEAR)[0], 409)
        self.assertEqual(call("POST", f"/requests/{rid}/collect", NEAR)[0], 200)
        self.assertEqual(call("POST", f"/requests/{rid}/collect", PROVIDER)[0], 403)
        s, data = call("POST", f"/requests/{rid}/complete", PROVIDER)
        self.assertEqual(s, 200)
        self.assertEqual(data["request"]["status"], "completed")
        s, data = call("GET", "/listings/mine", PROVIDER)
        self.assertEqual(data["listings"][0]["status"], "completed")
        self.assertEqual(data["listings"][0]["receiver_name"], NEAR[0])
        s, stats = call("GET", "/admin/stats", ADMIN)
        self.assertEqual(s, 200)
        self.assertEqual(stats["rescued_servings"], 30)
        self.assertEqual(stats["listings"]["completed"], 1)
        self.assertEqual(stats["rescue_rate"], 1.0)

    def test_cancel_returns_food_to_pool(self):
        listing = self.make_listing()
        s, data = call("POST", f"/listings/{listing['id']}/request", NEAR)
        rid = data["request"]["id"]
        self.assertEqual(call("POST", f"/requests/{rid}/cancel", NEAR)[0], 200)
        s, data = call("GET", "/listings", NEAR)
        self.assertEqual(data["count"], 1)
        self.assertEqual(call("POST", f"/listings/{listing['id']}/request", FAR)[0], 201)

    def test_expired_food_counts_as_wasted(self):
        listing = self.make_listing(hours=1, quantity=12)
        db.init()
        db.execute("UPDATE listings SET expiry_time = %s WHERE id = %s", (expiry(-1), listing["id"]))
        db.commit()
        db.close()
        s, stats = call("GET", "/admin/stats", ADMIN)
        self.assertEqual(stats["wasted_servings"], 12)
        self.assertEqual(stats["listings"]["expired"], 1)
        s, data = call("GET", "/listings/mine", PROVIDER)
        self.assertEqual(data["listings"][0]["status"], "expired")

    def test_presign_validates_content_type(self):
        self.assertEqual(call("POST", "/uploads/presign", PROVIDER, {"content_type": "text/html"})[0], 400)
        s, data = call("POST", "/uploads/presign", PROVIDER, {"content_type": "image/png"})
        self.assertEqual(s, 200)
        self.assertTrue(data["key"].endswith(".png"))

    def test_bad_image_key_rejected(self):
        body = {"title": "x", "food_type": "cooked", "quantity": 5, "expiry_time": expiry(2),
                "image_key": "../../etc/passwd"}
        self.assertEqual(call("POST", "/listings", PROVIDER, body)[0], 400)


if __name__ == "__main__":
    unittest.main()
