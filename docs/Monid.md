# Optional Monid evidence

Prism can import Search1API news and single-video YouTube transcripts through Monid. Public RSS/Atom and HTML acquisition continue through the runtime’s ordinary collectors. The normalized external records join the same bounded evidence pool at the next collection cycle, so analysts and upper layers can receive their original text and provenance.

The connector key is loaded only on the host from `PRISM_MONID_KEY_FILE`, `MONID_API_KEY`, or the private `.prism-runtime/secrets/monid.key` file. Credential files must belong to the current user, be regular files, and have no group/other permissions. Keys are excluded from model payloads and runtime state.

## Explicit acquisition

The command previews its one bounded request unless `--execute` is supplied. It does not start the runtime or schedule more calls. Use the intended journal path; the examples deliberately use an isolated temporary journal.

```sh
# Preview only: no credentials loaded and no request sent.
python3 scripts/monid_research.py --journal /tmp/prism-research.sqlite3 \
  --news "semiconductor capacity announcements"

# Explicitly execute one paid news query, at most five results, no article crawls.
python3 scripts/monid_research.py --journal /tmp/prism-research.sqlite3 \
  --news "semiconductor capacity announcements" --execute

# Explicitly execute one paid transcript request.
python3 scripts/monid_research.py --journal /tmp/prism-research.sqlite3 \
  --video "https://www.youtube.com/watch?v=VIDEO_ID_HERE" --execute

# Retrieve a previously accepted asynchronous run once; never repost it.
python3 scripts/monid_research.py --journal /tmp/prism-research.sqlite3 \
  --run-id RUN_ID_FROM_RECEIPT

# Import an exported run response entirely offline.
python3 scripts/monid_research.py --journal /tmp/prism-research.sqlite3 \
  --receipt /path/to/monid-run-response.json
```

Requests are fixed to the verified news and transcript contracts, with request/response byte limits, deadlines, no redirects, and no automatic retry. A failed or blocked run supplies no evidence. `COMPLETED` alone is insufficient: provider HTTP errors and malformed outputs are rejected. An uncertain transport failure is not automatically retried because the provider may already have accepted the paid request.

Receipts preserve run ID, input, status, provider output, and supplied cost. Sources retain their URL, publication time, first local availability, content hash, source family, import reference, and cost provenance. YouTube feed descriptions stay metadata; only actual transcript text is labeled a transcript. Imported publication dates never backdate first availability.

The queue keeps the latest 256 evidence IDs. Each candidate is conservatively charged the full cost of each distinct imported run it cites, once per run, in prospective operating economics. Missing or non-USD costs prevent qualification. These charges are separate from the UI’s model-call budget summary. No autonomous paid Monid loop is enabled.

Contracts checked against [Monid OpenAPI](https://monid.ai/openapi.json), [Search1API news](https://s1.dev/docs/basic/news), and [the transcript provider](https://apify.com/starvibe/youtube-video-transcript) on October 30, 2026. Provider results and pricing may change; exported fixtures test parsing without making paid calls.
