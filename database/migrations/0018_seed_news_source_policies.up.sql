-- Addendum 19: seed the policy rows migration 0017 created but never filled.
--
-- app/news/registry.py gates collection with an INNER JOIN from news_sources
-- to news_source_policies, so a source with no policy row can never be polled
-- however its own `enabled`/`policy_status` columns are set. 0016 seeded
-- news_sources; 0017 added the policy table; nothing populated it. The result
-- was a silent no-op: enabling NEWS_COLLECTION_ENABLED ran the job, and the
-- job skipped every source with "not approved or not enabled".
--
-- Fails closed by design: only fed_press is approved here. GDELT stays
-- exploratory (its access pattern is fine but its value to this application is
-- unproven), and OFAC has no news_sources row yet, so neither can be polled by
-- accident.

INSERT INTO news_source_policies (
    source_code, access_method, auth_type, approval_state, user_agent_policy,
    backfill_allowed, store_full_body, attribution_required,
    rate_limit_per_day, min_interval_seconds, policy_note, reviewed_at, reviewed_by
)
SELECT 'fed_press', 'rss', 'none', 'approved', 'honest',
       TRUE,   -- the feed itself is a public archive
       FALSE,  -- headline + summary only; the full release stays at the source
       TRUE,
       NULL,   -- federalreserve.gov publishes no documented request cap
       900,
       'Official Federal Reserve press RSS. Public, unauthenticated, no '
       || 'robots restriction on /feeds/. Honest User-Agent, 15-minute '
       || 'courtesy interval, metadata + excerpt stored with attribution.',
       now(), 'migration-0018'
WHERE EXISTS (SELECT 1 FROM news_sources WHERE code = 'fed_press')
ON CONFLICT (source_code) DO UPDATE
    SET approval_state = EXCLUDED.approval_state,
        policy_note    = EXCLUDED.policy_note,
        reviewed_at    = EXCLUDED.reviewed_at,
        reviewed_by    = EXCLUDED.reviewed_by,
        updated_at     = now();

-- GDELT: recorded as reviewed but NOT approved, so the decision is auditable
-- rather than implied by absence.
INSERT INTO news_source_policies (
    source_code, access_method, auth_type, approval_state, user_agent_policy,
    backfill_allowed, store_full_body, attribution_required,
    min_interval_seconds, policy_note, reviewed_at, reviewed_by
)
SELECT 'gdelt', 'api', 'none', 'experimental', 'honest',
       FALSE, FALSE, TRUE, 5,
       'GDELT DOC 2.0 is open and unauthenticated, but its contribution to '
       || 'Iranian gold forecasting is unmeasured. Kept experimental until an '
       || 'event study justifies polling it.',
       now(), 'migration-0018'
WHERE EXISTS (SELECT 1 FROM news_sources WHERE code = 'gdelt')
ON CONFLICT (source_code) DO NOTHING;
