-- Canonical Health Data Hub v1 warehouse schema.
-- Exactly five core tables support the S01 warehouse foundation.

CREATE TABLE IF NOT EXISTS sleep_nights (
    source VARCHAR NOT NULL,
    sleep_date DATE NOT NULL,
    bedtime_utc TIMESTAMP,
    waketime_utc TIMESTAMP,
    total_sleep_min INTEGER,
    rem_min INTEGER,
    deep_min INTEGER,
    light_min INTEGER,
    awake_min INTEGER,
    hrv_avg_ms DOUBLE,
    rhr_avg_bpm INTEGER,
    body_temp_dev_c DOUBLE,
    sleep_score INTEGER,
    ingested_at_utc TIMESTAMP NOT NULL,
    PRIMARY KEY (source, sleep_date)
);

CREATE TABLE IF NOT EXISTS mood_entries (
    log_id UUID PRIMARY KEY,
    logged_at_utc TIMESTAMP NOT NULL,
    mood_date DATE NOT NULL,
    feeling INTEGER NOT NULL,
    energy INTEGER,
    notes TEXT,
    context_chips VARCHAR[],
    source VARCHAR,
    supersedes_log_id UUID
);

CREATE TABLE IF NOT EXISTS mood_current (
    mood_date DATE PRIMARY KEY,
    log_id UUID NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_features (
    feature_date DATE PRIMARY KEY,
    total_sleep_min INTEGER,
    hrv_z DOUBLE,
    deep_sleep_pct DOUBLE,
    prior_day_feeling INTEGER,
    hrv_avg_ms DOUBLE,
    hrv_z_method VARCHAR,
    feature_version VARCHAR,
    prior_day_feeling_imputed BOOLEAN DEFAULT FALSE,
    sleep_source_count INTEGER,
    sleep_merge_warning VARCHAR,
    computed_at_utc TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS sleep_merge_diagnostics (
    sleep_date DATE PRIMARY KEY,
    oura_present BOOLEAN,
    eight_present BOOLEAN,
    total_sleep_delta_min INTEGER,
    hrv_merge_method VARCHAR,
    stage_source VARCHAR,
    warning VARCHAR,
    computed_at_utc TIMESTAMP NOT NULL
);
