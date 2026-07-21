-- Tehran-exchange gold investment funds (Addendum 7): provider registry row.
-- Data comes through BrsApi's TSETMC mirror (tsetmc.com is geo-blocked
-- outside Iran); the provider stays dormant until BRSAPI_KEY is configured.
INSERT INTO data_providers (code, name, base_url, category, priority, enabled)
VALUES ('tse_funds', 'TSE gold funds (via BrsApi TSETMC mirror)',
        'https://Api.BrsApi.ir/Tsetmc/', 'iran_fund', 40, TRUE)
ON CONFLICT (code) DO NOTHING;
