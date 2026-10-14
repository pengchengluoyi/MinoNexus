"""Published Web locator and three-valued predicate contract, stdlib only."""
from __future__ import annotations

CONTRACTS = {"web_v5": 1, "locator_contract": "web.locator.v1", "predicate_facts_contract": "web.atomic.v1"}
PREDICATES = {"visible", "not_visible", "text_equals", "value_equals", "count_equals",
              "secret_nonempty", "secret_length_matches"}
PROFILE = {"id": "web-p0-v1", "resolve_ms": 1000, "action_ms": 5000, "navigation_ms": 10000,
           "query_ms": 3000, "capture_ms": 3000, "transport_ms": 1000, "commit_ms": 2000}


def validate_locator(spec):
    if not isinstance(spec, dict) or set(spec) != {"playwright"}:
        raise ValueError("invalid_locator")
    cfg = spec["playwright"]
    if cfg.get("scope") != {"kind": "page", "page_id": "main"}:
        raise ValueError("unsupported_scope")
    if cfg.get("contract") != "web.locator.v1" or cfg.get("cardinality") not in {"one", "set"}:
        raise ValueError("invalid_locator")
    sel = cfg.get("selector") or {}
    kind = sel.get("kind")
    if kind not in {"test_id", "role", "label", "css"} or set(sel) - {"kind", "value", "name", "exact"}:
        raise ValueError("invalid_locator")
    if not isinstance(sel.get("value"), str) or not sel["value"].strip():
        raise ValueError("invalid_locator")
    if kind in {"role", "label"} and sel.get("exact") is not True:
        raise ValueError("invalid_locator")
    if kind == "role" and not isinstance(sel.get("name"), str):
        raise ValueError("invalid_locator")
    if kind == "css" and any(x in sel["value"].lower() for x in
                             ("xpath=", "text=", ">>", "nth", ":first", ":last", "javascript:", ":has")):
        raise ValueError("invalid_locator")


def validate_expected(expected, *, sensitive=False, depth=0):
    if depth > 8 or not isinstance(expected, dict):
        raise ValueError("invalid_predicate")
    if "all" in expected or "any" in expected:
        kind = "all" if "all" in expected else "any"
        children = expected[kind]
        if set(expected) != {kind} or not isinstance(children, list) or not 1 <= len(children) <= 32:
            raise ValueError("invalid_predicate")
        for child in children:
            validate_expected(child, sensitive=sensitive, depth=depth + 1)
        return
    op = expected.get("predicate")
    if op not in PREDICATES or set(expected) - {"predicate", "value"}:
        raise ValueError("invalid_predicate")
    if sensitive and op in {"value_equals", "text_equals"}:
        raise ValueError("sensitive_predicate_forbidden")
    if op.endswith("equals") or op == "secret_length_matches":
        val = expected.get("value")
        if op in {"count_equals", "secret_length_matches"}:
            if type(val) is not int or val < 0:
                raise ValueError("invalid_predicate")
        elif not isinstance(val, str):
            raise ValueError("invalid_predicate")


def requested_facts(expected):
    if "all" in expected or "any" in expected:
        return sorted({f for child in expected.get("all", expected.get("any", [])) for f in requested_facts(child)})
    op = expected["predicate"]
    return [{"visible": "visible", "not_visible": "visible", "text_equals": "text", "value_equals": "value",
             "count_equals": "count", "secret_nonempty": "secret_value", "secret_length_matches": "secret_value"}[op]]


def evaluate(expected, facts):
    """Every evaluated child is retained, including unavailable branches."""
    if "all" in expected or "any" in expected:
        kind = "all" if "all" in expected else "any"
        children = [evaluate(child, facts) for child in expected[kind]]
        values = [x["truth"] for x in children]
        deciding, neutral = ("false", "true") if kind == "all" else ("true", "false")
        truth = deciding if deciding in values else "unavailable" if "unavailable" in values else neutral
        return {"truth": truth, "expected": expected, "children": children}
    op = expected["predicate"]
    evidence = {"truth": "unavailable", "expected": expected}
    if not facts.get("scope_resolved") or not facts.get("collection_complete"):
        return evidence
    count, complete = facts.get("count"), facts.get("fact_complete", {})
    if type(count) is not int or not complete.get("count"):
        return evidence
    if op == "count_equals":
        answer = count == expected["value"]
    elif op == "not_visible" and count == 0:
        answer = True
    elif op == "visible" and count == 0:
        answer = False
    elif count != 1:
        return evidence
    elif op in {"visible", "not_visible"} and complete.get("visible") and type(facts.get("visible")) is bool:
        answer = facts["visible"] == (op == "visible")
    elif op in {"text_equals", "value_equals"}:
        field = "text" if op == "text_equals" else "value"
        if facts.get("sensitive") or not complete.get(field) or not isinstance(facts.get(field), str):
            return evidence
        answer = facts[field] == expected["value"]
    elif op in {"secret_nonempty", "secret_length_matches"} and complete.get("secret_value"):
        secret = facts.get("secret_value", {})
        if op == "secret_nonempty" and type(secret.get("nonempty")) is bool:
            answer = secret["nonempty"]
        elif op == "secret_length_matches" and type(secret.get("length")) is int:
            answer = secret["length"] == expected["value"]
        else:
            return evidence
    else:
        return evidence
    evidence["truth"] = "true" if answer else "false"
    return evidence
