"""OFAC SDN list — snapshot diff, with interpretation kept out of it.

The Specially Designated Nationals list is published as one XML document with
no change feed: the only way to learn what changed is to keep the previous
document and compare.  So this collector stores each snapshot in
``news_raw_payloads`` and diffs the new one against the last stored body.  Two
consequences follow, and both are deliberate:

*A first run produces no changes.*  There is no historical archive of this list
anywhere in the system; the baseline is whatever we first stored.  Emitting
"every entry was added" on day one would manufacture thousands of events that
did not happen.

*An identical snapshot is not re-stored.*  The list is republished unchanged on
most days and it is tens of megabytes; ``news_collection_attempts`` already
evidences that the fetch happened, so a byte-identical body only updates the
attempt log.

Iran relevance comes from EXPLICIT versioned rules, never from a substring
search over a name.  Programs whose tag is Iran-specific (``IRAN*``, ``IFSR``,
``IRGC``) match directly; multi-country programs (``NPWMD``, ``SDGT``, ``FTO``)
match only when the record itself carries Iran evidence — a country, a
nationality or a remark.  The reason string that fired is stored on the row, so
a later reader can check the claim instead of trusting it, and a designation
with no Iran connection is stored with the reason it was NOT flagged.

*Removal is not relief.*  An entry leaves the SDN list on delisting, on
correction, on re-designation under another authority, and on the death or
dissolution of the target.  This collector records that the entry left the
list.  Every reading beyond that fact is marked ``hypothesis_only`` and left to
a classifier that can weigh it against other evidence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence

from ...config import Settings
from ...db import utcnow
from ..safefetch import safe_get
from . import (
    OUTCOME_EMPTY,
    OUTCOME_OK,
    CollectedArticle,
    base_result,
    content_type_of,
    latest_raw_payload,
    load_source,
    mark_polled,
    parse_xml,
    poll_gate,
    record_attempt,
    record_failure,
    sha256_text,
    store_articles,
    store_raw_payload,
)

SOURCE_CODE = "ofac_sdn"
PARSER_VERSION = "ofac_sdn_xml_v1"
IRAN_RULES_VERSION = "ofac_iran_relevance_v1"

DEFAULT_LIST_URL = "https://www.treasury.gov/ofac/downloads/sdn.xml"
ALLOW_HOSTS = (
    "www.treasury.gov",
    "treasury.gov",
    "sanctionslist.ofac.treas.gov",
    "sanctionslistservice.ofac.treas.gov",
)

# The published list is tens of megabytes.  A truncated snapshot cannot be
# diffed — a missing tail would read as thousands of removals — so the cap sits
# above the list's size and truncation is treated as a failed collection.
MAX_FETCH_BYTES = 64_000_000
MAX_STORED_BODY_CHARS = 64_000_000
# A floor rather than a cap: the shared HTTP timeout is sized for API calls and
# a tens-of-megabytes download would fail on it every time.
MIN_TIMEOUT_SECONDS = 120.0

CHANGE_ADDED = "added"
CHANGE_REMOVED = "removed"
CHANGE_MODIFIED = "modified"
_CHANGE_ORDER = {CHANGE_ADDED: 0, CHANGE_MODIFIED: 1, CHANGE_REMOVED: 2}

# Fields compared to decide "modified".  List-valued fields are sorted at parse
# time so that a reordering in the published XML — which happens — cannot read
# as a change to the designation.
COMPARED_FIELDS: tuple[str, ...] = (
    "name",
    "sdn_type",
    "title",
    "remarks",
    "programs",
    "akas",
    "addresses",
    "ids",
    "nationalities",
    "citizenships",
    "dates_of_birth",
    "places_of_birth",
)

# Program tags that are Iran-specific by construction.  The prefix rule covers
# the executive-order variants (IRAN-EO13846, IRAN-HR, IRAN-TRA, ...) without
# enumerating a list that Treasury extends.
IRAN_PROGRAM_PREFIXES = ("IRAN",)
IRAN_PROGRAM_EXACT = frozenset({"IFSR", "IRGC"})

# Multi-country programs.  A designation under one of these is Iran-related
# only if the record itself says so; NPWMD alone describes DPRK, Syrian and
# Russian networks just as often.
CONDITIONAL_PROGRAMS = frozenset({"NPWMD", "SDGT", "FTO", "SDT"})

# Word-boundary matching only: a substring search flags "Tirana" and every
# surname containing "iran".
_IRAN_TERMS = re.compile(r"\b(iran|iranian|tehran)\b", re.IGNORECASE)

REMOVAL_NOTE = (
    "an entry leaves the SDN list on delisting, correction, re-designation "
    "under another authority, or the death/dissolution of the target; this row "
    "records only that it left the list"
)
CHANGE_NOTES = {
    CHANGE_ADDED: "a new designation was observed on the list",
    CHANGE_MODIFIED: "the listed record changed in the named fields",
    CHANGE_REMOVED: REMOVAL_NOTE,
}


@dataclass(frozen=True)
class IranRelevance:
    """Outcome of the versioned Iran rules, including why it did NOT match."""

    relevant: bool
    rule_id: str
    matched_reason: str
    matched_terms: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "relevant": self.relevant,
            "rules_version": IRAN_RULES_VERSION,
            "rule_id": self.rule_id,
            "matched_reason": self.matched_reason,
            "matched_terms": list(self.matched_terms),
        }


@dataclass(frozen=True)
class SdnChange:
    """One added/removed/modified entry between two snapshots."""

    uid: str
    change: str
    record: Optional[dict] = None            # current state (None when removed)
    previous: Optional[dict] = None          # prior state (None when added)
    changed_fields: tuple[str, ...] = ()
    programs: tuple[str, ...] = field(default=())


# --- parsing ------------------------------------------------------------------


def _local(tag: str) -> str:
    """Local name of a possibly namespaced tag.

    The published list carries a default namespace; matching on local names
    keeps the parser working when Treasury versions the namespace URI.
    """
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _child_text(node, name: str) -> str:
    for child in node:
        if _local(child.tag) == name:
            return (child.text or "").strip()
    return ""


def _children(node, name: str) -> list:
    return [child for child in node if _local(child.tag) == name]


def _joined(node, names: Sequence[str], separator: str = " ") -> str:
    parts = [_child_text(node, name) for name in names]
    return separator.join(part for part in parts if part).strip()


def _entry_name(entry) -> str:
    """Display name: entity names live in ``lastName`` alone, people in both."""
    return _joined(entry, ("firstName", "lastName")) or _child_text(entry, "lastName")


def _list_values(entry, list_tag: str, item_tag: str, fields: Sequence[str]) -> list[str]:
    values: list[str] = []
    for container in _children(entry, list_tag):
        for item in _children(container, item_tag):
            rendered = ", ".join(
                part for part in (_child_text(item, f) for f in fields) if part
            )
            if rendered:
                values.append(rendered)
    return sorted(set(values))


def parse_sdn(xml_text: str) -> tuple[dict[str, dict], dict[str, Any]]:
    """``({uid: record}, list metadata)`` from an SDN-style XML document.

    Returns empty structures rather than raising: an unusable body must be
    recorded as a collection failure, not thrown through the caller.
    """
    root = parse_xml(xml_text, source_code=SOURCE_CODE)
    if root is None:
        return {}, {}

    meta: dict[str, Any] = {}
    for node in root:
        if _local(node.tag) == "publshInformation":  # sic: the published spelling
            meta = {
                "publish_date": _child_text(node, "Publish_Date"),
                "record_count": _child_text(node, "Record_Count"),
            }
            break

    records: dict[str, dict] = {}
    for entry in root.iter():
        if _local(entry.tag) != "sdnEntry":
            continue
        uid = _child_text(entry, "uid")
        if not uid:
            continue  # an entry with no identity cannot be diffed
        programs = sorted(
            {
                text
                for container in _children(entry, "programList")
                for text in (
                    (item.text or "").strip() for item in _children(container, "program")
                )
                if text
            }
        )
        records[uid] = {
            "uid": uid,
            "name": _entry_name(entry),
            "sdn_type": _child_text(entry, "sdnType"),
            "title": _child_text(entry, "title"),
            "remarks": _child_text(entry, "remarks"),
            "programs": programs,
            "akas": _list_values(
                entry, "akaList", "aka", ("type", "category", "firstName", "lastName")
            ),
            "addresses": _list_values(
                entry, "addressList", "address", ("address1", "city", "stateOrProvince", "country")
            ),
            "ids": _list_values(entry, "idList", "id", ("idType", "idNumber", "idCountry")),
            "nationalities": _list_values(entry, "nationalityList", "nationality", ("country",)),
            "citizenships": _list_values(entry, "citizenshipList", "citizenship", ("country",)),
            "dates_of_birth": _list_values(
                entry, "dateOfBirthList", "dateOfBirthItem", ("dateOfBirth",)
            ),
            "places_of_birth": _list_values(
                entry, "placeOfBirthList", "placeOfBirthItem", ("placeOfBirth",)
            ),
        }
    return records, meta


# --- Iran relevance rules -----------------------------------------------------


def _is_direct_iran_program(program: str) -> bool:
    tag = program.strip().upper()
    return tag in IRAN_PROGRAM_EXACT or any(
        tag.startswith(prefix) for prefix in IRAN_PROGRAM_PREFIXES
    )


def _iran_evidence(record: Mapping[str, Any]) -> list[str]:
    """Fields of the record that name Iran, as ``field: matched term`` strings."""
    evidence: list[str] = []
    for field_name in ("name", "title", "remarks"):
        value = str(record.get(field_name) or "")
        for match in dict.fromkeys(_IRAN_TERMS.findall(value)):
            evidence.append(f"{field_name}:{match.lower()}")
    for field_name in ("addresses", "nationalities", "citizenships", "places_of_birth", "akas"):
        for value in record.get(field_name) or ():
            for match in dict.fromkeys(_IRAN_TERMS.findall(str(value))):
                evidence.append(f"{field_name}:{match.lower()}")
    return sorted(set(evidence))


def iran_relevance(record: Mapping[str, Any]) -> IranRelevance:
    """Apply the versioned Iran rules to one SDN record.

    Rules, in order, each stored with the reason it fired:

    1. an Iran-specific program tag makes the designation Iran-related;
    2. a multi-country program tag makes it Iran-related ONLY together with
       Iran evidence in the record itself;
    3. otherwise it is not Iran-related, and the reason for that is stored too.
    """
    programs = [str(p).strip().upper() for p in (record.get("programs") or ()) if str(p).strip()]

    direct = [p for p in programs if _is_direct_iran_program(p)]
    if direct:
        return IranRelevance(
            relevant=True,
            rule_id=f"{IRAN_RULES_VERSION}.direct_program",
            matched_reason=(
                f"program {'/'.join(direct)} is an Iran-specific OFAC program"
            ),
            matched_terms=tuple(direct),
        )

    conditional = [p for p in programs if p in CONDITIONAL_PROGRAMS]
    if conditional:
        evidence = _iran_evidence(record)
        if evidence:
            return IranRelevance(
                relevant=True,
                rule_id=f"{IRAN_RULES_VERSION}.conditional_program_with_iran_evidence",
                matched_reason=(
                    f"program {'/'.join(conditional)} is multi-country; Iran evidence "
                    f"in the record: {', '.join(evidence)}"
                ),
                matched_terms=tuple(conditional) + tuple(evidence),
            )
        return IranRelevance(
            relevant=False,
            rule_id=f"{IRAN_RULES_VERSION}.conditional_program_without_iran_evidence",
            matched_reason=(
                f"program {'/'.join(conditional)} is multi-country and the record "
                "carries no Iran country, nationality or remark"
            ),
        )

    return IranRelevance(
        relevant=False,
        rule_id=f"{IRAN_RULES_VERSION}.no_iran_program",
        matched_reason=(
            "no Iran-specific program tag on the designation"
            if programs
            else "the designation carries no program tag"
        ),
    )


# --- diff ---------------------------------------------------------------------


def diff_snapshots(
    previous: Mapping[str, dict], current: Mapping[str, dict]
) -> list[SdnChange]:
    """Added/removed/modified entries between two parsed snapshots.

    Deterministically ordered (added, modified, removed; uid within each) so a
    re-run over the same pair produces the same rows in the same order.
    """
    changes: list[SdnChange] = []
    for uid in current.keys() - previous.keys():
        record = current[uid]
        changes.append(
            SdnChange(
                uid=uid,
                change=CHANGE_ADDED,
                record=record,
                programs=tuple(record.get("programs") or ()),
            )
        )
    for uid in previous.keys() - current.keys():
        record = previous[uid]
        changes.append(
            SdnChange(
                uid=uid,
                change=CHANGE_REMOVED,
                previous=record,
                programs=tuple(record.get("programs") or ()),
            )
        )
    for uid in current.keys() & previous.keys():
        before, after = previous[uid], current[uid]
        changed = tuple(
            name for name in COMPARED_FIELDS if before.get(name) != after.get(name)
        )
        if changed:
            changes.append(
                SdnChange(
                    uid=uid,
                    change=CHANGE_MODIFIED,
                    record=after,
                    previous=before,
                    changed_fields=changed,
                    programs=tuple(after.get("programs") or ()),
                )
            )
    changes.sort(key=lambda change: (_CHANGE_ORDER[change.change], change.uid))
    return changes


# --- normalization ------------------------------------------------------------


def _summary(change: SdnChange) -> str:
    state = change.record or change.previous or {}
    programs = ", ".join(state.get("programs") or ()) or "none listed"
    if change.change == CHANGE_MODIFIED:
        return f"Changed fields: {', '.join(change.changed_fields)}. Programs: {programs}."
    if change.change == CHANGE_REMOVED:
        return f"Entry no longer present on the list. Programs when last listed: {programs}."
    return f"Entry present on the list. Programs: {programs}."


def build_articles(
    changes: Sequence[SdnChange],
    *,
    snapshot_sha256: str,
    previous_sha256: str,
    list_url: str,
    list_meta: Mapping[str, Any],
    fetched_at: datetime,
) -> list[CollectedArticle]:
    """One normalized row per change, carrying its rule match and its caveat.

    The dedupe key is a URN rather than a URL: the identity of a list change is
    the entry that changed inside a specific pair of snapshots, and no page
    exists for it.  Including the snapshot digest keeps a re-designation of the
    same uid from colliding with the earlier one while a re-run over the same
    snapshot pair stays idempotent.

    ``source_published_at`` is None on purpose.  The list states a publication
    DATE and no time; turning that into an instant would invent the hours, and
    the change is only actionable to us when we see it anyway.
    """
    articles: list[CollectedArticle] = []
    digest = snapshot_sha256[:12]
    for change in changes:
        state = change.record or change.previous or {}
        relevance = iran_relevance(state)
        name = state.get("name") or f"uid {change.uid}"
        previous_values = (
            {name_: change.previous.get(name_) for name_ in change.changed_fields}
            if change.previous is not None and change.changed_fields
            else None
        )
        provenance: dict[str, Any] = {
            "change": change.change,
            "uid": change.uid,
            "changed_fields": list(change.changed_fields),
            "programs": list(change.programs),
            "record": change.record,
            "previous_values": previous_values,
            "list_publish_date": list_meta.get("publish_date", ""),
            "list_record_count": list_meta.get("record_count", ""),
            "snapshot_sha256": snapshot_sha256,
            "previous_snapshot_sha256": previous_sha256,
            "iran_relevance": relevance.as_dict(),
            # Nothing here asserts a market effect or a policy direction.
            "interpretation": {
                "hypothesis_only": True,
                "note": CHANGE_NOTES[change.change],
            },
        }
        articles.append(
            CollectedArticle(
                source_code=SOURCE_CODE,
                canonical=f"urn:ofac:sdn:{change.uid}:{change.change}:{digest}",
                url=list_url,
                title=f"OFAC SDN entry {change.change}: {name} (uid {change.uid})",
                summary=_summary(change),
                source_published_at=None,
                published_at_is_estimated=True,
                available_at=fetched_at,
                external_id=change.uid,
                language="en",
                provenance=provenance,
            )
        )
    return articles


# --- collection ---------------------------------------------------------------


def collect(engine, settings: Settings, *, dry_run: bool = False) -> dict[str, Any]:
    """Fetch the SDN list, store the snapshot, and diff it against the last one."""
    result = base_result(SOURCE_CODE, PARSER_VERSION, dry_run)
    result.update(added=0, removed=0, modified=0, baseline=False, unchanged_snapshot=False)
    with engine.begin() as conn:
        source_row = load_source(conn, SOURCE_CODE)

    started_at = utcnow()
    reason = poll_gate(source_row, settings, now=started_at)
    if reason is not None:
        result["reason"] = reason
        return result

    list_url = (source_row or {}).get("feed_url") or DEFAULT_LIST_URL
    timeout = max(float(settings.http_timeout_seconds), MIN_TIMEOUT_SECONDS)
    try:
        response = safe_get(
            list_url,
            allow_hosts=ALLOW_HOSTS,
            max_bytes=MAX_FETCH_BYTES,
            timeout=timeout,
        )
        status = getattr(response, "status_code", None)
        body = getattr(response, "text", "") or ""
    except Exception as exc:
        return record_failure(
            engine,
            result,
            started_at=started_at,
            error_class="fetch_error",
            detail=f"{type(exc).__name__}: {exc}",
            dry_run=dry_run,
        )

    result["http_status"] = status
    byte_count = len(body.encode("utf-8"))
    if status is not None and int(status) != 200:
        return record_failure(
            engine,
            result,
            started_at=started_at,
            error_class="http_status",
            detail=f"HTTP {status}",
            dry_run=dry_run,
            http_status=status,
            bytes_received=byte_count,
        )

    current, meta = parse_sdn(body)
    if not current:
        return record_failure(
            engine,
            result,
            started_at=started_at,
            error_class="no_entries_parsed",
            detail="SDN body contained no parseable entries",
            dry_run=dry_run,
            outcome=OUTCOME_EMPTY,
            http_status=status,
            bytes_received=byte_count,
        )
    result["items_seen"] = len(current)

    snapshot_sha = sha256_text(body)
    if len(body) > MAX_STORED_BODY_CHARS:
        # Storing a partial snapshot would poison every future diff against it.
        return record_failure(
            engine,
            result,
            started_at=started_at,
            error_class="payload_too_large",
            detail=(
                f"snapshot of {len(body)} chars exceeds the {MAX_STORED_BODY_CHARS} "
                "storage cap; a truncated snapshot cannot be diffed"
            ),
            dry_run=dry_run,
            http_status=status,
            bytes_received=byte_count,
        )

    with engine.begin() as conn:
        baseline_row = latest_raw_payload(conn, SOURCE_CODE)

    previous_sha = str((baseline_row or {}).get("body_sha256") or "")
    if baseline_row is not None and previous_sha == snapshot_sha:
        result["status"] = OUTCOME_OK
        result["reason"] = "snapshot_unchanged"
        result["unchanged_snapshot"] = True
        result["raw_payload_id"] = int(baseline_row["id"])
        if not dry_run:
            _finish_ok(engine, result, started_at, status, byte_count, counts=None)
        return result

    previous: dict[str, dict] = {}
    if baseline_row is None:
        result["baseline"] = True
        result["reason"] = "first_snapshot"
    elif baseline_row.get("truncated") or not baseline_row.get("body"):
        # A stored snapshot we cannot re-read is not a baseline; treating it as
        # one would report the missing tail as mass delisting.
        result["baseline"] = True
        result["reason"] = "previous_snapshot_unusable"
    else:
        previous, _ = parse_sdn(str(baseline_row["body"]))
        if not previous:
            result["baseline"] = True
            result["reason"] = "previous_snapshot_unparseable"

    if dry_run:
        result["status"] = OUTCOME_OK
        changes = [] if result["baseline"] else diff_snapshots(previous, current)
        _count_changes(result, changes)
        result["reason"] = result["reason"] or "dry_run"
        return result

    with engine.begin() as conn:
        raw_payload_id = store_raw_payload(
            conn,
            source_code=SOURCE_CODE,
            request_url=list_url,
            body=body,
            fetched_at=started_at,
            parser_version=PARSER_VERSION,
            http_status=status,
            content_type=content_type_of(response),
            max_body_chars=MAX_STORED_BODY_CHARS,
        )
    result["raw_payload_id"] = raw_payload_id

    changes = [] if result["baseline"] else diff_snapshots(previous, current)
    _count_changes(result, changes)
    articles = build_articles(
        changes,
        snapshot_sha256=snapshot_sha,
        previous_sha256=previous_sha,
        list_url=list_url,
        list_meta=meta,
        fetched_at=started_at,
    )
    with engine.begin() as conn:
        counts = store_articles(
            conn,
            articles,
            raw_payload_id=raw_payload_id,
            parser_version=PARSER_VERSION,
            fetched_at=started_at,
        )
    # items_seen stays the number of ENTRIES read; the change counts are the
    # normalized output and are reported separately.
    result["items_new"] = counts["items_new"]
    result["items_duplicate"] = counts["items_duplicate"]
    result["status"] = OUTCOME_OK
    _finish_ok(engine, result, started_at, status, byte_count, counts=counts)
    return result


def _count_changes(result: dict[str, Any], changes: Sequence[SdnChange]) -> None:
    result["added"] = sum(1 for c in changes if c.change == CHANGE_ADDED)
    result["removed"] = sum(1 for c in changes if c.change == CHANGE_REMOVED)
    result["modified"] = sum(1 for c in changes if c.change == CHANGE_MODIFIED)


def _finish_ok(
    engine,
    result: dict[str, Any],
    started_at: datetime,
    http_status: Optional[int],
    byte_count: int,
    *,
    counts: Optional[dict[str, int]],
) -> None:
    finished_at = utcnow()
    with engine.begin() as conn:
        record_attempt(
            conn,
            source_code=SOURCE_CODE,
            started_at=started_at,
            finished_at=finished_at,
            outcome=OUTCOME_OK,
            parser_version=PARSER_VERSION,
            http_status=http_status,
            bytes_received=byte_count,
            items_seen=result["items_seen"],
            items_new=(counts or {}).get("items_new", 0),
            items_duplicate=(counts or {}).get("items_duplicate", 0),
        )
        mark_polled(conn, SOURCE_CODE, now=finished_at, ok=True)
