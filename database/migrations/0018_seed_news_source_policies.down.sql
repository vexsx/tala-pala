-- Removes only the rows 0018 seeded; the table itself belongs to 0017.
DELETE FROM news_source_policies WHERE reviewed_by = 'migration-0018';
