-- A certificate records its window's net per trade, and the row says so too.
--
-- The certificates table is the index `cert show`, composition and the stall's repeat check
-- read a certificate back from, column by column, as the document it was written from. A
-- certificate now carries `net_per_trade` -- the certification run's mean net per trade, its
-- session-clustered error, its t and the sessions that closed a trade, which a book ranks its
-- members by -- and a row without it would read back a certificate that recorded none when its
-- file recorded one. The column holds the record as JSON; a row written before this migration
-- holds NULL, which reads back as a certificate that recorded none, which is what it was.
ALTER TABLE certificates ADD COLUMN net_per_trade TEXT;
