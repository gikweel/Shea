CREATE TABLE IF NOT EXISTS recovery_checkpoints (
    task_id             TEXT PRIMARY KEY REFERENCES tasks(id),
    plan_id             TEXT,
    step_id             TEXT,
    state               TEXT NOT NULL,
    last_verified_step  INTEGER,
    metadata            TEXT NOT NULL DEFAULT '{}',
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);