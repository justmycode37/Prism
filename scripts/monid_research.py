#!/usr/bin/env python3
"""Fetch one explicitly requested Monid news/transcript run, or import a receipt.

No service starts. Paid POSTs require --execute. Pending runs are never reposted;
use --run-id to retrieve their result once. Transport failures are not retried.
"""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from prism_runtime.credentials import get_monid_key
from prism_runtime.monid import MonidClient, MonidError, MonidNewsRequest, MonidYouTubeTranscriptRequest, MonidRunReceipt
from prism_runtime.monid_sources import retain_receipt
from prism_runtime.store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--journal', required=True, type=Path)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--news', help='One news query (at most five results, no crawl).')
    group.add_argument('--video', help='One YouTube URL for a transcript.')
    group.add_argument('--run-id', help='Retrieve one existing run without posting another.')
    group.add_argument('--receipt', type=Path, help='Import an exported Monid run JSON offline.')
    parser.add_argument('--execute', action='store_true', help='Explicitly authorize the one paid POST.')
    args = parser.parse_args()
    try:
        if args.receipt:
            if args.receipt.stat().st_size > 1_000_000:
                raise ValueError('Receipt exceeds 1 MB.')
            response = json.loads(args.receipt.read_text())
            if not isinstance(response, dict) or not all(isinstance(response.get(k), str) and response[k] for k in ('runId', 'status', 'provider', 'endpoint')):
                raise ValueError('Invalid Monid run receipt.')
            receipt = MonidRunReceipt(response)
        else:
            client = MonidClient(get_monid_key)
            if args.run_id:
                receipt = client.get_run(args.run_id)
            else:
                plan = MonidNewsRequest(args.news) if args.news else MonidYouTubeTranscriptRequest(args.video)
                if not args.execute:
                    print(json.dumps({'status': 'preview', 'paid_requests': 0, 'request': plan.body(), 'execute_required': True}))
                    return 0
                receipt = client.run_batch([plan], enabled=True)[0]
        result = retain_receipt(Store(args.journal), receipt)
        print(json.dumps(result, allow_nan=False))
        return 0
    except (MonidError, ValueError, OSError, KeyError, TypeError) as error:
        print('Monid research: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
