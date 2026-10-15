-- A card records what its trades did, beside the one number it is scored on.
--
-- A card's row held its metric, its error and its trade count, and nothing else about the
-- trades: no record anywhere held a hit rate, a hold, a fill or maker share, or the split of
-- what the trades earned before costs from what the costs took. The proposer, `results.tsv`
-- and `research card` were all read from this row, so none of them could say whether a card
-- that scored better did so by trading better or by trading less. The column holds the
-- card's digest as JSON; a row written before this migration holds NULL, which reads back as
-- a card that recorded none, which is what it was.
ALTER TABLE cards ADD COLUMN digest TEXT;
