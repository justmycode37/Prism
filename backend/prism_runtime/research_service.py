"""Collector-independent recursive review worker and host revision transitions."""
from __future__ import annotations

import copy
import hashlib
import json
import time

from .agent_contracts import review_response_schema
from .agent_runtime import ResponsesAgent
from .domain import identity
from .live_research import LiveResearch
from .research_memory import ResearchMemory
from .research_tape import ResearchTape
from .simulation import execution_policy
from .store import BudgetExceeded


class ResearchWorker:
    def __init__(self, store, provider, *, memory=None, market=None, agents=None, clock=None):
        self.store, self.clock = store, clock or store._clock or time.time
        self.memory = memory or ResearchMemory(store, clock=self.clock)
        self.market = market
        self.agents = agents or ResponsesAgent(store, provider, clock=self.clock)
        self.live = LiveResearch(store, clock=self.clock)
        self.tape = ResearchTape(store)
        self.last_error = None

    def scan(self):
        # The dedicated scheduler chooses timed reviews. Automatic scans react
        # to results/evidence and explicit prior continuation conditions.
        config = self.store.config()['operations']
        return self.memory.scan_triggers(include_elapsed=False, thresholds={
            'session_interval': config['review_sessions'], 'trade_interval': config['review_trades']})

    def step(self):
        # A crash between result persistence and application cannot lose work or
        # repeat a completed model invocation.
        with self.store.connection() as db:
            completed = db.execute("SELECT id FROM research_memory_reviews WHERE status='completed' ORDER BY completed_at,id LIMIT 1").fetchone()
        if completed:
            result = self.memory.apply_review(completed['id'], markets=self.store.markets(), config=self.store.config())
            self._transition(result)
            return result
        with self.store.connection() as db:
            transitions = db.execute("SELECT value FROM cascade_meta WHERE key LIKE 'revision_transition_%' LIMIT 128").fetchall()
        for row in transitions:
            pending = json.loads(row['value'])
            if (pending.get('status') == 'blocked' and pending.get('application')
                    and self.clock() >= pending.get('retry_at', 0)):
                self._transition(pending['application'])
                return {'status': 'transition_rechecked', 'experiment_id': pending['application']['experiment_id']}
        review = self.memory.claim_review()
        if not review:
            return None
        try:
            payload = {k: v for k, v in review['request'].items() if k != 'response_schema'}
            result = self.agents.structured('review', payload, review_response_schema(), task_id=review['id'], role='reviewer')
            if result.get('revision'):
                result = copy.deepcopy(result)
                parent = self.store.experiment(review['experiment_id'])
                proposal = result['revision']['proposal']
                if proposal.get('symbol') != parent['spec']['symbol']:
                    raise ValueError('A review cannot substitute another instrument; use separate discovery')
                if parent['spec'].get('security_id'):
                    proposal['security_id'] = parent['spec']['security_id']
            self.memory.complete_review(review['id'], result)
            application = self.memory.apply_review(review['id'], markets=self.store.markets(), config=self.store.config())
            self._transition(application)
            self.last_error = None
            return application
        except BudgetExceeded as error:
            retry = self.clock() + 5 if error.temporary else (self.clock() // 86400 + 1) * 86400
            self.memory.defer_review(review['id'], str(error), retry_at=retry)
            self.last_error = str(error)
            return {'status': 'deferred', 'reason': str(error)}
        except Exception as error:
            self.last_error = type(error).__name__ + ': ' + str(error)[:300]
            # Unknown model outcomes are not blindly repeated. A later material
            # observation creates a new fingerprint and review request.
            self.memory.defer_review(review['id'], self.last_error)
            self.store.event('review', self.last_error, 'warning')
            return {'status': 'blocked', 'reason': self.last_error}

    def _transition(self, application):
        child_id = application.get('experiment_id')
        if application.get('outcome') != 'revise' or not child_id:
            return
        child = self.store.experiment(child_id)
        if not child or child['status'] in {'rejected', 'retired'}:
            self.store.set_document('revision_transition_' + child_id, {'status': 'cancelled', 'at': self.clock(),
                                                                       'reason': 'Candidate is absent or terminal'})
            return
        try:
            if child['policy'].get('version') == 2:
                self.live.adopt_revision(child_id)
                strategy = next(x for x in self.live.strategies() if x['id'] == child_id)
                if self.market:
                    self.market.register_strategy(strategy)
                    self._evaluate_initial(child)
                self.store.set_document('revision_transition_' + child_id, {'status': 'completed', 'at': self.clock()})
                return
            if application.get('status') != 'development_pass_forward_required':
                return
            if child['policy'] != execution_policy(self.store.config()):
                raise ValueError('Revision policy does not match the active daily host policy')
            candidates = [x for x in self.store.strategies() if x.get('experiment_id') == child_id]
            if not candidates:
                return
            strategy = candidates[-1]
            if strategy.get('status') in {'shadow', 'paper', 'queued'}:
                self.store.set_document('revision_transition_' + child_id, {'status': 'completed', 'at': self.clock()})
                return
            cap = self.store.config()['research']['max_shadow_candidates']
            active = sum(x.get('status') in {'shadow', 'paper', 'queued'} for x in self.store.strategies())
            if active >= cap:
                raise ValueError('Shadow capacity defers the separate revision')
            digest = hashlib.sha256(json.dumps(child['policy'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            self.store.save_strategy({**strategy, 'status': 'queued', 'phase': 'prospective_shadow',
                'registered_at_ms': child['registered_at_ms'], 'policy_hash': digest,
                'validation': {**strategy.get('validation', {}), 'passed': True, 'development_only': True},
                'reason': 'Separate revision admitted to a fresh shadow account; prospective gate unchanged'})
            self.store.set_document('revision_transition_' + child_id, {'status': 'completed', 'at': self.clock()})
        except (ValueError, KeyError, StopIteration) as error:
            self.store.set_document('revision_transition_' + child_id, {'status': 'blocked', 'reason': str(error),
                'at': self.clock(), 'retry_at': self.clock() + 60,
                'application': {key: application.get(key) for key in ('outcome', 'status', 'experiment_id')}})
            self.store.event('review', 'Revision transition blocked: ' + str(error)[:250], 'warning')

    def evaluate_initial(self):
        if not self.market:
            return []
        evaluated = []
        for strategy in self.live.strategies():
            exp = self.store.experiment(strategy['id'])
            if any(x.get('phase') == 'development' for x in exp.get('evaluations', [])):
                continue
            attempt = self.store.get_document('development_attempt_' + strategy['id'], {})
            if self.clock() < attempt.get('retry_at', 0):
                continue
            try:
                self._evaluate_initial(exp)
                evaluated.append(strategy['id'])
            except Exception as error:
                self.store.set_document('development_attempt_' + strategy['id'], {
                    'status': 'deferred', 'reason': type(error).__name__, 'retry_at': self.clock() + 60})
            break  # Bound each review-worker iteration; market protection is separate.
        return evaluated

    def _evaluate_initial(self, experiment):
        sid, policy = experiment['id'], experiment['policy']['market_policy']
        if policy['signal_mode'] == 'intrabar':
            self.tape.adopt(self.live, sid, self.market.environment)
            result = self.live.evaluate_intrabar_development(sid)
        else:
            # Request the registered development window, never today's history
            # projected backwards or bars learned after the experiment cutoff.
            from .market_policy import INTERVAL_MS
            cutoff, width = experiment['registered_at_ms'], INTERVAL_MS[policy['primary_timeframe']]
            start = max(0, cutoff // width * width - 500 * width)
            response = self.market._get_adapter().klines(category=policy['category'], symbol=policy['symbol'],
                interval=policy['primary_timeframe'], start=start, end=cutoff - 1, limit=500)
            rows = self.market._response_rows(response, policy['category'], policy['symbol'])
            bars = [self.market._normalize_bar(row, policy['primary_timeframe'], cutoff, source='rest') for row in rows]
            bars = sorted((x for x in bars if x['closed'] and x['end_ms'] <= cutoff), key=lambda x: x['start_ms'])
            if len(bars) < 300:
                raise ValueError('Insufficient pre-registration development history')
            result = self.live.evaluate_development(sid, bars)
        self.memory.enqueue_review(sid, trigger='initial_evaluation', material={'development': result})
        self.store.set_document('development_attempt_' + sid, {'status': 'completed', 'at': self.clock()})
        return result
