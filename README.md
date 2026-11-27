# FoodShare: Cloud-Based Food Waste Management System

**FoodShare** is a cloud-based food waste management system designed to bridge the gap between surplus food providers and organizations working to combat hunger. The platform enables food providers to list surplus food, allows NGOs and volunteers to claim and coordinate its collection, and provides administrators with a dashboard to monitor food redistribution and track its impact.

Built using AWS services, including AWS Lambda, API Gateway, Amazon RDS (PostgreSQL), Amazon S3, Amazon Cognito, Amazon SNS, and CloudWatch, FoodShare demonstrates how serverless cloud architecture can support scalable, accessible, and efficient food redistribution.

**Goal:** Reduce food wastage, improve food accessibility, and promote sustainable communities through technology.

##

Canteens, restaurants, hostels and event organisers post surplus food. Nearby NGOs and volunteers are alerted, claim it, and collect it before it expires. An admin dashboard tracks how much food was rescued versus wasted.

> “Good Food Should Be Eaten, Not Wasted”


## Deployed Project 

Access it through the url <a   https://d35fd1qxbttd31.cloudfront.net/   >


## Working / Signing in

Sign in as `cchauhan_be24@thapar.edu` "zxcvbnmL7" (provider), `riya.connect56@gmail.com` "Wwwwwwww8" (receiver), or 'chhayaa799@gmail.com' "cloudCC999" (admin). Notifications are sent via email to the registered emails.
One can also register themselves as Provider or Receiver.

Receiver can claim listed food.
Provider can post surplus food for the receivers to claim it.
Admin can monitor all the pickups and profiles.

## Run it locally

Requires Python 3.9+. Uses SQLite and a local image folder; auth is a demo sign-in with no password.

```bash
python3 backend/local_server.py --seed
# open http://localhost:8000
```


## How matching works

- **Browse** (`GET /listings`) returns available, unexpired food within the radius (default 10 km, max 100).
- **Reachability:** food is hidden if the straight-line travel time at 25 km/h is longer than the time left before it expires.
- **Ranking:** lowest `hours_left + 0.1 × distance_km` first, so urgent and close food comes first.
- **Alerts:** when a listing is created, receivers within `NOTIFY_RADIUS_KM` (default 10, max 50 per listing) get a notification.
- **No double booking:** claiming runs `UPDATE ... WHERE status='available'`, so only one receiver can win; the other gets `409`.

Status flow: listing `available → booked → completed` (or `expired`); request `confirmed → collected → completed` (or `cancelled`, which returns food to the pool). A provider can complete a handover directly from `confirmed`.

## API summary

All routes need a Cognito ID token (`Authorization: Bearer ...`) except `/health`.

| Method & path | Who | Purpose |
|---|---|---|
| `GET /me`, `POST /me` | any | read / save profile (name, phone, address, location) |
| `POST /uploads/presign` | any | get a presigned S3 PUT URL for a JPEG/PNG/WebP |
| `POST /listings`, `GET /listings/mine` | provider | add surplus food, view own listings and who claimed them |
| `GET /listings?lat&lng&radius_km` | any | nearby available food, ranked |
| `POST /listings/{id}/request` | receiver | claim food, returns pickup code and instructions |
| `GET /requests/mine` | receiver | own requests |
| `POST /requests/{id}/collect` | receiver | mark as collected |
| `POST /requests/{id}/complete` | provider / receiver | confirm handover, closes the listing |
| `POST /requests/{id}/cancel` | provider / receiver | cancel a pending pickup |
| `GET /admin/stats`, `/admin/listings`, `/admin/users` | admin group | analytics and management |

## Design decisions and limits

- **Roles:** users choose provider or receiver at sign-up. Admin comes only from the Cognito `admin` group, never from a user-editable attribute.
- **Notifications by email:** each user's email is subscribed to one SNS topic with a per-user filter. SNS sends a confirmation email on first subscribe; alerts flow after the user confirms.
- **Timestamps** are stored as UTC ISO text so the same schema runs on PostgreSQL and SQLite.
- **Cost notes (approximate, check current AWS pricing):** the RDS instance (`db.t4g.micro`) and the SNS VPC endpoint (about two interface endpoints) are the always-on costs. Everything else is pay-per-use.
- **Hardening before real production use:** store the DB password in Secrets Manager (needs a VPC endpoint for it) and add RDS Proxy for connection pooling. Also restrict API CORS origins, add AWS WAF, and use a custom domain. Image size (5 MB) is enforced in the browser; add S3-side limits if you need strict enforcement.
- **Quantity** means servings/portions; the admin "rescued" and "wasted" figures are sums of that number.
