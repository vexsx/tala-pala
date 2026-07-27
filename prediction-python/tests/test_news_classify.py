"""Tests for gazetteer entity extraction and the rule classifier.

The assertions that matter here are about SHAPE OF BELIEF, not accuracy: no
event study exists, so nothing can be tested against a measured effect.  What
can be tested is that the system says different things about different channels
(a hawkish Fed is not an Iranian FX story), that it says "I don't know" when no
rule fires, and that every claim it does make is labelled as a hypothesis with
the rule id that produced it.
"""
from __future__ import annotations

import pytest

from app.news import classify, entities

# The channel vocabulary the schema will accept
# (database/migrations/0017_news_intelligence.up.sql CHECK constraint).
SCHEMA_CHANNELS = {
    "xau_usd", "usd_irt", "local_premium", "liquidity_spread", "gold_funds",
    "combined_ir_gold",
}

HAWKISH_FED = "Fed signals further rate hikes as FOMC turns hawkish"
DOVISH_FED = "Fed opens the door to rate cuts as FOMC turns dovish"
SANCTIONS_UP = "US imposes new sanctions on Iran oil exports"
SANCTIONS_UP_BODY = "The Treasury blacklisted shipping firms carrying Iranian crude."
SANCTIONS_DOWN = "Washington lifted sanctions on Iran and released frozen funds"
IRRELEVANT = "Local bakery wins regional award for its sourdough"

# One headline per rule family, used for the invariants that must hold for
# every classified event.
CORPUS = (
    HAWKISH_FED,
    DOVISH_FED,
    SANCTIONS_UP,
    SANCTIONS_DOWN,
    "Treasury yields surged to a new high",
    "The dollar index rose as the greenback rallied",
    "Nonfarm payrolls came in above expectations",
    "CPI came in below forecast as consumer prices eased",
    "Safe haven demand jumped in a global market selloff",
    "Iran nuclear talks collapsed in Vienna",
    "Iran and the US resumed nuclear talks and reported progress",
    "Airstrike and missile attack reported overnight",
    "Ceasefire agreed as tensions eased",
    "Oil prices surged after OPEC supply news",
    "Protests and unrest reported across Iran",
    "بانک مرکزی محدودیت ارزی جدید اعمال کرد",
    "توقف معاملات در مرکز مبادله طلا اعلام شد",
    "نقدینگی و کسری بودجه افزایش یافت",
    "New value added tax rules announced for gold trading",
    "Central bank buying lifted official sector demand for gold",
)


# --- entities -----------------------------------------------------------------


def test_entities_extracted_in_english_and_persian():
    english = {match.code for match in entities.extract(SANCTIONS_UP, SANCTIONS_UP_BODY)}
    assert "iran" in english
    assert "crude_oil" in english

    persian = {
        match.code
        for match in entities.extract("بانک مرکزی ایران نرخ ارز را تثبیت کرد")
    }
    assert "cbi" in persian
    assert "iran" in persian


def test_matched_term_is_reported_with_the_entity():
    matches = {match.code: match for match in entities.extract("Powell spoke at the FOMC")}
    assert matches["jerome_powell"].matched_term.lower() == "powell"
    assert matches["federal_reserve"].matched_term.lower() in {"fomc", "fed", "the fed"}
    assert matches["federal_reserve"].extractor_version == entities.EXTRACTOR_VERSION


def test_same_kind_containment_resolves_to_the_longer_name():
    # The Persian for "US Federal Reserve" contains the Persian for "Central
    # Bank of Iran"; only one central bank may survive.
    codes = {
        match.code
        for match in entities.extract("بانک مرکزی آمریکا نرخ بهره را افزایش داد")
    }
    assert "federal_reserve" in codes
    assert "cbi" not in codes


def test_gazetteer_never_assigns_coordinates():
    for row in entities.gazetteer_rows():
        assert "latitude" not in row
        assert "longitude" not in row
        assert row["location_verified"] is False


def test_entity_overlap_of_disjoint_or_empty_sets_is_zero():
    assert entities.overlap({"iran"}, {"iran", "gold"}) == pytest.approx(0.5)
    assert entities.overlap(set(), {"iran"}) == 0.0


