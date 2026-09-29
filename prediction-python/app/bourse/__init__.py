"""The Tehran market as a whole: indices, market value, money flow, snapshots.

Fed files, never fetches: cdn.tsetmc.com refuses TCP 443 from the production
host, so :mod:`scripts.tsetmc_fetch` downloads these payloads where TSETMC
answers and posts their paths to ``/internal/bourse/ingest``.  Migration 0029's
header records what was measured about each payload before any of this was
written.
"""
