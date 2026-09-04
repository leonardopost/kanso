-- Initial schema: every table the workspace state store owns.
--
-- Applied by StateStore.migrate() inside one transaction that ends by stamping
-- PRAGMA user_version = 1. JSON-valued columns hold a serialised object or list;
-- they are opaque to SQL and parsed by the module that wrote them.

-- Content-addressed bytes of the files the research loop versions: strategy.py,
-- hypothesis.yaml, program.md. The sha256 hex digest of the bytes is the key, so a
-- repeated store is a no-op and any unique prefix names a row.
CREATE TABLE blobs (
    sha        TEXT    NOT NULL PRIMARY KEY,
    data       BLOB    NOT NULL,
    size       INTEGER NOT NULL,
    created_at TEXT    NOT NULL
);

-- The hypothesis registry: lifecycle status, the pins hyp add and classify record,
-- and the best keep seen across every run of the hypothesis.
CREATE TABLE hypotheses (
    hyp_id         TEXT NOT NULL PRIMARY KEY,
    status         TEXT NOT NULL CHECK (status IN (
                       'draft', 'classified', 'researching',
                       'candidate', 'certified', 'failed', 'retired')),
    hypothesis_sha TEXT,
    pins           TEXT NOT NULL DEFAULT '{}',
    construct_id   TEXT,
    objective_id   TEXT,
    best_sha       TEXT,
    best_metric    REAL,
    best_run_id    TEXT,
    consecutive_cert_failures INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX hypotheses_status ON hypotheses (status);

-- One research run: its lane, its lane directory and everything it is pinned to.
-- A run with no ended_at is active, and a hypothesis has at most one of those.
CREATE TABLE runs (
    run_id               TEXT    NOT NULL PRIMARY KEY,
    hyp_id               TEXT    NOT NULL REFERENCES hypotheses (hyp_id),
    tag                  TEXT    NOT NULL,
    lane                 TEXT    NOT NULL,
    dir                  TEXT    NOT NULL,
    base_sha             TEXT,
    hypothesis_sha       TEXT    NOT NULL,
    program_sha          TEXT    NOT NULL,
    snapshot_id          TEXT    NOT NULL,
    criteria_version     TEXT    NOT NULL,
    host_version         INTEGER,
    card_budget_s        REAL    NOT NULL,
    baseline_wall_s      REAL    NOT NULL,
    baseline_peak_mem_gb REAL    NOT NULL,
    best_sha             TEXT,
    best_metric          REAL,
    started_at           TEXT    NOT NULL,
    ended_at             TEXT
);
CREATE INDEX runs_hyp ON runs (hyp_id, started_at);
CREATE UNIQUE INDEX runs_one_active_per_hyp ON runs (hyp_id) WHERE ended_at IS NULL;

-- One card: the experiment record results.tsv is rendered from, plus the state the
-- file does not carry. strategy_sha names the blob of the strategy.py that ran.
CREATE TABLE cards (
    card_id      INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT    NOT NULL REFERENCES runs (run_id),
    hyp_id       TEXT    NOT NULL REFERENCES hypotheses (hyp_id),
    seq          INTEGER NOT NULL,
    lane         TEXT    NOT NULL,
    strategy_sha TEXT    NOT NULL REFERENCES blobs (sha),
    status       TEXT    NOT NULL CHECK (status IN ('keep', 'discard', 'crash')),
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
    UNIQUE (run_id, seq)
);
CREATE INDEX cards_hyp ON cards (hyp_id, created_at);
CREATE INDEX cards_run ON cards (run_id, created_at);
CREATE INDEX cards_strategy_sha ON cards (strategy_sha);

-- Certification plans, one per hypothesis and plan version.
CREATE TABLE plans (
    hyp_id       TEXT    NOT NULL REFERENCES hypotheses (hyp_id),
    plan_version INTEGER NOT NULL,
    planned_at   TEXT    NOT NULL,
    planned_by   TEXT    NOT NULL,
    inputs       TEXT    NOT NULL DEFAULT '{}',
    gates        TEXT    NOT NULL DEFAULT '[]',
    excluded     TEXT    NOT NULL DEFAULT '[]',
    PRIMARY KEY (hyp_id, plan_version)
);

-- Certificates are immutable. The key is what cert run refuses to repeat: the same
-- subject under the same plan and the same engine.
CREATE TABLE certificates (
    hyp_id           TEXT    NOT NULL REFERENCES hypotheses (hyp_id),
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
    PRIMARY KEY (hyp_id, strategy_sha, plan_version, nautilus_version)
);
CREATE INDEX certificates_hyp ON certificates (hyp_id, created_at);

CREATE TABLE strategies (
    strategy_id TEXT NOT NULL PRIMARY KEY,
    created_at  TEXT NOT NULL
);

-- One composed version of a strategy and where it is deployed. At most one version
-- of a strategy occupies a stage.
CREATE TABLE strategy_versions (
    strategy_id TEXT    NOT NULL REFERENCES strategies (strategy_id),
    version     INTEGER NOT NULL,
    state       TEXT    NOT NULL CHECK (state IN (
                    'composed', 'paper', 'promotable', 'live', 'retired')),
    stage       TEXT    CHECK (stage IS NULL OR stage IN ('paper', 'live')),
    sleeve      TEXT    NOT NULL DEFAULT '{}',
    attached    TEXT    NOT NULL DEFAULT '[]',
    config      TEXT    NOT NULL DEFAULT '{}',
    pins        TEXT    NOT NULL DEFAULT '{}',
    expectation TEXT,
    capital     REAL,
    joined_at   TEXT,
    created_at  TEXT    NOT NULL,
    PRIMARY KEY (strategy_id, version)
);
CREATE UNIQUE INDEX strategy_versions_one_per_stage
    ON strategy_versions (strategy_id, stage) WHERE stage IS NOT NULL;

-- Named operator approvals. Real capital moves only against a row here.
CREATE TABLE approvals (
    approval_id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    action      TEXT    NOT NULL,
    subject     TEXT    NOT NULL,
    approved_by TEXT    NOT NULL,
    detail      TEXT    NOT NULL DEFAULT '{}',
    created_at  TEXT    NOT NULL
);
CREATE INDEX approvals_subject ON approvals (subject, created_at);

-- The escalation inbox. inbox ack sets acked_at; it is never an approval.
CREATE TABLE escalations (
    escalation_id TEXT NOT NULL PRIMARY KEY,
    kind          TEXT NOT NULL,
    subject       TEXT NOT NULL,
    summary       TEXT NOT NULL,
    actions       TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    acked_at      TEXT
);
CREATE INDEX escalations_unread ON escalations (acked_at, created_at);

-- The LLM spend ledger: one row per model call.
CREATE TABLE spend (
    spend_id   INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    ts         TEXT    NOT NULL,
    lane       TEXT,
    task_class TEXT    NOT NULL,
    model      TEXT    NOT NULL,
    tokens_in  INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost       REAL    NOT NULL DEFAULT 0.0,
    cache_hit  INTEGER
);
CREATE INDEX spend_ts ON spend (ts);

-- Data snapshots: the dataset checksums and the resolved-instrument checksum a run
-- is pinned to. reproducible is false for a snapshot holding a vendor-adjusted set.
CREATE TABLE snapshots (
    snapshot_id          TEXT    NOT NULL PRIMARY KEY,
    datasets             TEXT    NOT NULL DEFAULT '[]',
    instruments_checksum TEXT    NOT NULL,
    reproducible         INTEGER NOT NULL DEFAULT 1,
    path                 TEXT,
    created_at           TEXT    NOT NULL
);

-- Node, engine, paper and live sessions. clock_ts is the stage's session clock: the
-- replay position a restart resumes from.
CREATE TABLE sessions (
    session_id  TEXT NOT NULL PRIMARY KEY,
    mode        TEXT NOT NULL CHECK (mode IN ('node', 'engine', 'paper', 'live')),
    target      TEXT NOT NULL,
    instruments TEXT NOT NULL DEFAULT '[]',
    from_ts     TEXT,
    to_ts       TEXT,
    speed       REAL,
    exec_id     TEXT,
    clock_ts    TEXT,
    started_at  TEXT NOT NULL,
    ended_at    TEXT
);
CREATE INDEX sessions_mode ON sessions (mode, started_at);

-- The append-only event log every command writes to.
CREATE TABLE events (
    event_id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    ts       TEXT    NOT NULL,
    kind     TEXT    NOT NULL,
    subject  TEXT    NOT NULL DEFAULT '',
    detail   TEXT    NOT NULL DEFAULT '{}'
);
CREATE INDEX events_kind ON events (kind, event_id);
CREATE INDEX events_subject ON events (subject, event_id);
