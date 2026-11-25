CREATE TABLE IF NOT EXISTS daily_metrics (
    day DATE NOT NULL,
    metric TEXT NOT NULL,
    dimension TEXT NOT NULL DEFAULT '',
    value DOUBLE PRECISION,
    computed_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (day, metric, dimension)
);
