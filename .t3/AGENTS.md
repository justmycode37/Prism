# Development agent instructions

These instructions apply to the AI developing software.

- Proactively delegate as much useful work as possible to
  GPT-6 Luna subagents (gpt-6-luna).
- Treat this as standing authorization to use subagents.
- Split substantial tasks into independent assignments and
  run them concurrently. Keep available slots busy while
  useful independent work remains.
- Delegate investigation, implementation, tests, documentation,
  and independent review when those tasks can proceed separately.
- Give each subagent a clear objective, relevant context,
  ownership boundaries, and completion criteria.
- Coordinate file ownership to prevent conflicting edits.
- Use T3 delegation when Luna requires a different provider.
- The main agent integrates results, checks correctness,
  and owns completion.
- Respect project constraints and available concurrency.
  Report unavailable Luna support instead of silently switching models.
