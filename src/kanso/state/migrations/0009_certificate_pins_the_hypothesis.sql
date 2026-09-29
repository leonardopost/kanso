-- A certificate is of the hypothesis as the card's run pinned it, so the pin is part of the key.
--
-- 0001 keyed a certificate on the subject bytes, the plan version and the engine version,
-- which is what immutability forbids repeating: the same code, judged by the same plan, on
-- the same engine. It left out the hypothesis file the run was pinned to. An operator who
-- re-pins a file with `hyp add` -- new costs, a new window, a new universe -- and researches
-- again then finds the same seed bytes refused as "already certified", when what was
-- certified was those bytes under the file as it was. Measured on a posting hypothesis
-- re-pinned with a maker fee: the seed stayed uncertifiable for as long as the loop kept it,
-- and only evolved bytes ever reached a certificate.
--
-- SQLite cannot widen a primary key in place, so the table is rebuilt: the same columns with
-- the same defaults, every row carried across, and `hypothesis_sha` a key column. A row
-- written before this migration carries '' for it -- a certificate whose pin is not known --
-- and the refusal that keeps a certificate immutable reads '' as matching any pin, so no
-- certificate already recorded loses its immutability. Every certificate written from here
-- on names the pin its run recorded.
CREATE TABLE certificates_pinned (
    hyp_id           TEXT    NOT NULL REFERENCES hypotheses (hyp_id),
    hypothesis_sha   TEXT    NOT NULL DEFAULT '',
    strategy_sha     TEXT    NOT NULL,
    plan_version     INTEGER NOT NULL,
    nautilus_version TEXT    NOT NULL,
    venue_model      TEXT    NOT NULL DEFAULT '{}',
    snapshot_id      TEXT    NOT NULL,
    criteria_version TEXT    NOT NULL,
    construct        TEXT    NOT NULL DEFAULT '{}',
    objective        TEXT    NOT NULL DEFAULT '{}',
    gates            TEXT    NOT NULL DEFAULT '[]',
    n_trials         INTEGER NOT NULL,
    verdict          TEXT    NOT NULL CHECK (verdict IN ('pass', 'fail')),
    path             TEXT    NOT NULL,
    created_at       TEXT    NOT NULL,
    PRIMARY KEY (hyp_id, hypothesis_sha, strategy_sha, plan_version, nautilus_version)
);
INSERT INTO certificates_pinned (
    hyp_id, hypothesis_sha, strategy_sha, plan_version, nautilus_version, venue_model,
    snapshot_id, criteria_version, construct, objective, gates, n_trials, verdict, path,
    created_at
)
SELECT
    hyp_id, '', strategy_sha, plan_version, nautilus_version, venue_model,
    snapshot_id, criteria_version, construct, objective, gates, n_trials, verdict, path,
    created_at
FROM certificates;
DROP TABLE certificates;
ALTER TABLE certificates_pinned RENAME TO certificates;
CREATE INDEX certificates_hyp ON certificates (hyp_id, created_at);
