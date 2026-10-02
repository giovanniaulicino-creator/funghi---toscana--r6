CREATE TABLE IF NOT EXISTS station_catalog (
  code TEXT PRIMARY KEY,
  name TEXT, municipality TEXT, province TEXT, zone TEXT,
  altitude_m REAL, lat REAL NOT NULL, lon REAL NOT NULL,
  registry_source TEXT, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS component_cache (
  code TEXT NOT NULL,
  component TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  source TEXT NOT NULL,
  observed_at TEXT,
  quality TEXT NOT NULL,
  cycle_key TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(code, component)
);

CREATE TABLE IF NOT EXISTS daily_history (
  code TEXT NOT NULL,
  day TEXT NOT NULL,
  rain_mm REAL,
  temperature_min_c REAL,
  temperature_max_c REAL,
  temperature_mean_c REAL,
  humidity_mean_pct REAL,
  wind_mean_ms REAL,
  vpd_max_kpa REAL,
  et0_mm REAL,
  source TEXT NOT NULL,
  quality TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(code, day)
);
CREATE INDEX IF NOT EXISTS idx_daily_history_code_day ON daily_history(code, day DESC);

CREATE TABLE IF NOT EXISTS source_health (
  source TEXT PRIMARY KEY,
  reachable INTEGER NOT NULL,
  acquired INTEGER NOT NULL DEFAULT 0,
  last_success_at TEXT,
  last_error TEXT,
  reference_at TEXT,
  details_json TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS generation_runs (
  generation TEXT PRIMARY KEY,
  generated_at TEXT NOT NULL,
  target_day TEXT NOT NULL,
  status TEXT NOT NULL,
  structural_total INTEGER NOT NULL,
  scientific_total INTEGER NOT NULL,
  manifest_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS generation_station (
  generation TEXT NOT NULL,
  code TEXT NOT NULL,
  complete INTEGER NOT NULL,
  PRIMARY KEY(generation, code)
);
CREATE INDEX IF NOT EXISTS idx_generation_station_generation ON generation_station(generation);

CREATE TABLE IF NOT EXISTS generation_snapshot (
  generation TEXT PRIMARY KEY,
  sha256 TEXT NOT NULL,
  byte_length INTEGER NOT NULL,
  chunk_count INTEGER NOT NULL,
  verified INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS generation_snapshot_chunk (
  generation TEXT NOT NULL,
  chunk_index INTEGER NOT NULL,
  chunk_text TEXT NOT NULL,
  raw_bytes INTEGER NOT NULL,
  PRIMARY KEY(generation, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_generation_snapshot_chunk_generation
  ON generation_snapshot_chunk(generation, chunk_index);

CREATE TABLE IF NOT EXISTS system_state (
  key TEXT PRIMARY KEY,
  value TEXT,
  updated_at TEXT NOT NULL
);
