"""Unit tests for edge-detection math."""
import pytest
from edge.odds_math import (
    american_to_decimal, american_to_implied, devig_two_way,
    devig_multi_way, expected_value, overround,
)
from edge.kelly import kelly, fractional_kelly


def test_american_to_decimal_negative():
    assert abs(american_to_decimal(-110) - 1.9091) < 0.001


def test_american_to_decimal_positive():
    assert abs(american_to_decimal(110) - 2.1) < 0.001


def test_devig_even_line():
    fh, fa = devig_two_way(-110, -110)
    assert abs(fh - 0.5) < 0.001
    assert abs(fa - 0.5) < 0.001


def test_devig_sums_to_one():
    fh, fa = devig_two_way(-150, 130)
    assert abs(fh + fa - 1.0) < 0.0001


def test_devig_favorite_higher():
    fh, fa = devig_two_way(-150, 130)
    assert fh > fa


def test_devig_multi_way_sums_to_one():
    h, d, a = devig_multi_way([-120, 280, 250])
    assert abs(h + d + a - 1.0) < 0.0001


def test_overround_positive():
    assert overround([-110, -110]) > 0


def test_kelly_no_edge():
    fair, _ = devig_two_way(-110, -110)
    assert kelly(fair, american_to_decimal(-110)) < 0.005


def test_kelly_clear_edge():
    # 60% win prob on even money -> Kelly = 0.20
    assert abs(kelly(0.60, 2.0) - 0.20) < 0.001


def test_fractional_kelly_quarter():
    full = kelly(0.55, american_to_decimal(-110))
    frac = fractional_kelly(0.55, american_to_decimal(-110), 0.25)
    assert abs(frac - full * 0.25) < 0.0001


def test_ev_positive():
    assert expected_value(0.60, 2.0) > 0


def test_ev_negative():
    assert expected_value(0.40, 2.0) < 0
