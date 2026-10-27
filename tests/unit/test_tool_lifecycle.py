import pytest

from nanoscrypt.core.tool_lifecycle import decide_lifecycle_state


@pytest.mark.parametrize(
    ("current_state", "outcomes", "expected_state"),
    [
        ("candidate", [], "candidate"),
        (
            "candidate",
            [
                ("data_transform", "helpful"),
                ("data_transform", "helpful"),
                ("file_cleanup", "helpful"),
                ("file_cleanup", "helpful"),
                ("report_generation", "neutral"),
            ],
            "shared",
        ),
        (
            "shared",
            [("data_transform", "harmful"), ("file_cleanup", "harmful")],
            "needs_improvement",
        ),
        (
            "shared",
            [
                ("data_transform", "harmful"),
                ("file_cleanup", "harmful"),
                ("report_generation", "harmful"),
            ],
            "quarantined",
        ),
        (
            "candidate",
            [
                ("data_transform", "harmful"),
                ("data_transform", "harmful"),
                ("file_cleanup", "harmful"),
            ],
            "quarantined",
        ),
        (
            "retired",
            [("data_transform", "helpful")],
            "retired",
        ),
    ],
)
def test_lifecycle_decisions(current_state, outcomes, expected_state):
    state, reason = decide_lifecycle_state(current_state, outcomes)

    assert state == expected_state
    assert reason
