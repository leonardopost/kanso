-- A run records the card its card budget was struck from.
--
-- A run's card budget was three times its baseline's wall, struck once at `research begin`,
-- and a hypothesis's first run starts from a stub that trades nothing, so a card that traded
-- spent the headroom on its fills and could be killed for its clock before it was scored. The
-- budget is now struck from the run's first card that trades — the baseline, when it does —
-- and the column names that card's `strategy.py`; NULL while no card of the run has traded,
-- when the budget is still three times the baseline's wall. A run recorded before this
-- migration whose baseline traded is given its base, from which its budget was struck; one
-- whose baseline traded nothing holds NULL, so its next card that trades strikes its budget,
-- as one in a run begun now would.
ALTER TABLE runs ADD COLUMN budget_sha TEXT REFERENCES blobs (sha);
UPDATE runs SET budget_sha = base_sha
WHERE EXISTS (
    SELECT 1 FROM cards
    WHERE cards.run_id = runs.run_id
      AND cards.strategy_sha = runs.base_sha
      AND cards.description = 'baseline'
      AND cards.n_trades > 0
);
