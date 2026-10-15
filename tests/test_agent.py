import pytest

from airport_agent import data_sources as ds
from airport_agent import scoring as sc
from airport_agent.agent import Agent

needs_bts = pytest.mark.skipif(ds.bts_cached_months() == 0, reason="run python -m airport_agent.build_cache first")


def test_universe_is_national():
    u = sc.universe()
    assert len(u) > 150 and {"BOS", "SNA", "LAX", "SFO", "ANC", "PVD"} <= set(u)
    assert len({a["state"] for a in u.values()}) > 40


def test_resolve_aliases_and_cities():
    assert sc.resolve("LA") == "LAX" and sc.resolve("Santa Ana") == "SNA" and sc.resolve("Anchorage") == "ANC"


def test_weights_sum_to_one():
    assert abs(sum(sc.DEFAULT_WEIGHTS.values()) - 1) < 1e-9


def test_scores_bounded_and_deterministic():
    live = ({}, True)
    a = sc.score("BOS", live=live)
    assert a == sc.score("BOS", live=live)
    assert 0 <= a["opportunity_score"] <= 100


def test_live_status_does_not_change_scores():
    quiet = sc.score("BOS", live=({}, True))
    busy = sc.score("BOS", live=({"BOS": {"program": "Ground Stop", "reason": "x", "severity": "CRITICAL"}}, True))
    assert quiet["opportunity_score"] == busy["opportunity_score"]


def test_sna_constraint_lowers_feasibility_and_unknown_is_not_assumed():
    assert sc.score("SNA", live=({}, True))["components"]["feasibility"] < 30
    s = sc.score("PVD", live=({}, True))
    assert s["components"]["feasibility"] is None and "feasibility" in s["missing_components"]
    assert abs(sum(s["weights_used"].values()) - 1) < 1e-6


@pytest.mark.parametrize("q", [
    "Which airports in New England are strong candidates for terminal expansion?",
    "Compare LA and Santa Ana airport congestion levels",
    "What is the unmet flight demand in SFO airport and why?",
])
def test_offline_questions(q):
    r = Agent()._offline(q)
    assert r["tools"] and "error" not in r["reply"].lower()[:20]


@needs_bts
def test_long_haul_from_real_flights():
    r = sc.long_haul_share("ANC")
    assert 0 < r["pct_long_haul_flights"] < 100 and r["flights_counted"] > 500


def test_followup_uses_context():
    a = Agent()
    a._offline("What is the unmet demand in SFO?")
    r = a._offline("and what about its long haul share?")
    assert r["tools"][0]["args"]["airport"] == "SFO"
