"""Independent finite-cap, shared-line and nonadditive-token counterexamples."""

import copy

import pytest

from packages.semantic import admission, coverage
from packages.semantic import diagnostics as d
from packages.semantic.policy import SemanticError
from tests.test_semantic_coverage import population


def selected_rows():
    members, _ = coverage.selection(population())
    return [
        {
            "member": m,
            "context": {
                "hunks": [None] * m["hunks"],
                "roles": {r: {"unrepresented_hunks": int(i < 7)} for r in coverage.CODE_ROLES},
            },
        }
        for i, m in enumerate(members)
    ]


def test_fixed_residuals_controls_independent_of_order_labels_scores():
    rows = selected_rows()
    first = d.choose(rows)
    assert len(first[0]) <= 17 and sum(m["residual"] for m in first[0]) == 7
    changed = copy.deepcopy(rows[::-1])
    for row in changed:
        row["member"].update(label="private-unknown", score=-900)
    assert d.choose(changed) == first
    assert all("label" not in m and "score" not in m for m in first[0])


@pytest.mark.parametrize(
    "alter", ["population", "residuals", "test", "duplicate", "repository", "strata"]
)
def test_wrong_population_never_substituted(alter):
    rows = selected_rows()
    if alter == "population":
        rows.pop()
    elif alter == "residuals":
        rows[0]["context"]["roles"] = {r: {"unrepresented_hunks": 0} for r in coverage.CODE_ROLES}
    elif alter == "test":
        rows[0]["member"]["partition"] = "test"
    elif alter == "duplicate":
        rows[-1]["member"]["commit_id"] = rows[0]["member"]["commit_id"]
    elif alter == "repository":
        rows[0]["member"]["canonical_repository"] = "another/repository"
    else:
        rows[0]["context"]["hunks"] *= 2
    with pytest.raises(SemanticError):
        d.choose(rows)


def diagnose(groups, encode=list):
    _, old = admission.select_lines(groups, encode)
    _, current = coverage.select_lines(groups, encode)
    return d.role_diagnosis(groups, old, current, encode)


def test_independent_fixture_proves_check_cap_can_hide_feasible_whole_lines():
    groups = [
        {
            "hunk": i,
            "eligible_lines": 3,
            "candidates": [(i, 0, "x" * 1200), (i, 1, "y" * 1200), (i, 2, "z\n")],
        }
        for i in range(256)
    ]
    result = diagnose(groups)
    assert len(result["v3_unrepresented"]) == 256 and result["v3_checks"] == 512
    assert result["finite_replay_unrepresented"] == [] and result["finite_replay_checks"] == 768
    assert result["witness_within_1008"] and result["witness_tokens"] == 767


def test_large_witness_is_not_proof_of_impossibility_and_floor_remains():
    groups = [
        {"hunk": 0, "eligible_lines": 1, "candidates": [(0, 0, "a" * 900)]},
        {"hunk": 1, "eligible_lines": 1, "candidates": [(1, 0, "b" * 200)]},
    ]
    result = diagnose(groups)
    assert result["witness_tokens"] == 1101 and not result["witness_within_1008"]
    assert result["floor_coordinates"] == [{"file": 1, "line": 1}]
    assert {"file": 1, "line": 1} in result["witness_coordinates"]
    assert not result["joined_minimum_or_infeasibility_certified"]


def test_shared_base_and_candidate_cap_are_distinct():
    groups = [
        {"hunk": i, "eligible_lines": 10, "candidates": [(0, 0, "shared\n")]} for i in range(3)
    ]
    result = diagnose(groups)
    assert result["shared_candidate_incidences"] == 2
    assert result["candidate_cap_omitted_incidences"] == 27
    assert len(result["witness_coordinates"]) == 1
    assert result["witness_all_candidate_hunks_represented"]


def test_joined_token_cost_is_measured_even_when_nonadditive():
    groups = [
        {"hunk": i, "eligible_lines": 1, "candidates": [(0, i, body)]}
        for i, body in enumerate(["a", "b"])
    ]

    def encode(value):
        return [0] if value else []

    result = diagnose(groups, encode)
    assert sum(h["minimum_isolated_whole_line_tokens"] for h in result["local_hunks"]) == 2
    assert result["witness_tokens"] == 1 and not result["local_costs_additive"]


def test_drift_in_selected_coordinates_or_checks_rejected():
    groups = [{"hunk": 0, "eligible_lines": 1, "candidates": [(0, 0, "line\n")]}]
    _, old = admission.select_lines(groups, list)
    _, current = coverage.select_lines(groups, list)
    current["budget_checks"] += 1
    with pytest.raises(SemanticError, match="REPLAY_DRIFT"):
        d.role_diagnosis(groups, old, current, list)
