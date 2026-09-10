from __future__ import annotations

import argparse
import json
from pathlib import Path

from first_party_graph_network import ExecutionRefused, candidate_identity, execute_once


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-identity", action="store_true")
    parser.add_argument("--authority")
    parser.add_argument("--authority-sha256")
    parser.add_argument("--protocol")
    parser.add_argument("--protocol-sha256")
    parser.add_argument("--candidate-sha256")
    parser.add_argument("--execution-handoff-sha256")
    parser.add_argument("--execution-authority-context-sha256")
    parser.add_argument("--state-root")
    arguments = parser.parse_args()

    if arguments.candidate_identity:
        if any(
            value is not None
            for value in (
                arguments.authority,
                arguments.authority_sha256,
                arguments.protocol,
                arguments.protocol_sha256,
                arguments.candidate_sha256,
                arguments.execution_handoff_sha256,
                arguments.execution_authority_context_sha256,
                arguments.state_root,
            )
        ):
            print(
                _json(
                    {
                        "schema": "quantresearch-first-party-graph-launch/v1",
                        "terminal_code": "ARGUMENTS_INVALID",
                    }
                )
            )
            return 2
        identity = candidate_identity()
        print(
            _json(
                {
                    "schema": "quantresearch-first-party-graph-candidate-identity/v1",
                    "offline_controller_sha256": identity.offline_controller_sha256,
                    "network_adapter_sha256": identity.network_adapter_sha256,
                    "launcher_sha256": identity.launcher_sha256,
                    "candidate_sha256": identity.candidate_sha256,
                    "next_stage_authorization": "NOT_AUTHORIZED",
                }
            )
        )
        return 0

    required = (
        arguments.authority,
        arguments.authority_sha256,
        arguments.protocol,
        arguments.protocol_sha256,
        arguments.candidate_sha256,
        arguments.execution_handoff_sha256,
        arguments.execution_authority_context_sha256,
        arguments.state_root,
    )
    if any(value is None for value in required):
        print(
            _json(
                {
                    "schema": "quantresearch-first-party-graph-launch/v1",
                    "terminal_code": "ARGUMENTS_INVALID",
                }
            )
        )
        return 2

    try:
        outcome = execute_once(
            authority_path=Path(arguments.authority),
            protocol_path=Path(arguments.protocol),
            state_root=Path(arguments.state_root),
            expected_protocol_sha256=arguments.protocol_sha256,
            expected_candidate_sha256=arguments.candidate_sha256,
            expected_authority_sha256=arguments.authority_sha256,
            expected_execution_handoff_sha256=arguments.execution_handoff_sha256,
            expected_execution_authority_context_sha256=(
                arguments.execution_authority_context_sha256
            ),
        )
    except ExecutionRefused as exc:
        print(
            _json(
                {"schema": "quantresearch-first-party-graph-launch/v1", "terminal_code": exc.code}
            )
        )
        return 1
    if outcome.graph.public_result is None:
        print(
            _json(
                {
                    "schema": "quantresearch-first-party-graph-launch/v1",
                    "terminal_code": outcome.graph.terminal_code,
                }
            )
        )
        return 1
    print(_json(outcome.graph.public_result))
    return 0 if outcome.graph.terminal_code != "CLAIM_ALREADY_CONSUMED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
