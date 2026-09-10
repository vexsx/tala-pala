"""Tehran equity daily bars: the raw series, and the adjustment over it.

Two modules, split along the line that matters:

* :mod:`app.equities.adjust` is the ENGINE — parse a TSETMC payload, detect
  corporate actions, chain the back-adjustment factors, and validate the
  result.  It is pure: no database, no clock, no network, so the numbers it
  produces can be checked against a file.
* :mod:`app.equities.ingest` is everything that touches the database — the
  roster, the per-symbol isolation, the idempotent writes, and the promotion
  of a verdict.

The split exists because the engine is the part that can be wrong in a way
nobody notices.  A missed corporate action does not raise; it silently turns a
capital increase into a -42% day, and every return, ratio and score computed
downstream inherits it.  So the arithmetic is testable without infrastructure,
and :mod:`app.equities.ingest` refuses to promote a series the engine could not
validate.
"""