# --- the asymmetry the channels exist for -------------------------------------


def test_hawkish_fed_is_bearish_gold_but_barely_a_toman_story():
    result = classify.classify(HAWKISH_FED)
    assert result.category == "federal_reserve"
    assert result.primary.rule_id == "fed_hawkish"

    xau = result.hypothesis(classify.CHANNEL_XAU)
    toman = result.hypothesis(classify.CHANNEL_USD_IRT)
    assert xau.score < -0.3
    # Near-neutral is the claim: the FOMC does not drive the free-market toman.
    assert abs(toman.score) <= 0.1
    assert xau.confidence > toman.confidence


def test_dovish_fed_mirrors_the_hawkish_sign():
    hawkish = classify.classify(HAWKISH_FED).hypothesis(classify.CHANNEL_XAU)
    dovish = classify.classify(DOVISH_FED).hypothesis(classify.CHANNEL_XAU)
    assert hawkish.score < 0 < dovish.score
    assert classify.classify(DOVISH_FED).category == "federal_reserve"


def test_sanctions_escalation_lifts_toman_and_premium_and_leaves_global_gold_alone():
    result = classify.classify(SANCTIONS_UP, SANCTIONS_UP_BODY)
    assert result.category == "sanctions_escalation"

    assert result.hypothesis(classify.CHANNEL_USD_IRT).score > 0
    assert result.hypothesis(classify.CHANNEL_PREMIUM).score > 0
    # An Iran-specific measure has no channel into the dollar gold price.
    assert result.hypothesis(classify.CHANNEL_XAU).score == 0.0
    assert result.hypothesis(classify.CHANNEL_LIQUIDITY).score > 0


def test_sanctions_relief_is_the_mirror_of_escalation():
    relief = classify.classify(SANCTIONS_DOWN)
    assert relief.category == "sanctions_relief"
    assert relief.hypothesis(classify.CHANNEL_USD_IRT).score < 0
    assert relief.hypothesis(classify.CHANNEL_PREMIUM).score < 0


def test_combined_channel_is_a_price_identity_not_a_sentiment_average():
    fed = classify.classify(HAWKISH_FED)
    sanctions = classify.classify(SANCTIONS_UP, SANCTIONS_UP_BODY)

    for result in (fed, sanctions):
        legs = [result.hypothesis(channel).score for channel in classify.PRICE_LEGS]
        combined = result.hypothesis(classify.CHANNEL_COMBINED).score
        expected = sum(
            classify.COMBINED_WEIGHTS[channel] * result.hypothesis(channel).score
            for channel in classify.PRICE_LEGS
        )
        assert combined == pytest.approx(expected, abs=1e-4)
        # The composite is weighted by the identity, so it is NOT the mean of
        # the legs and it is not the mean of every channel either.
        assert combined != pytest.approx(sum(legs) / len(legs), abs=1e-4)

    # Same weights, opposite stories: the Fed story lands on the dollar gold
    # leg, the sanctions story on the FX leg, so the composites disagree.
    assert fed.hypothesis(classify.CHANNEL_COMBINED).score < 0
    assert sanctions.hypothesis(classify.CHANNEL_COMBINED).score > 0


def test_liquidity_channel_is_a_cost_not_a_direction():
    # Both a shock up and a shock down widen spreads.
    escalation = classify.classify("Airstrike and missile attack reported overnight")
    disruption = classify.classify("توقف معاملات در مرکز مبادله طلا اعلام شد")
    assert escalation.hypothesis(classify.CHANNEL_LIQUIDITY).score > 0
    assert disruption.hypothesis(classify.CHANNEL_LIQUIDITY).score > 0
    # A halted venue says nothing about the level, only about execution.
    assert disruption.hypothesis(classify.CHANNEL_PREMIUM).score == 0.0


# --- the honesty invariants ---------------------------------------------------


def test_unclassified_when_no_rule_matches_and_it_asserts_nothing():
    result = classify.classify(IRRELEVANT)
    assert result.category == classify.UNCLASSIFIED
    assert result.is_classified is False
    assert result.primary.rule_id == ""
    assert result.primary.confidence == 0.0
    # No rule, no hypothesis: silence is not a neutral score.
    assert result.hypotheses == ()
    assert classify.hypothesis_rows(1, result) == []
    assert classify.classification_rows(1, result) == []


