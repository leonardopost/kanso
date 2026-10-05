-- A screen's results: one row per measurement of one screen's bytes, on one snapshot, under
-- one measure library.
--
-- The key is what makes a result immutable and a repeat free: the same bytes measured on the
-- same snapshot by the same library are the same measurement, and `screen run` returns the
-- stored row rather than measuring again. The bytes are a blob, as a hypothesis pin's are, so
-- a screen survives an edit of its file. `window_start`, `window_end` and `instruments` are
-- columns rather than parts of `result` because the embargo reads them: a hypothesis may not
-- certify on data a screen read for one of its instruments. `result` is the whole record,
-- the object `kanso screen show` prints and the file beside the screen renders.
CREATE TABLE screen_results (
    screen_sha     TEXT    NOT NULL REFERENCES blobs (sha),
    snapshot_id    TEXT    NOT NULL,
    screen_version TEXT    NOT NULL,
    screen_id      TEXT    NOT NULL,
    hyp_id         TEXT,
    window_start   TEXT    NOT NULL,
    window_end     TEXT    NOT NULL,
    instruments    TEXT    NOT NULL DEFAULT '[]',
    result         TEXT    NOT NULL DEFAULT '{}',
    created_at     TEXT    NOT NULL,
    PRIMARY KEY (screen_sha, snapshot_id, screen_version)
);
CREATE INDEX screen_results_by_screen ON screen_results (screen_id, created_at);
