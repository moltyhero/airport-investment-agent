import pytest

from airport_agent import scoring as sc
from airport_agent.agent import Agent


def test_resolve_aliases():
    assert sc.resolve("LA") == "LAX" and sc.resolve("Santa Ana") == "SNA"


def test_weights_sum_to_one():
    assert abs(sum(sc.DEFAULT_WEIGHTS.values()) - 1) < 1e-9


def test_scores_bounded_and_deterministic():
    live = ({}, True)
    a = sc.score("BOS", live=live)
    assert a == sc.score("BOS", live=live)
    assert 0 <= a["opportunity_score"] <= 100


def test_sna_constraint_lowers_feasibility():
    assert sc.score("SNA", live=({}, True))["components"]["feasibility"] < 30


def test_feed_down_drops_delay_weight():
    s = sc.score("BOS", live=({}, False))
    assert "delay" not in s["weights_used"] and abs(sum(s["weights_used"].values()) - 1) < 1e-6


@pytest.mark.parametrize("q", [
    "Which airports in New England are strong candidates for terminal expansion?",
    "Compare LA and Santa Ana airport congestion levels",
    "What is the percentage of long haul flights out of Anchorage airport?",
    "What is the unmet flight demand in SFO airport and why?",
])
def test_offline_questions(q):
    r = Agent()._offline(q)
    assert r["tools"] and "⚠️" not in r["reply"].split("\n")[0]


def test_followup_uses_context():
    a = Agent()
    a._offline("What is the unmet demand in SFO?")
    r = a._offline("and what about its long haul share?")
    assert r["tools"][0]["args"]["airport"] == "SFO"
