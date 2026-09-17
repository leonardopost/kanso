-- The loop's memory: what each card tried, and what each judged run held.
--
-- `tags` is the proposer's own account of a card, as a JSON array of strings drawn from
-- the vocabulary `kanso.schemas.TAGS` fixes, so the cards under one run's pins can be
-- read back as a coverage table: which corners of the search have been visited, how
-- often, and what the best of each scored. Every card ever recorded carries the empty
-- array, which is what a card made before the proposer was asked said.
ALTER TABLE cards ADD COLUMN tags TEXT NOT NULL DEFAULT '[]';

-- One row per strategy judged under one set of pins: for each period end of the
-- research window, keyed by its UTC day, the sorted (instrument, sign) pairs the run
-- held at that end. A candidate whose signature matches a stored one on enough of their
-- shared days is redundant — its result is already known — and is refused without a
-- card, so a redundant miss has a row here and no row in `cards`. Keyed by the strategy
-- bytes and the run's pins, never by run: two runs under the same pins ask the same
-- question of the same data.
CREATE TABLE signatures (
    strategy_sha     TEXT    NOT NULL REFERENCES blobs (sha),
    hyp_id           TEXT    NOT NULL REFERENCES hypotheses (hyp_id),
    hypothesis_sha   TEXT    NOT NULL,
    snapshot_id      TEXT    NOT NULL,
    criteria_version TEXT    NOT NULL,
    signature        TEXT    NOT NULL DEFAULT '{}',
    sessions         INTEGER NOT NULL,
    created_at       TEXT    NOT NULL,
    PRIMARY KEY (strategy_sha, hypothesis_sha, snapshot_id, criteria_version)
);
CREATE INDEX signatures_pins ON signatures (hyp_id, hypothesis_sha, snapshot_id, criteria_version);
