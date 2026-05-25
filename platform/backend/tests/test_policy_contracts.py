from __future__ import annotations

import pytest

from app.modules.miniapp_agent_loop.agent_command_policy import AgentCommandPolicy


@pytest.mark.parametrize(
    ("command", "expected_action"),
    [
        ("rg api miniapp/app", "allow"),
        ("python3 -m py_compile miniapp/app/main.py", "allow"),
        ("node --check miniapp/app/static/client/app.js", "allow"),
        ("ls miniapp/app", "allow"),
        ("git status --short", "allow"),
        ("curl https://example.com", "forbidden"),
        ("git pull", "forbidden"),
        ("zsh -lc 'ls'", "forbidden"),
        ("PYTHONPATH=platform/backend pytest -q", "forbidden"),
        ("cat miniapp/app/main.py > /tmp/out", "forbidden"),
        ("./tool", "forbidden"),
        ("/usr/bin/env python3 --version", "forbidden"),
    ],
)
def test_default_exec_policy_command_matrix(command: str, expected_action: str) -> None:
    decision = AgentCommandPolicy().decide(command)

    assert decision.action == expected_action


@pytest.mark.parametrize(
    ("command", "blocked_code"),
    [
        ("curl https://example.com", "direct_network_tool"),
        ("git pull", "git_network_operation"),
        ("zsh -lc 'ls'", "forbidden_executable"),
        ("PYTHONPATH=platform/backend pytest -q", "env_assignment"),
        ("cat miniapp/app/main.py > /tmp/out", "shell_metacharacter"),
        ("./tool", "relative_executable"),
        ("/usr/bin/env python3 --version", "forbidden_executable"),
    ],
)
def test_default_exec_policy_reports_stable_block_reasons(command: str, blocked_code: str) -> None:
    decision = AgentCommandPolicy().decide(command)

    assert decision.action == "forbidden"
    assert decision.blocked_syntax["code"] == blocked_code


def test_allow_amendment_cannot_unblock_network_or_shell_syntax() -> None:
    policy = AgentCommandPolicy.from_rule_payload(
        {
            "schema": "grounded.agent_exec_policy.v1",
            "amendments": [
                {
                    "id": "allow_curl_for_test",
                    "decision": "allow",
                    "prefixes": [["curl"]],
                    "reason": "This must not override network deny.",
                },
                {
                    "id": "allow_redirect_for_test",
                    "decision": "allow",
                    "prefixes": [["cat"]],
                    "reason": "This must not override shell syntax deny.",
                },
            ],
        }
    )

    curl = policy.decide("curl https://example.com")
    redirect = policy.decide("cat miniapp/app/main.py > /tmp/out")

    assert curl.action == "forbidden"
    assert curl.network_policy["code"] == "direct_network_tool"
    assert redirect.action == "forbidden"
    assert redirect.blocked_syntax["code"] == "shell_metacharacter"
