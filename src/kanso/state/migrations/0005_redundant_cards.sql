-- A card may be `redundant`: it ran, it was measured, and it held a book already judged.
--
-- Up to 0.8.1 such a candidate was refused after its backtest and left no row at all, so
-- the engine time was spent and the number it produced was thrown away with it. Four
-- things went with the card: the trial (certificates recorded n_trials of 7 and 8 against
-- about a thousand backtests actually run and selected over, so the deflated Sharpe was
-- computed on a search more than a hundred times too narrow), the coverage entry the proposer reads a
-- corner's emptiness from, the record of the idea for the runs after this one, and the
-- traceable link between the refusal and what it measured.
--
-- `status` carries a CHECK, which ALTER TABLE cannot widen, so the table is rebuilt: the
-- same columns, the same defaults, the same indexes, one more admissible status, and
-- every row carried across. No row is rewritten -- a card recorded before this migration
-- was refused before it could be a card, and nothing here can recover one.
CREATE TABLE cards_status_widened (
    card_id      INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT    NOT NULL REFERENCES runs (run_id),
    hyp_id       TEXT    NOT NULL REFERENCES hypotheses (hyp_id),
    seq          INTEGER NOT NULL,
    lane         TEXT    NOT NULL,
    strategy_sha TEXT    NOT NULL REFERENCES blobs (sha),
    status       TEXT    NOT NULL CHECK (status IN ('keep', 'discard', 'crash', 'redundant')),
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
    UNIQUE (run_id, seq)
);
INSERT INTO cards_status_widened (
    card_id, run_id, hyp_id, seq, lane, strategy_sha, status, metric, metric_se, n_trials,
    n_trades, wall_s, peak_mem_gb, aligned, gate_results, crash_tail, venue_model,
    description, created_at, tags
)
SELECT
    card_id, run_id, hyp_id, seq, lane, strategy_sha, status, metric, metric_se, n_trials,
    n_trades, wall_s, peak_mem_gb, aligned, gate_results, crash_tail, venue_model,
    description, created_at, tags
FROM cards;
DROP TABLE cards;
ALTER TABLE cards_status_widened RENAME TO cards;
CREATE INDEX cards_hyp ON cards (hyp_id, created_at);
CREATE INDEX cards_run ON cards (run_id, created_at);
CREATE INDEX cards_strategy_sha ON cards (strategy_sha);
