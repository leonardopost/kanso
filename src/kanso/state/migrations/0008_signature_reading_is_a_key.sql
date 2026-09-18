-- The reading a number was measured under is part of the key, not a column beside it.
--
-- 0007 made `measured_under` a term of the selection and left the primary key where it
-- was: (strategy_sha, hypothesis_sha, snapshot_id, criteria_version). So the same bytes
-- judged under two readings collide, and `record_signature`'s `INSERT OR REPLACE` keeps
-- whichever was written last. An operator who sets `[research] folds = 3`, runs cards and
-- sets it back to 4 then has no four-fold anchor for any strategy re-judged in between:
-- the rule that refuses a repeat looks for a row under its own reading, finds none, and
-- cards the third spelling of one idea again.
--
-- The same bytes over the same data measured twice under one reading are one fact. Under
-- two readings they are two facts, which is what 0007 measured rather than argued: the
-- demo hypothesis's same bytes earn 15.68944922049284 over four folds and
-- 16.110595594036468 over three. The book moves with the reading as well -- the capital
-- the harness starts with is in the digest, and it decides what a fill could be -- so a
-- row is one book and one number under one reading, and the two readings share nothing in
-- it worth keeping one of.
--
-- SQLite cannot widen a primary key in place, so the table is rebuilt: the same columns
-- with the same defaults, every row carried across, and `measured_under` a fifth key
-- column. It stays nullable, for the reading 0006 and 0007 state -- a row that carries no
-- reading is no anchor to anybody, and a NULL never equals the digest a card asks with --
-- and `record_signature` writes a digest on every row it writes, so nothing this package
-- writes can collide on one. The index the selection reads takes the reading as its fifth
-- column for the same reason the key does: every one of the five is asked for by `=`.
CREATE TABLE signatures_by_reading (
    strategy_sha     TEXT    NOT NULL REFERENCES blobs (sha),
    hyp_id           TEXT    NOT NULL REFERENCES hypotheses (hyp_id),
    hypothesis_sha   TEXT    NOT NULL,
    snapshot_id      TEXT    NOT NULL,
    criteria_version TEXT    NOT NULL,
    signature        TEXT    NOT NULL DEFAULT '{}',
    sessions         INTEGER NOT NULL,
    created_at       TEXT    NOT NULL,
    metric           REAL,
    measured_under   TEXT,
    PRIMARY KEY (strategy_sha, hypothesis_sha, snapshot_id, criteria_version, measured_under)
);
INSERT INTO signatures_by_reading (
    strategy_sha, hyp_id, hypothesis_sha, snapshot_id, criteria_version, signature,
    sessions, created_at, metric, measured_under
)
SELECT
    strategy_sha, hyp_id, hypothesis_sha, snapshot_id, criteria_version, signature,
    sessions, created_at, metric, measured_under
FROM signatures;
DROP TABLE signatures;
ALTER TABLE signatures_by_reading RENAME TO signatures;
CREATE INDEX signatures_pins ON signatures (
    hyp_id, hypothesis_sha, snapshot_id, criteria_version, measured_under
);
