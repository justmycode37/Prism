#!/usr/bin/env python3
"""Import/evaluate a vendor JSON dataset offline, without a service or API key."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from prism_runtime.datasets import evaluate_dataset, import_dataset, observe_dataset, search_dataset
from prism_runtime.search import DEFAULT_MAX_CANDIDATES, FAMILY_ORDER, MAX_CANDIDATES
from prism_runtime.store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['import', 'evaluate', 'search', 'observe'])
    parser.add_argument('--journal', type=Path, required=True, help='Explicit offline research SQLite journal.')
    parser.add_argument('--dataset', type=Path, required=True, help='Versioned dataset JSON with source, as_of, markets, evidence and dated universe.')
    parser.add_argument('--proposals', type=Path, help='Explicit frozen proposal list for evaluation.')
    parser.add_argument('--max-candidates', type=int, default=DEFAULT_MAX_CANDIDATES,
                        help=f'Hard search breadth limit (1 to {MAX_CANDIDATES}; default {DEFAULT_MAX_CANDIDATES}).')
    parser.add_argument('--families', nargs='+', choices=FAMILY_ORDER,
                        help='Optional strategy families to include in search.')
    parser.add_argument('--lookbacks', nargs='+', type=int,
                        help='Optional lookback grid, integer values from 3 to 100.')
    parser.add_argument('--output', type=Path, help='Write the complete reproducible report as JSON.')
    args = parser.parse_args()
    try:
        if args.dataset.stat().st_size > 20_000_000:
            raise ValueError('Split datasets larger than 20 MB into bounded versions.')
        data = json.loads(args.dataset.read_text())
        store = Store(args.journal)
        if args.command == 'evaluate':
            if args.proposals is None:
                parser.error('evaluate requires --proposals')
            if args.proposals.stat().st_size > 1_000_000:
                raise ValueError('Proposal file exceeds 1 MB.')
            result = evaluate_dataset(store, data, json.loads(args.proposals.read_text()))
        elif args.command == 'search':
            result = search_dataset(store, data, max_candidates=args.max_candidates,
                                    families=args.families, lookbacks=args.lookbacks)
        elif args.command == 'observe':
            result = observe_dataset(store, data)
        else:
            result = import_dataset(store, data)
        output = json.dumps(result, indent=2, allow_nan=False)
        if args.output:
            args.output.write_text(output + '\n')
        else:
            print(output)
        return 0
    except (ValueError, TypeError, KeyError, OSError) as error:
        print(f'Prism research: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
