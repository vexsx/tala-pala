-- Hamrah Gold (pwa.hamrahgold.com) becomes the PRIMARY 18k source: a 24/7
-- online trading platform whose public pre-login ticker quotes around the
-- clock. Milli Gold stays as first fallback (priority 5), TGJU after it.
INSERT INTO data_providers (code, name, base_url, category, priority, enabled)
VALUES ('hamrahgold', 'Hamrah Gold (pwa.hamrahgold.com, 24/7)',
        'https://pwa.hamrahgold.com/', 'iran_gold', 1, TRUE)
ON CONFLICT (code) DO UPDATE SET priority = 1, enabled = TRUE, updated_at = now();
