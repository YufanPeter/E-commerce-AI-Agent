import pytest

from store.numeric_attributes import numeric_attributes, rule_constraints, satisfies, stated_constraint


@pytest.mark.parametrize("text, expected", [
    ("speaker with at least 20 hours of battery life", [("battery_hours", ">=", 20)]),
    ("short lightning cable, 3 feet or less", [("cable_length_ft", "<=", 3)]),
    ("sunscreen SPF 50 or higher under $20", [("spf", ">=", 50)]),
    ("face sunscreen with at least SPF 30", [("spf", ">=", 30)]),
    ("travel shampoo under 3.4 oz", [("volume_ml", "<=", 100.54)]),
    ("128GB micro SD card", []),  # A bare size is not a limit.
    ("iPhone 15 case under $20", []),
])
def test_limits_come_from_units_in_the_schema(text, expected):
    assert [(c["attribute"], c["op"], c["value"]) for c in rule_constraints(text)] == expected


def test_model_constraints_need_a_known_attribute_and_a_written_amount():
    assert stated_constraint({"attribute": "storage_gb", "op": ">=", "value": 1024}, "at least 1TB") is not None
    assert stated_constraint({"attribute": "battery_hours", "op": ">=", "value": 20}, "iPhone 15 case") is None
    assert stated_constraint({"attribute": "weight_kg", "op": ">=", "value": 2}, "at least 2 kg") is None
    assert stated_constraint({"attribute": "spf", "op": "=", "value": 50}, "SPF 50") is None


def test_satisfies_is_unknown_without_a_stated_value():
    assert satisfies(["10.0", "48.0"], ">=", 20) is True
    assert satisfies(["10.0"], ">=", 20) is False
    assert satisfies([], "<=", 3) is None


def test_every_unit_belongs_to_one_attribute():
    units = [unit for attribute in numeric_attributes().values() for unit in attribute.units]
    assert len(units) == len(set(units))
