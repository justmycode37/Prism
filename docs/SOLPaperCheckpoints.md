# Prospective paper checkpoints

The checkpoint and benchmark modules are experimental paper-only building
blocks. When an audit is explicitly activated, the continuous runtime attempts
scheduled paper checkpoints; it does not activate a run or obtain index or FX
observations on its own. The authenticated paper-run control report includes
the audit, checkpoint and benchmark sections. The audit section and benchmark
section use separate read snapshots; benchmark and checkpoints share one. Their local implementation identity
is part of the frozen SOL behavior manifest, so changing these components
requires a new qualified identity. None of these records qualifies a trader.

`capture_paper_checkpoint(audit, run_id, scheduled_ms, snapshot_id)` reads the
paper account, quote head, adapter orders, fills, and funding under one SQLite
write transaction. It compares the canonical book with the adapter's indexed
ledger and reconstructs cash, positions, fees and funding using
`reconcile_paper_account`. A mismatch, unresolved decision, changed run binding,
or late book event rejects capture. A mark must be on the registered schedule
and captured within two seconds. At the initial boundary the isolated starting
book must be empty; its USDT equity can be captured with `snapshot_id=None`, so
a quote is not required at the exact start millisecond. Later observations need
an in-run, uncorrected snapshot that matches the current paper quote and is no
more than ten seconds old. A mutation at the scheduled millisecond is treated
as ambiguous and rejected. A subsequent same-millisecond audit event also
invalidates the stored observation. The collector retries only within that
window; a missing quote, pending decision or delayed worker leaves the mark
missing instead of reconstructing it later.

The local checkpoint record is immutable and hash-bound to the run manifest;
it stores accounting results and digests of the book and ledgers, not a full
replayable copy of their historical contents. It cannot prove that the ledgers
or funding schedule are complete. Simulator fills do not establish live fill
quality, and local hashes and clocks do not authenticate a provider.

`record_benchmark_mark` records caller-supplied S&P 500 total-return and
USD-per-USDT observations from the sources frozen in the run manifest. It
requires the index source time to equal a scheduled mark and the FX time to
fall within the declared age. Receipt is limited to the following 24 hours;
this preserves the declared reporting window but does not authenticate an
observation or prevent a caller from selecting a source revision. Corrections
are appended with `record_benchmark_correction`; they preserve the original
mark and prevent its use in a comparison.

The local control API exposes these operations as `record_mark` and
`correct_mark` actions on `POST /v1/sol-paper-run`. The former requires exactly
`run_id`, `scheduled_ms`, `index`, and `fx`; each observation contains `value`,
`source`, `source_time_ms`, and `source_record_id`. The latter requires
`run_id`, `scheduled_ms`, `correction_id`, and `reason`. These are operator
declarations, not authenticated source data. No provider is contacted.
The `report` action returns at most 100 audit events by default. Set an integer
`event_limit` from 1 through 1000 and use the returned `next_after_seq` as
`after_seq` to request another page. Run-level checks still inspect the complete
history on every page; long runs therefore remain expensive to validate.

`paper_benchmark_report` reads checkpoint and benchmark records in one database
snapshot. It produces a descriptive aligned comparison only when all scheduled
local marks and decision evidence pass its checks and timestamps are whole
seconds and no external paper orders are present. USD trading equity includes
the caller-supplied FX conversion. No operating costs are deducted, no annualization or hypothesis test is implied,
and source, funding and execution completeness remain unverified. Both reports
always set `performance_evidence_complete` and `eligible` to false.
