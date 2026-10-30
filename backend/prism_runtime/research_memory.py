"""Durable strategy dossiers and a recursively triggered review queue.

The memory owns only its queue and revision-link tables. Canonical experiments,
development evaluations, captured forward observations, strategies, evidence,
and immutable input snapshots remain in :class:`Store`'s existing tables.
This module never calls a model, collector, market source, or execution path.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import re
import time


REVIEW_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'required': ['outcome', 'conclusion', 'rationale', 'evidence_assessment',
                 'unresolved_questions', 'plan_changes', 'continuation_summary',
                 'next_review', 'revision'],
    'properties': {
        'outcome': {'type': 'string', 'enum': ['observe', 'more_data', 'revise', 'retire']},
        'conclusion': {'type': 'string', 'maxLength': 4000},
        'rationale': {'type': 'string', 'maxLength': 8000},
        'evidence_assessment': {
            'type': 'object', 'additionalProperties': False,
            'required': ['summary', 'supporting_ids', 'contradicting_ids', 'missing_ids'],
            'properties': {
                'summary': {'type': 'string', 'maxLength': 4000},
                'supporting_ids': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 128},
                'contradicting_ids': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 128},
                'missing_ids': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 128},
            },
        },
        'unresolved_questions': {'type': 'array', 'items': {'type': 'string', 'maxLength': 1000}, 'maxItems': 32},
        'plan_changes': {'type': 'array', 'items': {'type': 'string', 'maxLength': 2000}, 'maxItems': 32},
        'continuation_summary': {'type': 'string', 'maxLength': 4000},
        'next_review': {
            'type': 'object', 'additionalProperties': False,
            'required': ['trigger', 'after_sessions', 'after_seconds'],
            'properties': {
                'trigger': {'type': 'string', 'enum': ['initial_evaluation', 'session_checkpoint', 'trade_checkpoint', 'failure', 'risk', 'new_evidence', 'scheduler', 'manual', 'none']},
                'after_sessions': {'type': ['integer', 'null'], 'minimum': 1},
                'after_seconds': {'type': ['integer', 'null'], 'minimum': 1},
            },
        },
        'revision': {
            'oneOf': [
                {'type': 'null'},
                {
                    'type': 'object', 'additionalProperties': False,
                    'required': ['proposal', 'policy'],
                    'properties': {
                        'proposal': {'type': 'object'},
                        'policy': {'type': ['object', 'null']},
                    },
                },
            ],
        },
    },
}

_TRIGGERS = {
    'initial_evaluation', 'session_checkpoint', 'trade_checkpoint', 'failure',
    'risk', 'new_evidence', 'scheduler', 'manual',
}
_DEFAULT_THRESHOLDS = {
    'scheduler_interval_seconds': 86400,
    'session_interval': 20,
    'trade_interval': 1,
    'risk_drawdown_fraction': .10,
    'max_evidence_documents': 64,
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _text(value, limit=4000):
    return str(value or '')[:limit]


def _finite_number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _int_setting(value, name, minimum=1, maximum=1_000_000):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f'{name} must be an integer from {minimum} to {maximum}')
    return value


def _normalise_thresholds(values=None, base=None):
    allowed = set(_DEFAULT_THRESHOLDS)
    if values is not None and (not isinstance(values, dict) or set(values) - allowed):
        raise ValueError('Unknown ResearchMemory threshold')
    if base is not None and (not isinstance(base, dict) or set(base) - allowed):
        raise ValueError('Unknown ResearchMemory threshold')
    result = {**_DEFAULT_THRESHOLDS, **(base or {}), **(values or {})}
    for key, maximum in (('scheduler_interval_seconds', 31_536_000), ('session_interval', 2520),
                         ('trade_interval', 100_000), ('max_evidence_documents', 256)):
        result[key] = _int_setting(result[key], key, maximum=maximum)
    drawdown = result['risk_drawdown_fraction']
    if not _finite_number(drawdown) or not 0 < drawdown <= .10:
        raise ValueError('risk_drawdown_fraction must be greater than zero and at most 0.10')
    return result


class ResearchMemory:
    """Build durable dossiers and manage review work without collector coupling.

    ``clock`` is an injectable callable returning epoch seconds. ``thresholds``
    configures trigger intervals; a scan may supply per-call overrides.
    """

    def __init__(self, store, clock=None, *, thresholds=None):
        if clock is not None and not callable(clock):
            raise ValueError('clock must be callable')
        self.store = store
        self.clock = clock or getattr(store, '_clock', None) or time.time
        self.thresholds = _normalise_thresholds(thresholds)
        with self.store.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS research_memory_reviews (
                    id TEXT PRIMARY KEY,
                    target_id TEXT NOT NULL,
                    experiment_id TEXT,
                    strategy_id TEXT,
                    trigger TEXT NOT NULL,
                    task TEXT NOT NULL,
                    input_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    claimed_at REAL,
                    completed_at REAL,
                    request TEXT NOT NULL,
                    result TEXT,
                    application TEXT,
                    UNIQUE(target_id, input_fingerprint)
                );
                CREATE INDEX IF NOT EXISTS idx_research_memory_queue
                    ON research_memory_reviews(status, priority DESC, created_at, id);
                CREATE INDEX IF NOT EXISTS idx_research_memory_experiment
                    ON research_memory_reviews(experiment_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS research_memory_revisions (
                    id TEXT PRIMARY KEY,
                    review_id TEXT NOT NULL,
                    parent_experiment_id TEXT NOT NULL,
                    child_experiment_id TEXT,
                    spec_hash TEXT,
                    policy_hash TEXT,
                    status TEXT NOT NULL,
                    proposal TEXT NOT NULL,
                    evaluation TEXT,
                    created_at REAL NOT NULL,
                    UNIQUE(parent_experiment_id, spec_hash, policy_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_research_memory_revision_parent
                    ON research_memory_revisions(parent_experiment_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_research_memory_revision_child
                    ON research_memory_revisions(child_experiment_id);
            ''')

    def _now(self):
        value = self.clock()
        if not _finite_number(value) or value < 0:
            raise ValueError('ResearchMemory clock must return finite nonnegative epoch seconds')
        return float(value)

    def recover(self):
        """Called once by the process owner; completed responses remain immutable."""
        with self.store.connection() as db:
            db.execute("UPDATE research_memory_reviews SET status='queued',claimed_at=NULL WHERE status='claimed'")

    def defer_review(self, review_id, reason, *, retry_at=None):
        with self.store.connection() as db:
            row = db.execute('SELECT request FROM research_memory_reviews WHERE id=?', (review_id,)).fetchone()
            if row is None:
                raise ValueError('Unknown review')
            request = json.loads(row['request'])
            request.update(deferred_reason=str(reason)[:1000], retry_at=retry_at)
            db.execute('UPDATE research_memory_reviews SET status=?,request=? WHERE id=? AND status=\'claimed\'',
                       ('deferred' if retry_at is not None else 'blocked', _json(request), review_id))

    def record_execution(self, strategy_id, decision, *, positions=(), orders=(), fills=(), risk=None):
        _, _, experiment = self._lookup(strategy_id)
        key = 'strategy_execution_' + experiment['id']
        with self.store._lock:
            context = self.store.get_document(key, {})
            history = {row['id']: row for row in context.get('decisions', []) if row.get('id')}
            reconciled = decision.get('status') == 'reconciled'
            if decision.get('id') and not reconciled:
                history[decision['id']] = deepcopy(decision)
            previous = deepcopy(context)
            context.update(decisions=list(history.values())[-32:], positions=list(positions)[:32],
                           orders=list(orders)[-64:], fills=list(fills)[-64:])
            if risk is not None:
                context['risk'] = deepcopy(risk)
            if not reconciled:
                context['continuation_summary'] = str(decision.get('rationale', decision.get('reason', decision.get('summary', ''))))[:1200]
            if context == previous:
                return
            context['updated_at_ms'] = round(self._now() * 1000)
            self.store.set_document(key, context)

    def _thresholds(self, values=None):
        config = self.store.config()
        research = config.get('research', {}) if isinstance(config, dict) else {}
        operations = config.get('operations', {}) if isinstance(config, dict) else {}
        base = {
            'scheduler_interval_seconds': research.get('memory_review_interval_seconds',
                research.get('discovery_interval_seconds', self.thresholds['scheduler_interval_seconds'])),
            'session_interval': operations.get('review_sessions',
                research.get('review_interval_sessions', self.thresholds['session_interval'])),
            'trade_interval': operations.get('review_trades',
                research.get('memory_trade_interval', self.thresholds['trade_interval'])),
            'risk_drawdown_fraction': research.get('max_forward_drawdown', self.thresholds['risk_drawdown_fraction']),
            'max_evidence_documents': self.thresholds['max_evidence_documents'],
        }
        return _normalise_thresholds(values, base)

    def _lookup(self, target_id):
        if not isinstance(target_id, str) or not target_id.strip():
            raise ValueError('A strategy or experiment identifier is required')
        target_id = target_id.strip()
        strategy = None
        with self.store.connection() as db:
            row = db.execute('SELECT value FROM cascade_strategies WHERE id=?', (target_id,)).fetchone()
        if row:
            strategy = json.loads(row['value'])
        experiment_id = strategy.get('experiment_id') if strategy else target_id
        experiment = self.store.experiment(experiment_id) if experiment_id else None
        if experiment is None and strategy:
            experiment_id = strategy.get('id', '').removeprefix('strategy_')
            experiment = self.store.experiment(experiment_id)
        if experiment is None:
            raise ValueError(f'Unknown strategy or experiment: {target_id}')
        if strategy is None:
            with self.store.connection() as db:
                rows = db.execute('SELECT value FROM cascade_strategies WHERE json_extract(value,\'$.experiment_id\')=? ORDER BY rowid DESC',
                                  (experiment['id'],)).fetchall()
            strategy = json.loads(rows[0]['value']) if rows else None
        return target_id, strategy, experiment

    @staticmethod
    def _policy_identity(spec, policy):
        canon = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
        return hashlib.sha256((canon(spec) + canon(policy)).encode('utf-8')).hexdigest()

    def _revision_tree_ids(self, root_id):
        ids, frontier = {root_id}, [root_id]
        while frontier:
            current = frontier.pop()
            with self.store.connection() as db:
                rows = db.execute('SELECT child_experiment_id FROM research_memory_revisions '
                                  'WHERE parent_experiment_id=? AND child_experiment_id IS NOT NULL',
                                  (current,)).fetchall()
            for row in rows:
                child = row['child_experiment_id']
                if child and child not in ids:
                    ids.add(child)
                    frontier.append(child)
        return ids

    def _revision_root_id(self, experiment_id):
        current, seen = experiment_id, set()
        while current not in seen:
            seen.add(current)
            with self.store.connection() as db:
                row = db.execute('SELECT parent_experiment_id FROM research_memory_revisions '
                                 'WHERE child_experiment_id=? ORDER BY created_at,id LIMIT 1',
                                 (current,)).fetchone()
            if row is None or row['parent_experiment_id'] in seen:
                return current
            current = row['parent_experiment_id']
        return current

    def _related_reviews(self, experiment_ids, target_id):
        placeholders = ','.join('?' for _ in experiment_ids) or "''"
        with self.store.connection() as db:
            rows = db.execute(f'SELECT * FROM research_memory_reviews WHERE target_id=? OR experiment_id IN ({placeholders}) ORDER BY created_at DESC,id DESC',
                              (target_id, *sorted(experiment_ids))).fetchall()
        return [dict(row) for row in rows]

    def _revision_rows(self, experiment_ids):
        placeholders = ','.join('?' for _ in experiment_ids) or "''"
        with self.store.connection() as db:
            rows = db.execute(f'SELECT * FROM research_memory_revisions WHERE parent_experiment_id IN ({placeholders}) OR child_experiment_id IN ({placeholders}) ORDER BY created_at DESC,id DESC',
                              (*sorted(experiment_ids), *sorted(experiment_ids))).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _review_summary(row):
        result = json.loads(row['result']) if row.get('result') else {}
        app = json.loads(row['application']) if row.get('application') else None
        if isinstance(app, dict):
            app = {key: app.get(key) for key in (
                'outcome', 'status', 'experiment_id', 'parent_experiment_id',
                'revision_id', 'reason', 'fresh_forward_gate_required') if key in app}
        compact = {
            'id': row['id'], 'review_id': row['id'], 'target_id': row['target_id'],
            'experiment_id': row['experiment_id'], 'strategy_id': row['strategy_id'],
            'priority': row['priority'], 'trigger': row['trigger'], 'status': row['status'],
            'created_at': row['created_at'], 'outcome': result.get('outcome'),
            'conclusion': _text(result.get('conclusion'), 1600),
            'rationale': _text(result.get('rationale'), 1600),
            'unresolved_questions': result.get('unresolved_questions', [])[:16],
            'plan_changes': result.get('plan_changes', [])[:16],
            'continuation_summary': _text(result.get('continuation_summary'), 1600),
            'application': app,
        }
        request = json.loads(row['request']) if row.get('request') else {}
        compact.update(deferred_reason=request.get('deferred_reason'), retry_at=request.get('retry_at'))
        return compact

    def _evidence_for(self, identifiers, symbol, registered_at_ms):
        identifiers = list(dict.fromkeys(x for x in identifiers if isinstance(x, str) and x))[:512]
        evidence = self.store.evidence(identifiers) if identifiers else []
        rows = []
        for item in evidence:
            rows.append({key: item.get(key) for key in (
                'id', 'title', 'source_url', 'source_kind', 'source_family',
                'published_at', 'observed_at', 'content_hash', 'symbols', 'content') if key in item})
        rows.sort(key=lambda x: (x.get('id') not in identifiers, x.get('id', '')))
        return rows

    @staticmethod
    def _evaluation_parts(evaluation):
        if not isinstance(evaluation, dict):
            return {}
        return evaluation.get('edge') or evaluation.get('report') or evaluation

    def _bybit_forward_data(self, experiment):
        """Read Bybit's host-captured daily proofs for dossier context only."""
        if (not isinstance(experiment.get('policy'), dict)
            or experiment['policy'].get('version') != 2
            or experiment['policy'].get('venue') != 'bybit'):
            return [], {}
        with self.store.connection() as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='live_research_v2_days'").fetchone()
            if not exists:
                return [], {}
        # Use the same immutable-provenance and correction checks as promotion;
        # a dossier must not resurrect a rejected or legacy forward mark.
        from .live_research import LiveResearch
        research = LiveResearch(self.store, clock=self.clock)
        return research.observations(experiment['id']), research.report(experiment['id'])

    def _dossier_full(self, target_id, task):
        requested, strategy, experiment = self._lookup(target_id)
        spec = experiment['spec']
        policy = experiment['policy']
        context = experiment.get('context') or {}
        evaluations = experiment.get('evaluations', [])
        development = [row for row in evaluations if row.get('phase') == 'development']
        forward = [row for row in evaluations if row.get('phase') == 'forward']
        latest_dev = development[-1] if development else None
        latest_forward = forward[-1] if forward else None
        dev_report = self._evaluation_parts(latest_dev) if latest_dev else {}
        forward_report = latest_forward.get('report', {}) if latest_forward else {}

        evidence_ids = list(context.get('evidence_ids', []))
        dataset_evidence_ids = list(context.get('dataset_evidence_ids', []))
        for evaluation in development:
            report = self._evaluation_parts(evaluation)
            evidence_ids.extend(report.get('evidence_ids', []))
            dataset_evidence_ids.extend(report.get('dataset_evidence_ids', []))
        evidence_ids = list(dict.fromkeys(x for x in evidence_ids if isinstance(x, str)))
        dataset_evidence_ids = list(dict.fromkeys(x for x in dataset_evidence_ids if isinstance(x, str)))
        evidence = self._evidence_for(evidence_ids, spec.get('symbol'), experiment['registered_at_ms'])
        source_families = sorted({str(item.get('source_family') or item.get('source_url') or item.get('source_kind') or 'unknown') for item in evidence})

        tree_ids = self._revision_tree_ids(experiment['id'])
        reviews = self._related_reviews(tree_ids, requested)
        completed_reviews = [row for row in reviews if row.get('result')]
        review_summaries = [self._review_summary(row) for row in completed_reviews]
        revision_rows = self._revision_rows(tree_ids)
        revisions = []
        for row in revision_rows:
            revisions.append({
                'revision_id': row['id'], 'review_id': row['review_id'],
                'parent_experiment_id': row['parent_experiment_id'],
                'child_experiment_id': row['child_experiment_id'],
                'spec_hash': row['spec_hash'], 'policy_hash': row['policy_hash'],
                'status': row['status'], 'proposal': json.loads(row['proposal']),
                'evaluation': json.loads(row['evaluation']) if row['evaluation'] else None,
                'created_at': row['created_at'],
            })

        observations = experiment.get('observations', [])
        bybit_observations, bybit_forward_report = self._bybit_forward_data(experiment)
        forward_observations = bybit_observations or observations
        attested = (sum(row.get('provenance', {}).get('status') == 'attested' for row in observations)
                    + len(bybit_observations))
        current_status = experiment['status']
        policy_version = policy.get('version') if isinstance(policy, dict) else None
        policy_frequency = policy.get('frequency') if isinstance(policy, dict) else None
        bybit_v2 = isinstance(policy, dict) and policy.get('version') == 2 and policy.get('venue') == 'bybit'
        daily_gate = policy_version == 1 and policy_frequency == 'daily'
        terminal = current_status in {'retired', 'rejected'}
        context_parent = context.get('parent_experiment_id') or context.get('parent_id') or next((
            row.get('parent_experiment_id') for row in revision_rows
            if row.get('child_experiment_id') == experiment['id']), None)
        costs = {
            'research_cost_usd': self.store.research_cost(experiment['id']),
            'forward_research_cost_usd': sum(float(row.get('research_cost_usd', 0) or 0) for row in forward_observations if _finite_number(row.get('research_cost_usd'))),
            'forward_data_cost_usd': sum(float(row.get('data_cost_usd', 0) or 0) for row in forward_observations if _finite_number(row.get('data_cost_usd'))),
            'forward_infrastructure_cost_usd': sum(float(row.get('infrastructure_cost_usd', 0) or 0) for row in forward_observations if _finite_number(row.get('infrastructure_cost_usd'))),
        }
        report_metrics = dev_report.get('metrics', {}) if isinstance(dev_report, dict) else {}
        if not report_metrics and isinstance(dev_report, dict):
            report_metrics = dev_report
        if bybit_v2 and bybit_forward_report:
            forward_report = bybit_forward_report
        result = {
            'schema_version': 1,
            'target': {'requested_id': requested, 'strategy_id': strategy.get('id') if strategy else None,
                       'experiment_id': experiment['id'], 'parent_experiment_id': context_parent},
            'identity': {
                'canonical_spec': spec,
                'spec_hash': _digest(spec),
                'policy': policy,
                'policy_hash': _digest(policy),
                'rule_policy_identity': self._policy_identity(spec, policy),
                'policy_version': policy_version, 'policy_frequency': policy_frequency,
                'experiment_ordinal': experiment.get('ordinal'),
                'registered_at_ms': experiment['registered_at_ms'],
                'version_rule': 'Canonical rule plus exact frozen execution policy; any rule or policy change registers a separate experiment.',
            },
            'hypothesis': {
                'statement': _text(context.get('hypothesis') or (strategy or {}).get('name'), 4000),
                'mechanism': _text(context.get('mechanism'), 3000),
                'invalidation': _text(context.get('invalidation'), 2000),
            },
            'evidence_lineage': {
                'evidence_ids': evidence_ids,
                'dataset_evidence_ids': dataset_evidence_ids,
                'source_families': source_families,
                'input_snapshot': context.get('input_snapshot') or (latest_dev or {}).get('input_snapshot'),
                'information_cutoff_ms': context.get('information_cutoff_ms'),
                'documents': evidence,
            },
            'lifecycle': {
                'experiment_status': current_status,
                'strategy_status': strategy.get('status') if strategy else None,
                'registered_at_ms': experiment['registered_at_ms'],
                'status_history': experiment.get('status_history', [])[-16:],
                'revision_of': context_parent,
                'revision_review_id': context.get('revision_review_id'),
                'revision_number': context.get('revision_number'),
            },
            'eligibility': {
                'historical_results_are_development_only': True,
                'forward_observations': len(forward_observations),
                'attested_forward_observations': attested,
                'prospective_gate_state': ('retired' if terminal else
                    'fresh_forward_evidence_required' if daily_gate else
                    'venue_specific_host_gate_required' if bybit_v2 else 'unsupported_policy_for_current_forward_analytics'),
                'live_eligible': False,
                'reason': ('Current analytics support the frozen daily policy for prospective shadow review; development results do not pass that gate.'
                           if daily_gate else 'Bybit v2 results are context only; separate host verification and eligibility rules remain in force.'
                           if bybit_v2 else 'Policy identity is retained, but current analytics do not evaluate this policy version.'),
            },
            'results': {
                'development': {
                    'evaluation_count': len(development),
                    'latest_evaluation_id': latest_dev.get('id') if latest_dev else None,
                    'status': dev_report.get('status') or ('supported' if dev_report.get('supported') else 'insufficient' if bybit_v2 else None),
                    'reason': _text(dev_report.get('reason'), 3000),
                    'net_return': report_metrics.get('net_return'),
                    'train_net_return': report_metrics.get('train_net_return'),
                    'max_drawdown': report_metrics.get('max_drawdown'),
                    'trades': report_metrics.get('trades'),
                    'metrics': report_metrics,
                    'benchmarks': dev_report.get('benchmarks'),
                    'data_fingerprint': (latest_dev.get('data_fingerprint') or latest_dev.get('input_fingerprint')) if latest_dev else None,
                    'trial_count': (latest_dev.get('trial_count') if latest_dev else None) or report_metrics.get('trial_count'),
                    'alpha_budget': latest_dev.get('alpha_budget') if latest_dev else None,
                },
                'forward': {
                    'evaluation_count': len(forward),
                    'latest_evaluation_id': latest_forward.get('id') if latest_forward else None,
                    'report': forward_report,
                    'observation_count': len(forward_observations),
                    'latest_session_ms': forward_observations[-1].get('session_ms') if forward_observations else None,
                    'completed_trades': forward_observations[-1].get('completed_trades') if forward_observations else 0,
                    'risk_halted': bool(forward_observations[-1].get('risk_halted')) if forward_observations else False,
                },
                'costs': costs,
                'benchmarks': forward_report.get('baseline_returns', dev_report.get('benchmarks')) if isinstance(forward_report, dict) else dev_report.get('benchmarks'),
                'data_quality': {
                    'data_fingerprints': list(dict.fromkeys(row.get('data_fingerprint') for row in evaluations if row.get('data_fingerprint')))[-16:],
                    'forward_attestation_complete': bool(forward_observations) and attested == len(forward_observations),
                    'unattested_forward_observations': len(forward_observations) - (attested if bybit_v2 else min(attested, len(forward_observations))),
                    'missing_baselines': forward_report.get('missing_baselines', []),
                    'review_reasons': forward_report.get('reasons', []),
                    'development_only': True,
                },
            },
            'previous_reviews': review_summaries,
            'rejected_revisions': [row for row in revisions if row.get('status') in {'rejected', 'evaluation_rejected', 'branch_limit'}],
            'revisions': revisions,
            'unresolved_questions': list(dict.fromkeys(
                question for row in review_summaries for question in row.get('unresolved_questions', []) if isinstance(question, str)))[:64],
            'plan_changes': [change for row in reversed(review_summaries) for change in row.get('plan_changes', []) if isinstance(change, str)][-64:],
            'execution_context': {
                'task': task if isinstance(task, str) else 'review',
                'mode': 'paper_research',
                'policy_hash': _digest(policy),
                'risk_halted': bool(observations[-1].get('risk_halted')) if observations else False,
                'strategy_validation': strategy.get('validation') if strategy else None,
                'parent_active_state_preserved': bool(context_parent),
            },
            'continuation_summary': next((row['continuation_summary'] for row in review_summaries if row['continuation_summary']), ''),
            'truncation': {'truncated': False, 'omitted_sections': [], 'original_bytes': None, 'returned_bytes': None},
        }
        execution_context = self.store.get_document('strategy_execution_' + experiment['id'], {})
        result['execution_context'].update(execution_context)
        # Tool inspection results can contain entire market packets. Durable
        # history keeps them; a dossier carries decisions and outcomes, not
        # another copy of every prior OHLCV response.
        decisions = execution_context.get('decisions', [])
        if decisions:
            result['execution_context']['decisions'] = [{
                **{key: row.get(key) for key in ('id', 'status', 'snapshot_id', 'created_at_ms', 'expires_at_ms')},
                'rationale': _text(row.get('rationale'), 600),
                'actions': [{'tool': item.get('name'), 'status': (item.get('result') or {}).get('status')}
                            for item in row.get('result', {}).get('tools', [])[:8]]
            } for row in decisions[-8:]]
            self._omit(result, 'execution_context.decisions', 'latest eight decision summaries; full tool payloads remain in the execution journal')
        last_request = json.loads(completed_reviews[0]['request']) if completed_reviews and isinstance(completed_reviews[0].get('request'), str) else {}
        previous = last_request.get('dossier', {}).get('results', {})
        result['changes_since_previous_decision'] = {
            'additional_observations': len(forward_observations) - previous.get('forward', {}).get('observation_count', 0),
            'latest_development_evaluation': latest_dev.get('id') if latest_dev else None,
            'execution_updated_at_ms': execution_context.get('updated_at_ms'),
        }
        return result

    @staticmethod
    def _encoded_size(value):
        return len(_json(value).encode('utf-8'))

    @staticmethod
    def _omit(payload, path, action):
        payload['truncation']['truncated'] = True
        payload['truncation']['omitted_sections'].append({'section': path, 'action': action})

    @staticmethod
    def _compact_core(payload):
        identity = payload['identity']
        development = payload['results']['development']
        forward = payload['results']['forward']
        forward_report = forward.get('report') if isinstance(forward.get('report'), dict) else {}
        prior = payload['previous_reviews'][:1]
        if prior:
            prior = [{key: prior[0].get(key) for key in (
                'review_id', 'outcome', 'conclusion')}]
        revisions = [{key: row.get(key) for key in (
            'revision_id', 'review_id', 'parent_experiment_id', 'child_experiment_id', 'spec_hash',
            'policy_hash', 'status')} for row in payload['revisions'][:1]]
        compact = {
            'schema_version': payload['schema_version'],
            'target': {key: payload['target'].get(key) for key in ('strategy_id', 'experiment_id', 'parent_experiment_id')},
            'identity': {
                'canonical_spec': identity['canonical_spec'], 'spec_hash': identity['spec_hash'],
                'policy': {'truncated': True}, 'policy_hash': identity['policy_hash'],
                'rule_policy_identity': identity['rule_policy_identity'],
                'policy_version': identity['policy_version'], 'policy_frequency': identity['policy_frequency'],
            },
            'hypothesis': {key: _text(value, 220 if key == 'statement' else 120)
                           for key, value in payload['hypothesis'].items()},
            'evidence_lineage': {
                'evidence_ids': payload['evidence_lineage']['evidence_ids'][:8],
                'dataset_evidence_ids': payload['evidence_lineage']['dataset_evidence_ids'][:4],
                'source_families': payload['evidence_lineage']['source_families'][:3],
                'documents': [],
            },
            'lifecycle': {key: payload['lifecycle'].get(key) for key in (
                'experiment_status', 'strategy_status', 'revision_of', 'revision_number')},
            'eligibility': {key: payload['eligibility'].get(key) for key in (
                'historical_results_are_development_only', 'forward_observations',
                'attested_forward_observations', 'prospective_gate_state', 'live_eligible')},
            'results': {
                'development': {key: development.get(key) for key in (
                    'status', 'net_return', 'max_drawdown', 'trades', 'trial_count')},
                'forward': {key: value for key, value in {
                    'observation_count': forward.get('observation_count'),
                    'completed_trades': forward.get('completed_trades'),
                    'risk_halted': forward.get('risk_halted'),
                    'status': forward_report.get('status'), 'net_return': forward_report.get('net_return'),
                    'excess_return': forward_report.get('excess_return'),
                    'operating_cost_usd': forward_report.get('operating_cost_usd'),
                    'max_drawdown': forward_report.get('max_drawdown')}.items() if value is not None},
                'costs': payload['results']['costs'],
                'benchmarks': None,
            },
            'previous_reviews': prior,
            'rejected_revisions': [{key: row.get(key) for key in (
                'revision_id', 'status', 'spec_hash', 'policy_hash')} for row in payload['rejected_revisions'][:1]],
            'revisions': revisions,
            'unresolved_questions': [_text(value, 140) for value in payload['unresolved_questions'][:4]],
            'plan_changes': [_text(value, 160) for value in payload['plan_changes'][-2:]],
            'execution_context': {'task': payload['execution_context'].get('task'),
                'policy_hash': identity['policy_hash'],
                'parent_active_state_preserved': payload['execution_context'].get('parent_active_state_preserved'),
                'continuation_summary': _text(payload['execution_context'].get('continuation_summary'), 220),
                'risk': payload['execution_context'].get('risk'),
                'latest_decision': (payload['execution_context'].get('decisions') or [None])[-1]},
            'continuation_summary': _text(payload.get('continuation_summary'), 220),
            'truncation': {'truncated': True, 'omitted_sections': ['compact_core'],
                           'original_bytes': payload['truncation']['original_bytes'], 'returned_bytes': 0},
        }
        def prune(value):
            if isinstance(value, dict):
                return {key: pruned for key, item in value.items()
                        if (pruned := prune(item)) not in (None, '', [], {})}
            if isinstance(value, list):
                return [pruned for item in value if (pruned := prune(item)) not in (None, '', [], {})]
            return value
        return prune(compact)

    def dossier(self, strategy_or_experiment_id, task='review', max_bytes=16000):
        """Return a bounded JSON-safe dossier, preserving identity and last conclusions."""
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 512:
            raise ValueError('max_bytes must be an integer of at least 512')
        payload = self._dossier_full(strategy_or_experiment_id, task)
        original_size = self._encoded_size(payload)
        payload['truncation']['original_bytes'] = original_size
        if original_size + 32 <= max_bytes:
            for _ in range(4):
                size = self._encoded_size(payload)
                if payload['truncation']['returned_bytes'] == size:
                    break
                payload['truncation']['returned_bytes'] = size
            return payload

        # Keep the current rule/policy, outcome, and latest prior conclusion;
        # trim source passages and old detail before removing those core fields.
        passage_trimmed = False
        for item in payload['evidence_lineage']['documents']:
            content = item.get('content')
            if isinstance(content, str) and len(content) > 700:
                item['content'] = content[:700] + '…'
                passage_trimmed = True
        if passage_trimmed:
            self._omit(payload, 'evidence_lineage.documents', 'source passages limited to 700 characters')

        while self._encoded_size(payload) > max_bytes and len(payload['previous_reviews']) > 1:
            payload['previous_reviews'].pop()
            self._omit(payload, 'previous_reviews', 'older review summaries removed; latest conclusion retained')
        while self._encoded_size(payload) > max_bytes and len(payload['revisions']) > 1:
            payload['revisions'].pop()
            payload['rejected_revisions'] = [row for row in payload['revisions'] if row.get('status') in {'rejected', 'evaluation_rejected', 'branch_limit'}]
            self._omit(payload, 'revisions', 'older revision detail removed')
        if self._encoded_size(payload) > max_bytes and payload['evidence_lineage']['documents']:
            payload['evidence_lineage']['documents'] = payload['evidence_lineage']['documents'][:4]
            self._omit(payload, 'evidence_lineage.documents', 'document details limited to four; full evidence ids retained')
        if self._encoded_size(payload) + 64 > max_bytes:
            payload['results']['development']['metrics'] = {
                key: payload['results']['development']['metrics'].get(key)
                for key in ('net_return', 'adjusted_p_value', 'trial_count', 'trades', 'max_drawdown')
                if key in payload['results']['development']['metrics']
            }
            payload['results']['development']['benchmarks'] = None
            self._omit(payload, 'results.development', 'kept key screening metrics; removed detailed benchmark report')
        if self._encoded_size(payload) > max_bytes:
            payload['evidence_lineage']['documents'] = []
            self._omit(payload, 'evidence_lineage.documents', 'document bodies removed; evidence ids and source families retained')
        if self._encoded_size(payload) > max_bytes:
            for name, limit in (('decisions', 2), ('orders', 8), ('fills', 8)):
                rows = payload['execution_context'].get(name, [])
                if len(rows) > limit:
                    payload['execution_context'][name] = rows[-limit:]
                    self._omit(payload, 'execution_context.' + name,
                               str(len(rows) - limit) + ' older records omitted; latest outcomes retained')
        if self._encoded_size(payload) > max_bytes and len(payload['evidence_lineage']['evidence_ids']) > 24:
            omitted_ids = len(payload['evidence_lineage']['evidence_ids']) - 24
            payload['evidence_lineage']['evidence_ids'] = payload['evidence_lineage']['evidence_ids'][:24]
            payload['evidence_lineage']['omitted_evidence_ids'] = omitted_ids
            self._omit(payload, 'evidence_lineage.evidence_ids', 'lineage list bounded to 24 ids')
        if self._encoded_size(payload) > max_bytes:
            for key, limit in (('rationale', 500), ('conclusion', 800), ('continuation_summary', 600)):
                for row in payload['previous_reviews']:
                    if len(row.get(key, '')) > limit:
                        row[key] = row[key][:limit] + '…'
                if key == 'continuation_summary' and len(payload.get(key, '')) > limit:
                    payload[key] = payload[key][:limit] + '…'
            self._omit(payload, 'previous_reviews', 'long narrative summaries shortened; latest conclusion retained')
        if self._encoded_size(payload) > max_bytes:
            # Large execution policies may be retained by identity even when
            # a caller asks for a small packet. The digest remains explicit.
            policy = payload['identity']['policy']
            encoded_policy = _json(policy)
            keep = max(256, max_bytes // 8)
            payload['identity']['policy'] = {'truncated_json_prefix': encoded_policy[:keep],
                                             'truncated': True,
                                             'sha256': payload['identity']['policy_hash']}
            self._omit(payload, 'identity.policy', 'policy body shortened; exact policy hash retained')
        if self._encoded_size(payload) > max_bytes:
            # A conclusion is a continuation anchor; keep at least the latest
            # summary and questions while dropping older review details.
            latest = payload['previous_reviews'][:1]
            if latest:
                latest[0] = {key: latest[0].get(key) for key in (
                    'review_id', 'trigger', 'status', 'outcome', 'conclusion', 'continuation_summary')}
            payload['previous_reviews'] = latest
            payload['plan_changes'] = payload['plan_changes'][-8:]
            payload['unresolved_questions'] = payload['unresolved_questions'][-8:]
            payload['revisions'] = payload['revisions'][:1]
            payload['rejected_revisions'] = payload['rejected_revisions'][:1]
            self._omit(payload, 'history', 'history limited to latest review and revision')
        size = self._encoded_size(payload)
        if size > max_bytes:
            payload = self._compact_core(payload)
            size = self._encoded_size(payload)
        if size > max_bytes:
            if payload['previous_reviews']:
                payload['previous_reviews'][0]['conclusion'] = _text(payload['previous_reviews'][0].get('conclusion'), max(120, max_bytes // 5))
                if 'continuation_summary' in payload['previous_reviews'][0]:
                    payload['previous_reviews'][0]['continuation_summary'] = _text(payload['previous_reviews'][0].get('continuation_summary'), 120)
            payload['continuation_summary'] = _text(payload.get('continuation_summary'), 120)
            payload['hypothesis']['statement'] = _text(payload['hypothesis'].get('statement'), 160)
            payload['unresolved_questions'] = payload['unresolved_questions'][:3]
            payload['plan_changes'] = payload['plan_changes'][:2]
            payload['evidence_lineage']['evidence_ids'] = payload['evidence_lineage']['evidence_ids'][:8]
            payload['evidence_lineage']['dataset_evidence_ids'] = payload['evidence_lineage']['dataset_evidence_ids'][:4]
            payload['truncation']['omitted_sections'] = ['compact_core']
            size = self._encoded_size(payload)
        if size > max_bytes:
            raise ValueError(f'max_bytes={max_bytes} is too small for the dossier core ({size} bytes)')
        for _ in range(4):
            size = self._encoded_size(payload)
            if payload['truncation']['returned_bytes'] == size:
                break
            payload['truncation']['returned_bytes'] = size
        if self._encoded_size(payload) > max_bytes:
            raise ValueError(f'max_bytes={max_bytes} is too small for the dossier core')
        return payload

    def _material(self, target_id):
        _, strategy, experiment = self._lookup(target_id)
        observations = experiment.get('observations', [])
        bybit_observations, _ = self._bybit_forward_data(experiment)
        if bybit_observations:
            observations = bybit_observations
        evaluations = experiment.get('evaluations', [])
        latest_observation = observations[-1] if observations else {}
        latest_evaluation = evaluations[-1] if evaluations else {}
        report = self._evaluation_parts(latest_evaluation)
        return {
            'experiment_id': experiment['id'], 'spec': experiment['spec'], 'policy': experiment['policy'],
            'status': experiment['status'], 'strategy_status': strategy.get('status') if strategy else None,
            'latest_evaluation_id': latest_evaluation.get('id'),
            'latest_evaluation_phase': latest_evaluation.get('phase'),
            'latest_evaluation_status': report.get('status'),
            'latest_evaluation_reason': report.get('reason'),
            'latest_session_ms': latest_observation.get('session_ms'),
            'latest_completed_trades': latest_observation.get('completed_trades', 0),
            'latest_risk_halted': latest_observation.get('risk_halted', False),
            'observation_count': len(observations),
            'execution_updated_at_ms': self.store.get_document('strategy_execution_' + experiment['id'], {}).get('updated_at_ms'),
        }

    def enqueue_review(self, strategy_or_experiment_id, *, trigger='manual', task='review',
                       material=None, material_fingerprint=None, priority=0, thresholds=None):
        """Persist one review unless the same target/material/trigger was seen."""
        if trigger not in _TRIGGERS:
            raise ValueError('Unknown review trigger')
        if not isinstance(task, str) or not task.strip() or len(task) > 200:
            raise ValueError('task must be a nonempty string of at most 200 characters')
        if isinstance(priority, bool) or not isinstance(priority, int) or not -1000 <= priority <= 1000:
            raise ValueError('priority must be an integer from -1000 to 1000')
        target_id, strategy, experiment = self._lookup(strategy_or_experiment_id)
        selected_thresholds = self._thresholds(thresholds)
        body = self._material(target_id) if material is None else material
        if not isinstance(body, (dict, list, str, int, float, bool, type(None))):
            raise ValueError('Review material must be JSON-compatible')
        if len(_json(body).encode('utf-8')) > 512_000:
            raise ValueError('Review material exceeds the 512 KB durable queue limit')
        if material_fingerprint is None:
            fingerprint = _digest({'trigger': trigger, 'task': task, 'material': body,
                                   'thresholds': selected_thresholds})
        else:
            if not isinstance(material_fingerprint, str) or not material_fingerprint:
                raise ValueError('material_fingerprint must be a nonempty string')
            fingerprint = _digest({'trigger': trigger, 'task': task,
                                   'material_fingerprint': material_fingerprint,
                                   'thresholds': selected_thresholds})
        review_id = 'review_' + _digest([target_id, fingerprint])[:32]
        dossier = self.dossier(target_id, task=task, max_bytes=48000)
        request = {'schema_version': 1, 'review_id': review_id, 'target_id': target_id,
                   'experiment_id': experiment['id'], 'strategy_id': strategy.get('id') if strategy else None,
                   'trigger': trigger, 'task': task, 'input_fingerprint': fingerprint,
                   'thresholds': selected_thresholds, 'material': body, 'dossier': dossier,
                   'response_schema': REVIEW_SCHEMA}
        now = self._now()
        with self.store.connection() as db:
            db.execute('INSERT OR IGNORE INTO research_memory_reviews '
                       '(id,target_id,experiment_id,strategy_id,trigger,task,input_fingerprint,status,priority,created_at,request) '
                       'VALUES (?,?,?,?,?,?,?,\'queued\',?,?,?)',
                       (review_id, target_id, experiment['id'], strategy.get('id') if strategy else None,
                        trigger, task, fingerprint, priority, now, _json(request)))
            row = db.execute('SELECT * FROM research_memory_reviews WHERE target_id=? AND input_fingerprint=?',
                             (target_id, fingerprint)).fetchone()
        return self._public_review(row)

    @staticmethod
    def _public_review(row):
        if row is None:
            return None
        return {
            'id': row['id'], 'target_id': row['target_id'], 'experiment_id': row['experiment_id'],
            'strategy_id': row['strategy_id'], 'trigger': row['trigger'], 'task': row['task'],
            'input_fingerprint': row['input_fingerprint'], 'status': row['status'],
            'priority': row['priority'], 'created_at': row['created_at'], 'claimed_at': row['claimed_at'],
            'completed_at': row['completed_at'],
            'request': json.loads(row['request']),
            'result': json.loads(row['result']) if row['result'] else None,
            'application': json.loads(row['application']) if row['application'] else None,
        }

    def scan_triggers(self, strategy_or_experiment_id=None, *, thresholds=None, task='review', evidence_ids=None, include_elapsed=True,
                      exclude_execution_mode=None):
        """Discover reviews from registry state, captured sessions, risk, and raw evidence."""
        limits = self._thresholds(thresholds)
        if strategy_or_experiment_id:
            candidates = [self._lookup(strategy_or_experiment_id)[2]['id']]
        else:
            with self.store.connection() as db:
                candidates = [row['id'] for row in db.execute('SELECT id FROM experiments ORDER BY ordinal DESC LIMIT 10000')]
        queued = []
        now = self._now()
        all_evidence = (self.store.evidence(list(evidence_ids), limit=4000)
                        if evidence_ids is not None else self.store.evidence(limit=4000))
        from .temporal import evidence_as_of
        eligible_evidence = evidence_as_of(all_evidence, round(now * 1000))
        for experiment_id in candidates:
            experiment = self.store.experiment(experiment_id)
            if not experiment:
                continue
            if exclude_execution_mode and (experiment['policy'].get('execution_identity') or {}).get('mode') == exclude_execution_mode:
                continue
            spec = experiment['spec']
            context = experiment.get('context') or {}
            observations = experiment.get('observations', [])
            bybit_observations, bybit_report = self._bybit_forward_data(experiment)
            if bybit_observations:
                observations = bybit_observations
            evaluations = experiment.get('evaluations', [])
            latest_eval = evaluations[-1] if evaluations else None
            latest_report = self._evaluation_parts(latest_eval) if latest_eval else {}
            current = self._material(experiment_id)

            execution = self.store.get_document('strategy_execution_' + experiment_id, {})
            execution_risk = execution.get('risk', {})
            if execution_risk.get('risk_halted'):
                queued.append(self.enqueue_review(experiment_id, trigger='risk', task=task,
                    material={'source': 'execution', 'risk_halted': True,
                              'halt_reasons': execution_risk.get('halt_reasons', [])},
                    priority=100, thresholds=limits))
            for order in execution.get('orders', []):
                if order.get('status') == 'rejected':
                    queued.append(self.enqueue_review(experiment_id, trigger='failure', task=task,
                        material={'source': 'execution', 'order_link_id': order.get('order_link_id'),
                                  'status': 'rejected', 'reason': order.get('reason')},
                        priority=30, thresholds=limits))

            if latest_eval:
                first_eval = next((row for row in evaluations if row.get('phase') == 'development'), latest_eval)
                first_report = self._evaluation_parts(first_eval)
                queued.append(self.enqueue_review(experiment_id, trigger='initial_evaluation', task=task,
                    material={'evaluation_id': first_eval.get('id'), 'phase': first_eval.get('phase'),
                              'status': first_report.get('status'), 'data_fingerprint': first_eval.get('data_fingerprint')},
                    thresholds=limits))

            failure = (latest_report.get('status') in {'rejected', 'retired', 'inconclusive'}
                       or latest_report.get('supported') is False
                       or experiment['status'] in {'rejected', 'retired', 'inconclusive'})
            if failure:
                queued.append(self.enqueue_review(experiment_id, trigger='failure', task=task,
                    material={'evaluation_id': latest_eval.get('id') if latest_eval else None,
                              'status': latest_report.get('status') or experiment['status'],
                              'reason': latest_report.get('reason') or latest_report.get('reasons')},
                    priority=20, thresholds=limits))

            count = len(observations)
            session_bucket = count // limits['session_interval']
            if session_bucket:
                queued.append(self.enqueue_review(experiment_id, trigger='session_checkpoint', task=task,
                    material={'session_bucket': session_bucket, 'session_count': count,
                              'latest_session_ms': observations[-1].get('session_ms')},
                    material_fingerprint='sessions:' + str(session_bucket),
                    thresholds=limits))

            trades = observations[-1].get('completed_trades', 0) if observations else 0
            trade_bucket = trades // limits['trade_interval']
            if trade_bucket:
                queued.append(self.enqueue_review(experiment_id, trigger='trade_checkpoint', task=task,
                    material={'trade_bucket': trade_bucket, 'completed_trades': trades,
                              'latest_session_ms': observations[-1].get('session_ms')},
                    material_fingerprint='trades:' + str(trade_bucket),
                    thresholds=limits))

            risk_report = next((row.get('report', {}) for row in reversed(evaluations)
                                if row.get('phase') == 'forward' and isinstance(row.get('report'), dict)), {})
            if bybit_report:
                risk_report = bybit_report
            drawdown = risk_report.get('max_drawdown')
            risk_halted = bool(observations[-1].get('risk_halted')) if observations else False
            if risk_halted or (_finite_number(drawdown) and drawdown >= limits['risk_drawdown_fraction']):
                queued.append(self.enqueue_review(experiment_id, trigger='risk', task=task,
                    material={'risk_halted': risk_halted, 'max_drawdown': drawdown,
                              'threshold': limits['risk_drawdown_fraction'],
                              'latest_session_ms': observations[-1].get('session_ms') if observations else None},
                    priority=100, thresholds=limits))

            with self.store.connection() as db:
                prior_rows = db.execute("SELECT * FROM research_memory_reviews WHERE experiment_id=? AND status='applied' "
                                        'AND result IS NOT NULL '
                                        'ORDER BY created_at DESC,id DESC LIMIT 50', (experiment_id,)).fetchall()
            latest_prior = prior_rows[0] if prior_rows else None
            schedule_from = experiment['registered_at_ms'] / 1000
            scheduler_enabled = True
            if latest_prior:
                schedule_from = latest_prior['completed_at'] or latest_prior['created_at']
                latest_plan = json.loads(latest_prior['result']).get('next_review', {})
                scheduler_enabled = (latest_plan.get('trigger') == 'scheduler'
                    and latest_plan.get('after_sessions') is None
                    and latest_plan.get('after_seconds') is None)
            scheduler_bucket = int(max(0, now - schedule_from) // limits['scheduler_interval_seconds'])
            if (include_elapsed and scheduler_enabled and scheduler_bucket > 0
                and experiment['status'] not in {'retired', 'rejected'}):
                queued.append(self.enqueue_review(experiment_id, trigger='scheduler', task=task,
                    material={'scheduler_bucket': scheduler_bucket,
                              'schedule_anchor_review_id': latest_prior['id'] if latest_prior else None,
                              'latest_session_ms': observations[-1].get('session_ms') if observations else None,
                              'latest_evaluation_id': current.get('latest_evaluation_id')},
                    thresholds=limits))

            for prior in prior_rows[:1]:
                prior_result = json.loads(prior['result'])
                followup = prior_result.get('next_review', {})
                trigger = followup.get('trigger')
                if trigger not in _TRIGGERS or trigger == 'manual':
                    continue
                request = json.loads(prior['request'])
                baseline_sessions = request.get('dossier', {}).get('results', {}).get('forward', {}).get('observation_count', 0)
                due_sessions = followup.get('after_sessions')
                due_seconds = followup.get('after_seconds')
                session_due = (isinstance(due_sessions, int) and not isinstance(due_sessions, bool)
                               and count >= baseline_sessions + due_sessions)
                since = prior['completed_at'] or prior['created_at']
                time_due = (isinstance(due_seconds, int) and not isinstance(due_seconds, bool)
                            and now >= since + due_seconds)
                if session_due or time_due:
                    queued.append(self.enqueue_review(experiment_id, trigger=trigger,
                        task=prior['task'] or task,
                        material={'followup_of': prior['id'], 'due_sessions': due_sessions,
                                  'due_seconds': due_seconds,
                                  'session_due': session_due, 'time_due': time_due},
                        thresholds=limits))

            used = set(context.get('evidence_ids', []))
            used.update(context.get('dataset_evidence_ids', []))
            new_docs = []
            symbol = str(spec.get('symbol', '')).upper()
            for item in eligible_evidence:
                ident = item.get('id')
                if (not isinstance(ident, str) or not ident or ident in used
                    or item.get('available_at_ms', 0) <= experiment['registered_at_ms']):
                    continue
                tags = {str(value).upper() for value in item.get('symbols', []) if isinstance(value, str)}
                tags.update(str(item.get(key, '')).upper() for key in ('symbol', 'ticker') if item.get(key))
                body = ' '.join(str(item.get(key, '')) for key in ('title', 'content', 'summary'))
                if symbol not in tags and not re.search(r'(?<![A-Z0-9])\$?' + re.escape(symbol) + r'(?![A-Z0-9])', body.upper()):
                    continue
                new_docs.append(item)
            new_docs = sorted(new_docs, key=lambda row: row.get('id', ''))[:limits['max_evidence_documents']]
            if new_docs:
                material = [{'id': row['id'], 'title': row.get('title'),
                             'source_url': row.get('source_url'), 'source_kind': row.get('source_kind'),
                             'source_family': row.get('source_family'), 'published_at': row.get('published_at'),
                             'observed_at': row.get('observed_at'),
                             'content_hash': row.get('content_hash') or _digest(row.get('content', '')),
                             'content': _text(row.get('content'), 3000)} for row in new_docs]
                queued.append(self.enqueue_review(experiment_id, trigger='new_evidence', task=task,
                    material={'evidence': material, 'target_symbol': symbol}, thresholds=limits))
        return queued

    def claim_review(self, *, exclude_execution_mode=None):
        """Atomically claim the highest-priority queued review, if any."""
        now = self._now()
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            query = "SELECT id FROM research_memory_reviews WHERE (status='queued' OR (status='deferred' AND json_extract(request,'$.retry_at')<=?))"
            params = [now]
            if exclude_execution_mode:
                query += " AND experiment_id NOT IN (SELECT id FROM experiments WHERE json_extract(policy,'$.execution_identity.mode')=?)"
                params.append(exclude_execution_mode)
            row = db.execute(query + ' ORDER BY priority DESC,created_at,id LIMIT 1', params).fetchone()
            if row is None:
                return None
            db.execute("UPDATE research_memory_reviews SET status='claimed',claimed_at=? WHERE id=? AND status IN ('queued','deferred')",
                       (now, row['id']))
            claimed = db.execute('SELECT * FROM research_memory_reviews WHERE id=?', (row['id'],)).fetchone()
        result = self._public_review(claimed)
        if not result['request'].get('invocation_dossier_frozen'):
            # Queued work may precede another completed review. Freeze its
            # actual invocation dossier now, then keep it stable on recovery.
            result['request']['dossier'] = self.dossier(result['target_id'], task=result['task'], max_bytes=48000)
            result['request']['invocation_dossier_frozen'] = True
            with self.store.connection() as db:
                db.execute('UPDATE research_memory_reviews SET request=? WHERE id=?', (_json(result['request']), result['id']))
        return result

    @staticmethod
    def _validate_review(result):
        if not isinstance(result, dict):
            raise ValueError('Review result must be an object matching REVIEW_SCHEMA')
        required = set(REVIEW_SCHEMA['required'])
        if set(result) != required:
            raise ValueError('Review result fields must exactly match REVIEW_SCHEMA')
        if result['outcome'] not in {'observe', 'more_data', 'revise', 'retire'}:
            raise ValueError('Review outcome must be observe, more_data, revise, or retire')
        for name, limit in (('conclusion', 4000), ('rationale', 8000), ('continuation_summary', 4000)):
            if not isinstance(result[name], str) or len(result[name]) > limit:
                raise ValueError(f'{name} must be a string of at most {limit} characters')
        evidence = result['evidence_assessment']
        if not isinstance(evidence, dict) or set(evidence) != {'summary', 'supporting_ids', 'contradicting_ids', 'missing_ids'}:
            raise ValueError('evidence_assessment must contain summary and source ID lists')
        if not isinstance(evidence['summary'], str) or len(evidence['summary']) > 4000:
            raise ValueError('evidence_assessment.summary is invalid')
        for name in ('supporting_ids', 'contradicting_ids', 'missing_ids'):
            values = evidence[name]
            if not isinstance(values, list) or len(values) > 128 or any(not isinstance(item, str) or len(item) > 240 for item in values):
                raise ValueError(f'evidence_assessment.{name} must be a bounded list of strings')
        for name, max_items in (('unresolved_questions', 32), ('plan_changes', 32)):
            values = result[name]
            max_len = 1000 if name == 'unresolved_questions' else 2000
            if not isinstance(values, list) or len(values) > max_items or any(not isinstance(item, str) or len(item) > max_len for item in values):
                raise ValueError(f'{name} must be a bounded list of strings')
        next_review = result['next_review']
        allowed = {'initial_evaluation', 'session_checkpoint', 'trade_checkpoint', 'failure', 'risk', 'new_evidence', 'scheduler', 'manual', 'none'}
        if (not isinstance(next_review, dict) or set(next_review) != {'trigger', 'after_sessions', 'after_seconds'}
            or next_review['trigger'] not in allowed):
            raise ValueError('next_review must match REVIEW_SCHEMA')
        for name in ('after_sessions', 'after_seconds'):
            val = next_review[name]
            if val is not None and (isinstance(val, bool) or not isinstance(val, int) or val < 1):
                raise ValueError(f'next_review.{name} must be a positive integer or null')
        revision = result['revision']
        if result['outcome'] == 'revise':
            if not isinstance(revision, dict) or set(revision) != {'proposal', 'policy'} or not isinstance(revision['proposal'], dict):
                raise ValueError('A revise outcome requires revision.proposal and revision.policy')
            if revision['policy'] is not None and not isinstance(revision['policy'], dict):
                raise ValueError('revision.policy must be an object or null')
            if len(_json(revision).encode('utf-8')) > 64_000:
                raise ValueError('revision exceeds the 64 KB schema limit')
        elif revision is not None:
            raise ValueError('Only a revise outcome may contain a revision')
        _json(result)
        return deepcopy(result)

    def complete_review(self, review_id, result):
        """Persist a structured model/provider result; no model call is made here."""
        clean = self._validate_review(result)
        if isinstance(review_id, dict):
            review_id = review_id.get('id')
        if not isinstance(review_id, str) or not review_id:
            raise ValueError('review_id is required')
        now = self._now()
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM research_memory_reviews WHERE id=?', (review_id,)).fetchone()
            if row is None:
                raise ValueError('Unknown review id')
            if row['status'] in {'completed', 'applied'}:
                saved = json.loads(row['result'])
                if saved != clean:
                    raise ValueError('Completed review results are immutable')
                return self._public_review(row)
            if row['status'] != 'claimed':
                raise ValueError('Only a claimed review can be completed')
            request = json.loads(row['request'])
            dossier_ids = set(request.get('dossier', {}).get('evidence_lineage', {}).get('evidence_ids', []))
            dossier_ids.update(request.get('dossier', {}).get('evidence_lineage', {}).get('dataset_evidence_ids', []))
            material_evidence = request.get('material', {}).get('evidence', []) if isinstance(request.get('material'), dict) else []
            if isinstance(material_evidence, list):
                dossier_ids.update(item['id'] for item in material_evidence
                                   if isinstance(item, dict) and isinstance(item.get('id'), str))
            citations = result['evidence_assessment']
            unknown = (set(citations['supporting_ids']) | set(citations['contradicting_ids'])) - dossier_ids
            if unknown:
                raise ValueError('Review cites evidence IDs absent from its dossier/material: ' + ', '.join(sorted(unknown)[:8]))
            db.execute("UPDATE research_memory_reviews SET status='completed',completed_at=?,result=? WHERE id=?",
                       (now, _json(clean), review_id))
            saved = db.execute('SELECT * FROM research_memory_reviews WHERE id=?', (review_id,)).fetchone()
        return self._public_review(saved)

    def _store_revision_attempt(self, review, parent_id, proposal, policy, *, status, child_id=None,
                                spec_hash=None, policy_hash=None, evaluation=None):
        stable = {'review_id': review['id'], 'parent_experiment_id': parent_id,
                  'proposal': proposal, 'policy': policy}
        revision_id = 'revision_' + _digest(stable)[:32]
        now = self._now()
        with self.store.connection() as db:
            if spec_hash is not None and policy_hash is not None:
                existing = db.execute('SELECT * FROM research_memory_revisions WHERE parent_experiment_id=? AND spec_hash=? AND policy_hash=?',
                                      (parent_id, spec_hash, policy_hash)).fetchone()
                if existing:
                    return dict(existing)
            db.execute('INSERT OR IGNORE INTO research_memory_revisions '
                       '(id,review_id,parent_experiment_id,child_experiment_id,spec_hash,policy_hash,status,proposal,evaluation,created_at) '
                       'VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (revision_id, review['id'], parent_id, child_id, spec_hash, policy_hash, status,
                        _json({'proposal': proposal, 'policy': policy}), _json(evaluation) if evaluation is not None else None, now))
            row = db.execute('SELECT * FROM research_memory_revisions WHERE id=?', (revision_id,)).fetchone()
        return dict(row)

    def _branch_limits(self, parent_id, config):
        research = config.get('research', {}) if isinstance(config, dict) else {}
        operations = config.get('operations', {}) if isinstance(config, dict) else {}
        max_variants = _int_setting(research.get('variants_per_hypothesis', 16), 'variants_per_hypothesis', 1, 128)
        max_variants = min(max_variants, _int_setting(
            operations.get('max_revisions_per_review', max_variants), 'max_revisions_per_review', 1, 64))
        max_candidates = research.get('max_candidates_per_cycle', 256)
        max_shadows = research.get('max_shadow_candidates', 64)
        for value, name, maximum in ((max_candidates, 'max_candidates_per_cycle', 2000),
                                     (max_shadows, 'max_shadow_candidates', 256)):
            _int_setting(value, name, 1, maximum)
        with self.store.connection() as db:
            sibling_count = db.execute('SELECT COUNT(*) FROM research_memory_revisions WHERE parent_experiment_id=?', (parent_id,)).fetchone()[0]
            active_shadow_count = db.execute("SELECT COUNT(*) FROM cascade_strategies WHERE json_extract(value,'$.status') IN ('shadow','paper')").fetchone()[0]
        tree_ids = self._revision_tree_ids(self._revision_root_id(parent_id))
        placeholders = ','.join('?' for _ in tree_ids)
        with self.store.connection() as db:
            total_count = db.execute(f'SELECT COUNT(*) FROM research_memory_revisions WHERE parent_experiment_id IN ({placeholders})',
                                     tuple(sorted(tree_ids))).fetchone()[0]
        reason = None
        if sibling_count >= max_variants:
            reason = 'Parent revision branch reached variants_per_hypothesis limit'
        elif total_count >= max_candidates:
            reason = 'Research revision registry reached max_candidates_per_cycle limit'
        return reason, {'variants_per_hypothesis': max_variants, 'max_candidates_per_cycle': max_candidates,
                        'max_shadow_candidates': max_shadows, 'active_shadow_candidates': active_shadow_count,
                        'shadow_capacity_available': active_shadow_count < max_shadows,
                        'candidate_count_in_revision_tree': total_count}

    @staticmethod
    def _daily_policy_regression(parent, candidate):
        """Reject a daily-policy revision that loosens an existing gate."""
        if candidate.get('version') != 1 or candidate.get('frequency') != 'daily':
            return None
        if parent.get('version') != 1 or parent.get('frequency') != 'daily':
            return None
        smaller_is_safer = ('allocation_fraction', 'max_symbol_fraction', 'max_gross_fraction',
                            'max_participation_fraction', 'max_correlated_fraction',
                            'correlation_ceiling', 'stop_loss_fraction', 'max_drawdown_fraction',
                            'max_quote_age_seconds')
        larger_is_safer = ('fee_bps', 'slippage_bps', 'spread_bps', 'impact_bps')
        for key in smaller_is_safer:
            if _finite_number(parent.get(key)) and _finite_number(candidate.get(key)) and candidate[key] > parent[key]:
                return 'Daily execution policy revision loosens risk bound: ' + key
        for key in larger_is_safer:
            if _finite_number(parent.get(key)) and _finite_number(candidate.get(key)) and candidate[key] < parent[key]:
                return 'Daily execution policy revision lowers modeled cost: ' + key
        if (isinstance(parent.get('max_holding_sessions'), int)
            and isinstance(candidate.get('max_holding_sessions'), int)
            and candidate['max_holding_sessions'] > parent['max_holding_sessions']):
            return 'Daily execution policy revision increases maximum holding duration'
        before, after = parent.get('review_protocol', {}), candidate.get('review_protocol', {})
        for key in ('min_forward_sessions', 'min_forward_trades', 'min_forward_blocks', 'forward_block_sessions'):
            if (isinstance(before.get(key), int) and not isinstance(before.get(key), bool)
                and isinstance(after.get(key), int) and not isinstance(after.get(key), bool)
                and after[key] < before[key]):
                return 'Daily execution policy revision weakens prospective minimum: ' + key
        if (isinstance(before.get('review_interval_sessions'), int)
            and isinstance(after.get('review_interval_sessions'), int)
            and after['review_interval_sessions'] > before['review_interval_sessions']):
            return 'Daily execution policy revision delays prospective review checkpoints'
        if (_finite_number(before.get('max_forward_drawdown'))
            and _finite_number(after.get('max_forward_drawdown'))
            and after['max_forward_drawdown'] > before['max_forward_drawdown']):
            return 'Daily execution policy revision loosens prospective risk bound: max_forward_drawdown'
        for key in ('min_excess_return', 'data_cost_usd_per_day', 'infrastructure_cost_usd_per_day'):
            if _finite_number(before.get(key)) and _finite_number(after.get(key)) and after[key] < before[key]:
                return 'Daily execution policy revision weakens prospective requirement: ' + key
        if before.get('benchmark_symbols') != after.get('benchmark_symbols'):
            return 'Daily execution policy revision cannot remove or replace frozen benchmarks'
        return None

    @staticmethod
    def _config_matching_policy(parent, requested_policy, fallback):
        from . import simulation
        if not isinstance(requested_policy, dict):
            return None
        if requested_policy.get('version') != 1 or requested_policy.get('frequency') != 'daily':
            return None
        context = parent.get('context') or {}
        config = deepcopy(context.get('config') if isinstance(context.get('config'), dict) else fallback)
        config.setdefault('risk', {})
        config.setdefault('research', {})
        risk_keys = ('allocation_fraction', 'max_symbol_fraction', 'max_gross_fraction', 'fee_bps',
                     'slippage_bps', 'spread_bps', 'impact_bps', 'max_participation_fraction',
                     'max_correlated_fraction', 'correlation_ceiling', 'stop_loss_fraction',
                     'max_drawdown_fraction', 'max_quote_age_seconds')
        for key in risk_keys:
            if key in requested_policy:
                config['risk'][key] = requested_policy[key]
        if 'max_holding_sessions' in requested_policy:
            config['research']['max_holding_sessions'] = requested_policy['max_holding_sessions']
        review = requested_policy.get('review_protocol', {})
        if isinstance(review, dict):
            config['research'].update(review)
        try:
            return config if simulation.execution_policy(config) == requested_policy else None
        except (ValueError, TypeError, KeyError):
            return None

    @staticmethod
    def _market_fingerprint(markets):
        simplified = []
        for market in markets:
            simplified.append({key: market.get(key) for key in ('symbol', 'security_id', 'asset_class',
                'exchange_timezone', 'history_adjustment', 'raw_history_basis', 'candles', 'raw_candles',
                'corporate_actions', 'event_features') if key in market})
        return _digest(simplified)

    def _apply_revision(self, review_row, result, markets=None, config=None):
        from . import analytics, simulation
        request = json.loads(review_row['request'])
        parent = self.store.experiment(review_row['experiment_id'])
        if parent is None:
            return {'outcome': 'revise', 'status': 'rejected', 'reason': 'Parent experiment no longer exists.'}
        revision = result.get('revision') or {}
        raw_proposal = deepcopy(revision.get('proposal') or {})
        proposal = deepcopy(raw_proposal)
        raw_policy = revision.get('policy')
        candidate_policy = deepcopy(raw_policy if raw_policy is not None else parent['policy'])
        try:
            spec = analytics.canonical_spec(proposal)
            proposal.update(spec)
            hypothesis = proposal.get('hypothesis')
            if not isinstance(hypothesis, str) or not hypothesis.strip() or len(hypothesis) > 4000:
                raise ValueError('Revision proposal needs a falsifiable hypothesis statement')
            evidence_ids = proposal.get('evidence_ids', parent.get('context', {}).get('evidence_ids', []))
            if (not isinstance(evidence_ids, list) or not evidence_ids or len(evidence_ids) > 128
                or any(not isinstance(item, str) or not item for item in evidence_ids)):
                raise ValueError('Revision proposal needs bounded source evidence lineage')
            evidence_ids = list(dict.fromkeys(evidence_ids))
            found = {row['id'] for row in self.store.evidence(evidence_ids)}
            if set(evidence_ids) - found:
                raise ValueError('Revision cites evidence absent from the Store')
            proposal['evidence_ids'] = evidence_ids
            if not isinstance(candidate_policy, dict):
                raise ValueError('Revision execution policy must be an object')
            _json(candidate_policy)
            candidate_spec_hash = analytics.spec_hash(spec)
            candidate_policy_hash = _digest(candidate_policy)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            saved = self._store_revision_attempt(review_row, parent['id'], raw_proposal, candidate_policy,
                                                  status='rejected', evaluation={'reason': str(exc)})
            return {'outcome': 'revise', 'status': 'rejected', 'revision_id': saved['id'], 'reason': str(exc)}

        if spec == parent['spec'] and candidate_policy == parent['policy']:
            saved = self._store_revision_attempt(review_row, parent['id'], proposal, candidate_policy,
                status='rejected', spec_hash=candidate_spec_hash, policy_hash=candidate_policy_hash,
                evaluation={'reason': 'Revision must change the canonical rule or frozen execution policy.'})
            return {'outcome': 'revise', 'status': 'rejected', 'revision_id': saved['id'],
                    'reason': 'Revision must change the canonical rule or frozen execution policy.'}

        regression = self._daily_policy_regression(parent['policy'], candidate_policy)
        if regression:
            saved = self._store_revision_attempt(review_row, parent['id'], proposal, candidate_policy,
                status='rejected', spec_hash=candidate_spec_hash, policy_hash=candidate_policy_hash,
                evaluation={'reason': regression})
            return {'outcome': 'revise', 'status': 'rejected', 'revision_id': saved['id'], 'reason': regression}

        with self.store.connection() as db:
            existing_revision = db.execute('SELECT * FROM research_memory_revisions '
                'WHERE parent_experiment_id=? AND spec_hash=? AND policy_hash=?',
                (parent['id'], candidate_spec_hash, candidate_policy_hash)).fetchone()
        if existing_revision:
            evaluation = json.loads(existing_revision['evaluation']) if existing_revision['evaluation'] else None
            return {'outcome': 'revise', 'status': existing_revision['status'],
                    'revision_id': existing_revision['id'], 'parent_experiment_id': parent['id'],
                    'experiment_id': existing_revision['child_experiment_id'], 'evaluation': evaluation,
                    'fresh_forward_gate_required': bool(existing_revision['child_experiment_id']),
                    'parent_preserved': True, 'deduplicated': True}

        effective_config = deepcopy(config if config is not None else self.store.config())
        limit_reason, limits = self._branch_limits(parent['id'], effective_config)
        if limit_reason:
            saved = self._store_revision_attempt(review_row, parent['id'], proposal, candidate_policy,
                status='branch_limit', spec_hash=candidate_spec_hash, policy_hash=candidate_policy_hash,
                evaluation={'reason': limit_reason, 'limits': limits})
            return {'outcome': 'revise', 'status': 'rejected', 'revision_id': saved['id'], 'reason': limit_reason, 'limits': limits}

        parent_policy = parent['policy']
        data_fingerprint = self._market_fingerprint(markets) if isinstance(markets, list) and markets else None
        if data_fingerprint is None:
            data_fingerprint = (parent.get('context') or {}).get('data_fingerprint') or _digest({'parent': parent['id'], 'no_revision_data': True})
        child_context = {
            'parent_experiment_id': parent['id'], 'parent_strategy_id': review_row['strategy_id'],
            'revision_review_id': review_row['id'], 'revision_trigger': review_row['trigger'],
            'revision_number': 1 + self._revision_count_for(parent['id']),
            'hypothesis': hypothesis, 'evidence_ids': evidence_ids,
            'parent_evidence_ids': list((parent.get('context') or {}).get('evidence_ids', [])),
            'data_fingerprint': data_fingerprint,
            'historical_phase': 'development', 'revision_candidate': True,
            'source_families': sorted({row.get('source_family') or row.get('source_url') or 'unknown'
                                       for row in self.store.evidence(evidence_ids)}),
        }
        base_config = self._config_matching_policy(parent, candidate_policy, effective_config)
        if base_config is not None:
            child_context['config'] = base_config
        child = self.store.register_experiment(spec, policy=candidate_policy, context=child_context)
        if child['id'] == parent['id']:
            saved = self._store_revision_attempt(review_row, parent['id'], proposal, candidate_policy,
                status='rejected', child_id=None, spec_hash=candidate_spec_hash, policy_hash=candidate_policy_hash,
                evaluation={'reason': 'Canonical revision deduplicated to the parent experiment.'})
            return {'outcome': 'revise', 'status': 'rejected', 'revision_id': saved['id'], 'reason': 'Canonical revision deduplicated to the parent experiment.'}

        report = None
        revision_status = 'registered_awaiting_evaluation'
        evaluation_summary = {'data_fingerprint': data_fingerprint, 'trial_count': None,
                              'alpha_budget': child['alpha_budget'], 'development_only': True,
                              'fresh_forward_gate_required': True, 'limits': limits}
        eval_config = self._config_matching_policy(parent, candidate_policy, effective_config)
        if eval_config is None:
            revision_status = 'stored_unsupported_policy'
            evaluation_summary['reason'] = 'Policy identity is stored, but the current daily analytics do not evaluate this policy version.'
        elif not isinstance(markets, list) or not markets:
            evaluation_summary['reason'] = 'Revision registered without frozen offline market data; evaluation requires an explicit market fixture.'
        else:
            # Freeze the independent input packet and lineage before its result.
            cutoff = min(round(self._now() * 1000), round(time.time() * 1000))
            market_snapshot = self.store.freeze_markets(markets, cutoff)
            evidence = self.store.evidence(evidence_ids)
            input_snapshot = self.store.save_snapshot('experiment_inputs',
                {'market_snapshot': market_snapshot, 'evidence': evidence,
                 'parent_experiment_id': parent['id'], 'review_id': review_row['id']}, cutoff)
            child_context.update(input_snapshot=input_snapshot, market_snapshot=market_snapshot,
                                 information_cutoff_ms=cutoff)
            # Store registration context is immutable; save the snapshot link in
            # the evaluation payload as well, which is the authoritative record.
            with self.store.connection() as db:
                attempt_count = db.execute('SELECT COUNT(*) FROM experiment_attempts').fetchone()[0]
                experiment_count = db.execute('SELECT COUNT(*) FROM experiments').fetchone()[0]
            from .datasets import _dataset_alpha_allocation, _refresh_search_correction
            alpha, dataset_index = _dataset_alpha_allocation(self.store, child, data_fingerprint)
            prior_trials = self.store.get_document('statistical_trial_count', 0)
            trial_count = max(1, attempt_count, experiment_count,
                              prior_trials if isinstance(prior_trials, int) and not isinstance(prior_trials, bool) else 0)
            candidate_cache_key = _digest([data_fingerprint, candidate_spec_hash, candidate_policy_hash])
            cached = next((row for row in child.get('evaluations', [])
                           if row.get('revision_evaluation') and row.get('candidate_cache_key') == candidate_cache_key
                           and isinstance(row.get('report'), dict)), None)
            if cached:
                report = deepcopy(cached['report'])
                _refresh_search_correction(report, trial_count, alpha)
            else:
                report = analytics.validate_edges([proposal], markets, trial_count=trial_count,
                    config=eval_config, alpha_budget=alpha)[0]
                _refresh_search_correction(report, trial_count, alpha)
            report.update(experiment_id=child['id'], parent_experiment_id=parent['id'],
                          revision_review_id=review_row['id'], alpha_budget=alpha,
                          registry_alpha_budget=child['alpha_budget'],
                          dataset_alpha_allocation_index=dataset_index,
                          revision_evaluation=True, development_only=True,
                          fresh_forward_gate_required=True)
            saved_evaluation = self.store.record_evaluation(child['id'], {
                'phase': 'development', 'revision_evaluation': True, 'revision_review_id': review_row['id'],
                'parent_experiment_id': parent['id'], 'data_fingerprint': data_fingerprint,
                'candidate_cache_key': candidate_cache_key, 'cache_hit': bool(cached),
                'input_snapshot': input_snapshot, 'market_snapshot': market_snapshot,
                'information_cutoff_ms': cutoff, 'policy_hash': _digest(candidate_policy),
                'trial_count': trial_count, 'alpha_budget': alpha,
                'registry_alpha_budget': child['alpha_budget'],
                'dataset_alpha_allocation_index': dataset_index, 'report': report,
            })
            evaluation_summary.update(status=report.get('status'), reason=report.get('reason'),
                trial_count=trial_count, alpha_budget=alpha, dataset_alpha_allocation_index=dataset_index,
                evaluation_id=saved_evaluation.get('id'), input_snapshot=input_snapshot,
                metrics=report.get('metrics', {}), benchmarks=report.get('benchmarks'))
            revision_status = 'development_pass_forward_required' if report.get('status') == 'validated' else 'evaluation_rejected'
            if report.get('status') == 'validated':
                self.store.save_strategy({
                    'id': 'review_strategy_' + child['id'] + '_' + review_row['id'][-8:],
                    'experiment_id': child['id'], 'parent_experiment_id': parent['id'],
                    'name': hypothesis[:120], 'symbol': spec['symbol'], 'family': spec['kind'],
                    'status': 'revision_development', 'phase': 'development', 'spec': spec,
                    'policy': candidate_policy, 'policy_hash': _digest(candidate_policy),
                    'registered_at_ms': child['registered_at_ms'], 'evidence_ids': evidence_ids,
                    'validation': {'passed': False, 'development_only': True,
                                   'spec_hash': analytics.spec_hash(spec), 'policy_hash': _digest(candidate_policy)},
                    'forward_eligible': False, 'live_eligible': False,
                    'reason': 'Development screen passed; independent fresh prospective review is still required.',
                })

        saved = self._store_revision_attempt(review_row, parent['id'], proposal, candidate_policy,
            status=revision_status, child_id=child['id'], spec_hash=candidate_spec_hash,
            policy_hash=candidate_policy_hash, evaluation=evaluation_summary)
        return {'outcome': 'revise', 'status': revision_status, 'revision_id': saved['id'],
                'parent_experiment_id': parent['id'], 'experiment_id': child['id'],
                'canonical_spec_hash': candidate_spec_hash, 'policy_hash': candidate_policy_hash,
                'evaluation': evaluation_summary, 'report': report,
                'fresh_forward_gate_required': True, 'parent_preserved': True}

    def _revision_count_for(self, parent_id):
        with self.store.connection() as db:
            return db.execute('SELECT COUNT(*) FROM research_memory_revisions WHERE parent_experiment_id=?',
                              (parent_id,)).fetchone()[0]

    def apply_review(self, review_id, *, markets=None, config=None):
        """Apply a completed outcome; revisions register/evaluate as separate experiments."""
        if isinstance(review_id, dict):
            review_id = review_id.get('id')
        if not isinstance(review_id, str) or not review_id:
            raise ValueError('review_id is required')
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM research_memory_reviews WHERE id=?', (review_id,)).fetchone()
        if row is None:
            raise ValueError('Unknown review id')
        if row['status'] == 'applied':
            return json.loads(row['application'])
        if row['status'] != 'completed' or not row['result']:
            raise ValueError('Only a completed review can be applied')
        result = self._validate_review(json.loads(row['result']))
        outcome = result['outcome']
        if outcome in {'observe', 'more_data'}:
            application = {'outcome': outcome, 'status': 'recorded',
                           'conclusion': result['conclusion'],
                           'next_review': result['next_review'], 'parent_preserved': True}
        elif outcome == 'retire':
            parent = self.store.experiment(row['experiment_id'])
            if parent and parent['status'] not in {'retired', 'rejected'}:
                self.store.update_experiment_status(parent['id'], 'retired', 'ResearchMemory review: ' + result['conclusion'][:500])
            if row['strategy_id']:
                with self.store.connection() as db:
                    saved = db.execute('SELECT value FROM cascade_strategies WHERE id=?', (row['strategy_id'],)).fetchone()
                if saved:
                    strategy = json.loads(saved['value'])
                    strategy.update(status='retired', reason='ResearchMemory review: ' + result['conclusion'][:500])
                    self.store.save_strategy(strategy)
            application = {'outcome': outcome, 'status': 'retired', 'experiment_id': row['experiment_id'],
                           'conclusion': result['conclusion']}
        else:
            application = self._apply_revision(row, result, markets=markets, config=config)
            application['conclusion'] = result['conclusion']
        with self.store.connection() as db:
            db.execute("UPDATE research_memory_reviews SET status='applied',application=? WHERE id=? AND status='completed'",
                       (_json(application), review_id))
        return application

    def state(self):
        """Return queue/revision counts and bounded recent records."""
        with self.store.connection() as db:
            statuses = {row['status']: row['count'] for row in db.execute(
                'SELECT status,COUNT(*) AS count FROM research_memory_reviews GROUP BY status')}
            revision_statuses = {row['status']: row['count'] for row in db.execute(
                'SELECT status,COUNT(*) AS count FROM research_memory_revisions GROUP BY status')}
            reviews = db.execute('SELECT * FROM research_memory_reviews ORDER BY created_at DESC,id DESC LIMIT 20').fetchall()
            pending = db.execute("SELECT * FROM research_memory_reviews WHERE status IN ('queued','claimed','deferred','blocked') "
                                 "ORDER BY priority DESC,created_at,id LIMIT 128").fetchall()
            revisions = db.execute('SELECT * FROM research_memory_revisions ORDER BY created_at DESC,id DESC LIMIT 20').fetchall()
        return {
            'schema_version': 1,
            'queue_counts': statuses,
            'revision_counts': revision_statuses,
            'thresholds': deepcopy(self.thresholds),
            'queued': [self._review_summary(dict(row)) for row in pending],
            'recent_reviews': [self._review_summary(dict(row)) for row in reviews],
            'recent_revisions': [{
                'id': row['id'], 'review_id': row['review_id'], 'parent_experiment_id': row['parent_experiment_id'],
                'child_experiment_id': row['child_experiment_id'], 'status': row['status'],
                'spec_hash': row['spec_hash'], 'policy_hash': row['policy_hash'],
                'evaluation': json.loads(row['evaluation']) if row['evaluation'] else None,
            } for row in revisions],
        }
