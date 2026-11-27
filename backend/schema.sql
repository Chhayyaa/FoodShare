CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  email TEXT NOT NULL,
  name TEXT NOT NULL,
  role TEXT NOT NULL,
  phone TEXT,
  address TEXT,
  latitude DOUBLE PRECISION,
  longitude DOUBLE PRECISION,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS listings (
  id TEXT PRIMARY KEY,
  provider_id TEXT NOT NULL REFERENCES users(id),
  title TEXT NOT NULL,
  food_type TEXT NOT NULL,
  description TEXT,
  quantity INTEGER NOT NULL,
  expiry_time TEXT NOT NULL,
  address TEXT,
  latitude DOUBLE PRECISION NOT NULL,
  longitude DOUBLE PRECISION NOT NULL,
  image_key TEXT,
  status TEXT NOT NULL DEFAULT 'available',
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_listings_status_expiry ON listings(status, expiry_time);
CREATE INDEX IF NOT EXISTS idx_listings_provider ON listings(provider_id);

CREATE TABLE IF NOT EXISTS requests (
  id TEXT PRIMARY KEY,
  listing_id TEXT NOT NULL REFERENCES listings(id),
  receiver_id TEXT NOT NULL REFERENCES users(id),
  status TEXT NOT NULL DEFAULT 'confirmed',
  pickup_note TEXT,
  created_at TEXT NOT NULL,
  collected_at TEXT,
  completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_requests_listing ON requests(listing_id);
CREATE INDEX IF NOT EXISTS idx_requests_receiver ON requests(receiver_id);
