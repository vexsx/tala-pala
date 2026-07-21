-- 0011: Milli Gold becomes the PRIMARY source for IR_GOLD_18K.
-- milli.gold is a 24-hour online gold trading platform, so its quote updates
-- around the clock (TGJU's bazaar ticker stops evenings/Fridays) and reflects
-- retail platform pricing. TGJU remains the fallback for 18k and the primary
-- for USD_IRT / IR_COIN_EMAMI / history backfill.

UPDATE data_providers
SET priority = 5, enabled = TRUE, updated_at = now()
WHERE code = 'milligold';
