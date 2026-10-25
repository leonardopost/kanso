-- A card may be a `runner_up`: a card of a turn that ran several, which cleared the keep
-- floor against the best its turn began from while another card of the same turn scored
-- higher and was kept.
--
-- Up to 0.19.0 such a card was written `discard`, and a discard says the change did not
-- beat the best. This one did, against the best it was judged by, and lost only to a
-- sibling it was never compared with; written as a discard, the condition it tested read to
-- the proposer -- in its recent cards and as its tag's best status -- as a corner searched
-- and found wanting. A card written before this migration keeps the status it was written
-- with: which of a turn's discards cleared the floor was never recorded, and nothing here
-- can recover it.
--
-- `status` carries a CHECK, which ALTER TABLE cannot widen, so the table is rebuilt as
-- 0005 rebuilt it: the same columns, `digest` and `measured_sha` included, the same
-- defaults, the same indexes, one more admissible status, and every row carried across.
CREATE TABLE cards_status_widened (
    card_id      INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT    NOT NULL REFERENCES runs (run_id),
    hyp_id       TEXT    NOT NULL REFERENCES hypotheses (hyp_id),
    seq          INTEGER NOT NULL,
    lane         TEXT    NOT NULL,
    strategy_sha TEXT    NOT NULL REFERENCES blobs (sha),
    status       TEXT    NOT NULL
                 CHECK (status IN ('keep', 'discard', 'crash', 'redundant', 'runner_up')),
    metric       REAL    NOT NULL,
    metric_se    REAL,
    n_trials     INTEGER NOT NULL,
    n_trades     INTEGER NOT NULL,
    wall_s       REAL    NOT NULL,
    peak_mem_gb  REAL,
    aligned      INTEGER NOT NULL DEFAULT 0,
    gate_results TEXT    NOT NULL DEFAULT '[]',
    crash_tail   TEXT,
    venue_model  TEXT    NOT NULL DEFAULT '{}',
    description  TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL,
    tags         TEXT    NOT NULL DEFAULT '[]',
    digest       TEXT,
    measured_sha TEXT    REFERENCES blobs (sha),
    UNIQUE (run_id, seq)
);
INSERT INTO cards_status_widened (
    card_id, run_id, hyp_id, seq, lane, strategy_sha, status, metric, metric_se, n_trials,
    n_trades, wall_s, peak_mem_gb, aligned, gate_results, crash_tail, venue_model,
    description, created_at, tags, digest, measured_sha
)
SELECT
    card_id, run_id, hyp_id, seq, lane, strategy_sha, status, metric, metric_se, n_trials,
    n_trades, wall_s, peak_mem_gb, aligned, gate_results, crash_tail, venue_model,
    description, created_at, tags, digest, measured_sha
FROM cards;
DROP TABLE cards;
ALTER TABLE cards_status_widened RENAME TO cards;
CREATE INDEX cards_hyp ON cards (hyp_id, created_at);
CREATE INDEX cards_run ON cards (run_id, created_at);
CREATE INDEX cards_strategy_sha ON cards (strategy_sha);
