"""Построение маршрута каскада по ORM-правилам (без БД, объекты в памяти)."""

from decimal import Decimal

import pytest

from app.db.models import RoutingRule
from app.domain.enums import Flow, PaymentMethod, ProviderMode
from app.routing.router import RouteStep, build_route


def make_rule(
    provider: str,
    *,
    flow: Flow = Flow.LINK,
    mode: ProviderMode = ProviderMode.HOSTED,
    priority: int = 100,
    enabled: bool = True,
    currencies: list[str] | None = None,
    methods: list[str] | None = None,
    min_amount: Decimal | None = None,
    max_amount: Decimal | None = None,
    fee_percent: str = "1",
) -> RoutingRule:
    return RoutingRule(
        provider=provider,
        flow=flow,
        mode=mode,
        priority=priority,
        enabled=enabled,
        currencies=["RUB"] if currencies is None else currencies,
        methods=["bankcard"] if methods is None else methods,
        min_amount=min_amount,
        max_amount=max_amount,
        fee_percent=Decimal(fee_percent),
    )


def route(rules: list[RoutingRule], *, amount: Decimal = Decimal("100"),
          currency: str = "RUB", method: PaymentMethod = PaymentMethod.BANKCARD,
          flow: Flow = Flow.LINK) -> list[RouteStep]:
    return build_route(rules, flow=flow, amount=amount, currency=currency, method=method)


def providers(steps: list[RouteStep]) -> list[str]:
    return [s.provider for s in steps]


def test_h2h_goes_before_hosted_even_with_worse_priority() -> None:
    rules = [
        make_rule("hosted_fast", mode=ProviderMode.HOSTED, priority=1),
        make_rule("h2h_slow", mode=ProviderMode.H2H, priority=999),
    ]

    assert providers(route(rules)) == ["h2h_slow", "hosted_fast"]


def test_same_mode_sorted_by_priority_then_fee() -> None:
    rules = [
        make_rule("expensive", priority=10, fee_percent="2"),
        make_rule("cheap", priority=10, fee_percent="1"),
        make_rule("top", priority=0, fee_percent="5"),
    ]

    assert providers(route(rules)) == ["top", "cheap", "expensive"]


def test_disabled_rule_filtered_out() -> None:
    rules = [make_rule("off", enabled=False), make_rule("on")]

    assert providers(route(rules)) == ["on"]


def test_other_flow_filtered_out() -> None:
    rules = [make_rule("card_only", flow=Flow.CARD), make_rule("link")]

    assert providers(route(rules)) == ["link"]


def test_other_currency_filtered_out() -> None:
    rules = [make_rule("usd", currencies=["USD"]), make_rule("rub", currencies=["RUB", "USD"])]

    assert providers(route(rules)) == ["rub"]


def test_other_method_filtered_out() -> None:
    rules = [make_rule("sbp_only", methods=["sbp"]), make_rule("card", methods=["bankcard"])]

    assert providers(route(rules)) == ["card"]


def test_amount_below_min_filtered_out() -> None:
    rules = [make_rule("min_500", min_amount=Decimal("500")), make_rule("no_min")]

    assert providers(route(rules, amount=Decimal("100"))) == ["no_min"]


def test_amount_above_max_filtered_out() -> None:
    rules = [make_rule("max_50", max_amount=Decimal("50")), make_rule("no_max")]

    assert providers(route(rules, amount=Decimal("100"))) == ["no_max"]


def test_amount_on_boundaries_is_eligible() -> None:
    rules = [make_rule("bounded", min_amount=Decimal("100"), max_amount=Decimal("100"))]

    assert providers(route(rules, amount=Decimal("100"))) == ["bounded"]


def test_empty_rules_give_empty_route() -> None:
    assert route([]) == []


def test_route_step_dict_roundtrip() -> None:
    rules = [make_rule("pm", mode=ProviderMode.H2H, priority=7, fee_percent="1.5")]

    (step,) = route(rules)

    assert step.to_dict() == {
        "provider": "pm",
        "mode": "h2h",
        "priority": 7,
        "fee_percent": "1.5",
    }
    assert RouteStep.from_dict(step.to_dict()) == step


@pytest.mark.parametrize("mode", list(ProviderMode))
def test_route_step_roundtrip_for_each_mode(mode: ProviderMode) -> None:
    step = RouteStep(provider="p", mode=mode, priority=1, fee_percent="0.5")

    assert RouteStep.from_dict(step.to_dict()) == step
