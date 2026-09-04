-- The research queue: which hypotheses wait for a lane, and in what order.
--
-- Serving order is priority descending, then first in, so `queue_id` supplies the
-- arrival order and requeueing at a lower priority is a delete and an insert that
-- lands behind everything already in the band. A hypothesis appears at most once,
-- which is what makes "only failed and retired leave the queue" a statement about
-- one row rather than about a set.
CREATE TABLE queue (
    queue_id    INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    hyp_id      TEXT    NOT NULL UNIQUE REFERENCES hypotheses (hyp_id),
    priority    INTEGER NOT NULL DEFAULT 0,
    enqueued_at TEXT    NOT NULL
);
CREATE INDEX queue_order ON queue (priority DESC, queue_id);
