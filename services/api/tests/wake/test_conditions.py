"""Tests for wake/conditions.py — Condition parser and evaluator (T1.4)."""

from __future__ import annotations

import pytest

from chronos.wake.conditions import Condition, Operator, parse_condition


class TestParseStringConditions:
    def test_parse_gte(self) -> None:
        c = parse_condition(">= 540")
        assert c.operator is Operator.gte
        assert c.operand == 540

    def test_parse_gt(self) -> None:
        c = parse_condition("> 10")
        assert c.operator is Operator.gt
        assert c.operand == 10

    def test_parse_lte(self) -> None:
        c = parse_condition("<= 30")
        assert c.operator is Operator.lte
        assert c.operand == 30

    def test_parse_lt(self) -> None:
        c = parse_condition("< 10")
        assert c.operator is Operator.lt
        assert c.operand == 10

    def test_parse_eq_numeric(self) -> None:
        c = parse_condition("== 5")
        assert c.operator is Operator.eq
        assert c.operand == 5

    def test_parse_eq_string(self) -> None:
        c = parse_condition("== foreground")
        assert c.operator is Operator.eq
        assert c.operand == "foreground"

    def test_parse_eq_true(self) -> None:
        c = parse_condition("== true")
        assert c.operator is Operator.eq
        assert c.operand is True

    def test_parse_eq_false(self) -> None:
        c = parse_condition("== false")
        assert c.operator is Operator.eq
        assert c.operand is False

    def test_parse_float_operand(self) -> None:
        c = parse_condition(">= 0.5")
        assert c.operator is Operator.gte
        assert c.operand == 0.5

    def test_parse_strips_whitespace(self) -> None:
        c = parse_condition("  >=  100  ")
        assert c.operator is Operator.gte
        assert c.operand == 100


class TestParseListConditions:
    def test_parse_list_as_in(self) -> None:
        c = parse_condition(["denied", "restricted"])
        assert c.operator is Operator.in_
        assert c.operand == frozenset({"denied", "restricted"})

    def test_parse_single_element_list(self) -> None:
        c = parse_condition(["foreground"])
        assert c.operator is Operator.in_
        assert c.operand == frozenset({"foreground"})

    def test_empty_list_raises(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            parse_condition([])


class TestParseErrors:
    def test_invalid_operator_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid condition"):
            parse_condition("!= 540")

    def test_missing_operand_raises(self) -> None:
        with pytest.raises(ValueError, match="no operand"):
            parse_condition(">=")

    def test_garbage_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid condition"):
            parse_condition("?? invalid")

    def test_numeric_operator_string_operand_raises(self) -> None:
        with pytest.raises(ValueError, match="numeric operand"):
            parse_condition(">= notanumber")


class TestEvaluateNumeric:
    def test_gte_true(self) -> None:
        assert Condition(Operator.gte, 540).evaluate(600)

    def test_gte_equal(self) -> None:
        assert Condition(Operator.gte, 540).evaluate(540)

    def test_gte_false(self) -> None:
        assert not Condition(Operator.gte, 540).evaluate(539)

    def test_gt_true(self) -> None:
        assert Condition(Operator.gt, 10).evaluate(11)

    def test_gt_equal_false(self) -> None:
        assert not Condition(Operator.gt, 10).evaluate(10)

    def test_lte_true(self) -> None:
        assert Condition(Operator.lte, 10).evaluate(9)

    def test_lte_equal(self) -> None:
        assert Condition(Operator.lte, 10).evaluate(10)

    def test_lt_true(self) -> None:
        assert Condition(Operator.lt, 10).evaluate(9)

    def test_lt_equal_false(self) -> None:
        assert not Condition(Operator.lt, 10).evaluate(10)

    def test_evaluate_numeric_false(self) -> None:
        c = parse_condition(">= 540")
        assert not c.evaluate(100)

    def test_evaluate_numeric_true(self) -> None:
        c = parse_condition("< 10")
        assert c.evaluate(5)

    def test_numeric_operator_on_string_raises(self) -> None:
        c = Condition(Operator.gte, 10)
        with pytest.raises(TypeError):
            c.evaluate("foo")


class TestEvaluateBool:
    def test_eq_true_matches(self) -> None:
        c = Condition(Operator.eq, True)
        assert c.evaluate(True)

    def test_eq_true_no_match(self) -> None:
        c = Condition(Operator.eq, True)
        assert not c.evaluate(False)

    def test_evaluate_bool_true(self) -> None:
        c = parse_condition("== true")
        assert c.evaluate(True)

    def test_evaluate_bool_false_no_match(self) -> None:
        c = parse_condition("== true")
        assert not c.evaluate(False)


class TestEvaluateIn:
    def test_evaluate_in_match(self) -> None:
        c = parse_condition(["denied", "restricted"])
        assert c.evaluate("denied")

    def test_evaluate_in_no_match(self) -> None:
        c = parse_condition(["denied", "restricted"])
        assert not c.evaluate("authorized")

    def test_in_operator_wrong_operand_raises(self) -> None:
        # in_ requires frozenset operand; build invalid one manually
        c = Condition(Operator.in_, 42)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="frozenset"):
            c.evaluate("foo")
