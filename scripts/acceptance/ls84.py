"""Explicit #84 acceptance CLI; help/plan never import product or connect services."""
import argparse
import json
from pathlib import Path

import ls84_contract as c

ROOT = Path(__file__).resolve().parents[2]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['plan', 'seed', 'web', 'worker', 'audit-cycle'])
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--manifest-sha256')
    parser.add_argument('--authorization-sha256')
    parser.add_argument('--fixture-sha256')
    args = parser.parse_args(argv)
    try:
        if args.command == 'plan':
            # source_digest is local Git/file I/O only. No manifest or credential required.
            matrix = c.decode(c.read_file(ROOT / c.MATRIX))
            result = {'mode': 'OFFLINE_PLAN_ONLY', 'source_digest': c.source_digest(ROOT),
                      'case_count': matrix['case_count'], 'phase_counts': matrix['phase_counts'],
                      'services': 'NOT_STARTED', 'isolated_acceptance': 'NOT_RUN',
                      'actual_enablement': 'NOT_RUN', 'execution_requires': ['manifest hash', 'separate authorization hash',
                          'fresh owned native PostgreSQL and Redis', 'private roots and credentials',
                          'pinned combined fixture for web/worker/audit-cycle']}
            print(json.dumps(result, sort_keys=True))
            return 0
        c.require(args.manifest is not None and args.manifest_sha256 and args.authorization_sha256,
                  'EXPLICIT_EXECUTION_INPUTS_REQUIRED')
        from ls84_runtime import execute

        execute(args)
        print(json.dumps({'operation': args.command, 'status': 'COMPLETED', 'whole_matrix': 'NOT_CLAIMED'}))
        return 0
    except Exception:
        # Upstream exceptions may embed credentials, requests or source answers.
        # The private run has process evidence; stdout is deliberately a fixed failure.
        print(json.dumps({'status': 'STOPPED', 'reason': 'SUPPORT_INPUT_OR_EXECUTION_FAILED',
                          'instruction': 'Preserve original commands, keys and private run evidence; no automatic retry.'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