@pytest.mark.parametrize("headline", CORPUS)
def test_every_hypothesis_is_labelled_and_attributable(headline):
    result = classify.classify(headline)
    assert result.is_classified, headline
    assert result.category in classify.CATEGORIES
    channels = {item.channel for item in result.hypotheses}
    assert channels == set(classify.CHANNELS)
    for item in result.hypotheses:
        assert item.hypothesis_only is True
        assert item.rule_id
        assert item.rule_version
        assert item.channel in SCHEMA_CHANNELS
        assert -1.0 <= item.score <= 1.0
        assert 0.0 <= item.confidence <= classify.MAX_RULE_CONFIDENCE
        assert item.mechanism
        assert item.supporting_evidence


def test_rule_confidence_never_reaches_certainty():
    for headline in CORPUS:
        result = classify.classify(headline)
        assert result.primary.confidence <= classify.MAX_RULE_CONFIDENCE


def test_priced_in_qualifier_weakens_the_claim():
    surprise = classify.classify(HAWKISH_FED)
    expected = classify.classify(HAWKISH_FED + ", as expected")
    assert expected.primary.confidence < surprise.primary.confidence
    assert "as expected" in expected.primary.contradicting_terms


def test_opposing_rules_penalise_each_other_and_record_the_contradiction():
    both = classify.classify(
        "Fed weighs rate hikes and rate cuts as officials split hawkish and dovish"
    )
    assert both.category == "federal_reserve"
    alone = classify.classify(HAWKISH_FED)
    assert both.primary.confidence < alone.primary.confidence
    assert both.primary.contradicting_terms


def test_secondary_categories_are_kept_when_two_mechanisms_fire():
    result = classify.classify(
        "US imposes new sanctions on Iran as airstrike and missile attack reported",
        SANCTIONS_UP_BODY,
    )
    categories = {result.category} | {item.category for item in result.secondary}
    assert {"sanctions_escalation", "geopolitical_escalation"} <= categories
    # Still exactly one hypothesis per channel: the table is unique per channel.
    assert len(result.hypotheses) == len(set(classify.CHANNELS))


# --- persisted shapes ---------------------------------------------------------


def test_hypothesis_rows_match_the_schema_and_flag_themselves():
    result = classify.classify(SANCTIONS_UP, SANCTIONS_UP_BODY)
    rows = classify.hypothesis_rows(42, result)
    assert len(rows) == len(classify.CHANNELS)
    for row in rows:
        assert row["event_id"] == 42
        assert row["channel"] in SCHEMA_CHANNELS
        assert row["classifier_version"] == classify.CLASSIFIER_VERSION
        assert row["hypothesis_only"] is True
        assert row["rule_id"] and row["rule_version"]
        # NULL, not 0: no event study has counted supporting cases.
        assert row["sample_support"] is None
        assert isinstance(row["supporting_evidence"], list)


def test_classification_rows_are_unique_per_category():
    result = classify.classify(
        "US imposes new sanctions on Iran as airstrike and missile attack reported",
        SANCTIONS_UP_BODY,
    )
    rows = classify.classification_rows(7, result)
    categories = [row["category"] for row in rows]
    assert len(categories) == len(set(categories))
    assert all(row["classifier_version"] == classify.CLASSIFIER_VERSION for row in rows)


def test_rule_set_is_internally_consistent():
    assert classify.classifier_version_row()["rule_count"] == len(classify.RULES)
    for rule in classify.RULES:
        assert rule.category in classify.CATEGORIES
        assert set(rule.channels) <= set(classify.CHANNELS)
        assert rule.require_terms or rule.require_entities
        for prior in rule.channels.values():
            assert -1.0 <= prior.score <= 1.0
            assert 0.0 < prior.directness <= 1.0
            assert prior.mechanism
    # Every taxonomy mapping points at a real taxonomy entry (or is explicitly
    # None, meaning the rule states the mechanism first).
    from app.news import taxonomy

    for category, mapped in classify.TAXONOMY_CATEGORY.items():
        assert category in classify.CATEGORIES
        assert mapped is None or taxonomy.is_known(mapped)
