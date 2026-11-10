from evals.build_amazon_labels import ATTRIBUTES, DESCRIPTION, TITLE, SourceProduct, extract_field, retrieval_rows


def test_watch_claims_preserve_negation_conflicts_and_source_offsets():
    title = "Test watch, IP67, 22 mm replacement strap"
    description = "No built-in GPS; uses your phone's GPS. IP68 water rating. Case diameter: 40 mm."
    product = SourceProduct({}, {TITLE: title, DESCRIPTION: description})
    fields = ATTRIBUTES["watch"]
    gps = extract_field(product, fields["gps_mode"])
    assert gps["status"] == "observed" and gps["value"] == "phone_connected"
    evidence = gps["evidence"]
    assert evidence["field"] == DESCRIPTION
    assert description[evidence["start"]:evidence["end"]] == evidence["text"] == "uses your phone's GPS"
    ingress = extract_field(product, fields["ingress_rating"])
    assert ingress["status"] == "ambiguous" and ingress["value"] is None
    assert {v["value"] for v in ingress["observations"]} == {"IP67", "IP68"}
    assert extract_field(product, fields["case_diameter_mm"])["value"] == 40


def test_coffee_capacity_does_not_confuse_brew_setting_and_carafe():
    item = SourceProduct({}, {
        TITLE: "12-cup programmable coffee maker",
        DESCRIPTION: "1-4 cup brew setting. Removable 80 oz. Water Reservoir. 15 bar pressure pump.",
    })
    fields = ATTRIBUTES["coffee"]
    assert extract_field(item, fields["carafe_cups"])["value"] == 12
    assert extract_field(item, fields["water_tank_ml"])["value"] == 2365.9
    assert extract_field(item, fields["pump_pressure_bar"])["value"] == 15


def test_shoe_size_is_not_inferred_from_other_size_systems():
    fields = ATTRIBUTES["running"]
    product = SourceProduct({}, {TITLE: "Running shoe UK 6.5 US 7.5 EU 40.5", DESCRIPTION: "Midsole drop: 8 mm"})
    assert extract_field(product, fields["listed_us_size"])["value"] == 7.5
    assert extract_field(product, fields["heel_drop_mm"])["value"] == 8
    product.text[TITLE] = "Running shoe EU 40.5"
    assert extract_field(product, fields["listed_us_size"]) == {"status": "unknown", "value": None}


def test_color_lists_and_capacity_ranges_are_not_single_value_labels():
    product = SourceProduct({}, {TITLE: "Coffee machine", DESCRIPTION: "Color: Blue, Gold. 1 to 2 L water tank."})
    fields = ATTRIBUTES["coffee"]
    assert extract_field(product, fields["listed_color"])["status"] == "unknown"
    assert extract_field(product, fields["water_tank_ml"])["status"] == "unknown"


def test_attribute_qrels_do_not_treat_unknown_as_negative():
    items = [
        SourceProduct({"product_id": pid, "category_path": cell, "brand": "Brand",
                       "reference_price_usd": price}, {})
        for pid, cell, price in (("yes", "Watch", 45), ("unknown", "Watch", 40),
                                 ("above_budget", "Watch", 60), ("strap", "Strap", 8))
    ]
    labels = [
        {"product_id": "yes", "attributes": {"gps_mode": {"status": "observed", "value": "built_in"}}},
        {"product_id": "above_budget", "attributes": {"gps_mode": {"status": "observed", "value": "built_in"}}},
    ]
    spec = {
        "taxonomy": {"watch": "Watch", "band": "Strap"},
        "retrieval": [{"id": "query", "query": "Watches with built-in GPS at most $50",
                       "cells": ["watch"], "max_usd": 50, "hard_negative_cells": ["band"],
                       "attribute": {"field": "gps_mode", "equals": "built_in"}}],
    }
    qrel, = retrieval_rows(items, labels, spec)
    assert qrel["relevant_ids"] == ["yes"]
    assert qrel["unjudged_ids"] == ["unknown"]
    assert qrel["hard_negative_ids"] == ["strap"]
