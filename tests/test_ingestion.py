"""Light ingestion-layer tests (no real network calls)."""
import pytest
from edge.odds_math import american_to_implied, devig_two_way
from ingestion.odds import parse_odds_to_snapshots


def test_parse_empty():
    assert parse_odds_to_snapshots([], "MLB") == []


def test_parse_basic_structure():
    events = [{
        "id": "abc123",
        "bookmakers": [{
            "title": "FanDuel",
            "markets": [{
                "key": "h2h",
                "outcomes": [
                    {"name": "Home Team", "price": -130},
                    {"name": "Away Team", "price": 110},
                ]
            }]
        }]
    }]
    snaps = parse_odds_to_snapshots(events, "MLB")
    assert len(snaps) == 2
    assert all(s["book"] == "FanDuel" for s in snaps)
    assert all(s["sport"] == "MLB" for s in snaps)
    assert all(s["market"] == "h2h" for s in snaps)


def test_implied_negative():
    assert abs(american_to_implied(-130) - 0.5652) < 0.001


def test_implied_positive():
    assert abs(american_to_implied(110) - 0.4762) < 0.001


def test_devig_sums_to_one():
    p1, p2 = devig_two_way(-130, 110)
    assert abs(p1 + p2 - 1.0) < 0.0001
