"""Durable, narrowing research cascade with bounded parallel work at each layer."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
import json
import threading
import time
import uuid

from . import analytics, execution
from .config import LAYER_ORDER
from .credentials import get_openai_key
from .domain import handoff, identity, roster, timestamp
from .ingestion import SourceCollector
from .provider import ModelProvider
from .store import BudgetExceeded


class Runtime:
    def __init__(self, store, *, key_loader=None, collector=None, provider=None):
        self.store = store
        self.key_loader = key_loader or get_openai_key
        self.collector = collector or SourceCollector(store.config())
        self._collector_injected = collector is not None
        self.provider = provider or ModelProvider(self.key_loader)
        self.started_at = timestamp()
        self.last_error = None
        self.status = 'paused'
        # Every newly installed architecture and every process restart starts paused.
        self.paused = True
        self.next_cycle_at = time.time()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._cycle_lock = threading.Lock()
        self._execution_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._trial_lock = threading.Lock()
        self._generation = 0
        self._market_thread = None
        self._key_configured = False
        self._key_checked = 0.0
        self.store.seed_agents(roster(self.store.config()))
        if self.store.get_document('portfolio') is None:
            self.store.set_document('portfolio', execution.initial_portfolio())
        self.store.event('cascade', 'Reverse cascade loaded: collectors → analysts → strategists → executor. Paper mode; paused until Start.')

    def has_key(self):
        with self._state_lock:
            if time.monotonic() - self._key_checked > 30:
                try:
                    self._key_configured = bool(self.key_loader())
                except Exception:
                    self._key_configured = False
                self._key_checked = time.monotonic()
            return self._key_configured

    def control(self, action):
        if action == 'start_all':  # Existing UI's Start button.
            action = 'start'
        if action not in {'start', 'resume', 'stop', 'pause', 'run_cycle'}:
            raise ValueError('Unknown cascade control action.')
        with self._execution_lock, self._state_lock:
            if action in {'stop', 'pause'}:
                self.paused = True
                self._generation += 1
                self.status = 'paused'
            else:
                self.paused = False
                self.status = 'running'
                self.next_cycle_at = time.time()
            self.last_error = None
        self.store.event('control', f'Cascade {action}; execution mode remains paper.')
        self._wake.set()

    def configuration_changed(self):
        # Freeze a cycle's roster and inputs. New topology applies only at its next boundary.
        if not self._cycle_lock.locked():
            self.store.seed_agents(roster(self.store.config()))
            if not self._collector_injected:
                self.collector = SourceCollector(self.store.config())
        self._wake.set()

    def _active(self, generation):
        return not self.paused and not self._stop.is_set() and self._generation == generation

    def run_forever(self):
        self._market_thread = threading.Thread(target=self._market_loop, name='cascade-market-clock', daemon=True)
        self._market_thread.start()
        while not self._stop.is_set():
            if not self.paused and time.time() >= self.next_cycle_at:
                try:
                    self.run_cycle()
                except Exception:
                    self.last_error = 'Cycle interrupted; completed jobs and handoffs are retained.'
                    self.store.event('cascade', self.last_error, 'error')
                    self.status = 'error'
                    self.next_cycle_at = time.time() + 30
            self._wake.wait(0.5)
            self._wake.clear()

    def shutdown(self):
        with self._execution_lock, self._state_lock:
            self.paused = True
            self._generation += 1
            self._stop.set()
            self._wake.set()
        if self._market_thread:
            self._market_thread.join(timeout=15)

    def _market_loop(self):
        while not self._stop.is_set():
            try:
                if not self.paused:
                    self.refresh_markets()
            except Exception:
                self.store.event('market', 'Public market refresh failed; stale prices cannot authorize fills.', 'warning')
            self._stop.wait(self.store.config()['market_refresh_seconds'])

    def refresh_markets(self):
        """A separate clock keeps prices/risk checks moving during slow research."""
        method = getattr(self.collector, 'collect_markets', None)
        if method is None:
            return
        markets = method(self.store.config()['symbols'])
        if isinstance(markets, dict):
            markets = markets.get('markets', [])
        if markets:
            self._merge_markets(markets)
            with self._execution_lock:
                if not self.paused and not self._stop.is_set():
                    decisions = self.store.get_document('active_decisions', [])
                    self._execute(decisions)

    def _merge_markets(self, markets):
        with self._state_lock:
            known = {item['symbol']: item for item in self._markets()}
            for market in markets:
                symbol = market.get('symbol')
                if symbol in self.store.config()['symbols']:
                    # Failed/stale refreshes never replace a newer observation.
                    old = known.get(symbol, {})
                    if market.get('as_of_ms', 0) >= old.get('as_of_ms', 0):
                        known[symbol] = {**old, **market}
            self.store.set_document('markets', list(known.values()))

    def _markets(self):
        return self.store.get_document('markets', [])

    def _payload(self, agent, cycle, inputs):
        config = self.store.config()
        payload = {'cycle_id': cycle['id'], 'layer': agent['layer'], 'topic': agent['topic'],
                   'as_of': timestamp(), 'inputs': inputs, 'allowed_symbols': config['symbols'],
                   'max_output_tokens': config['max_output_tokens']}
        if agent['layer'] == 'analyst':
            symbols = set()
            for item in inputs:
                symbols.update(item['payload'].get('symbols', []))
            markets = [row for row in self._research_markets(cycle) if not symbols or row['symbol'] in symbols]
            # Parameters are proposed before seeing the withheld chronological tail.
            payload['markets'] = [{'symbol': row['symbol'], 'candles': row.get('candles', [])[:int(len(row.get('candles', [])) * .65)][-100:]} for row in markets]
            payload['validation_policy'] = 'Use training history only; the host alone scores the withheld tail once, with frozen parameters and costs.'
        if agent['layer'] == 'executor':
            portfolio = self.store.get_document('portfolio')
            payload['portfolio'] = {k: v for k, v in portfolio.items() if k not in {'trades', 'pnl_history'}}
            payload['risk'] = config['risk']
        # Drop whole oldest inputs instead of corrupting source text or JSON framing.
        while len(json.dumps(payload, ensure_ascii=False).encode()) > config['max_input_bytes'] and payload['inputs']:
            payload['inputs'] = payload['inputs'][:-1]
        if len(json.dumps(payload, ensure_ascii=False).encode()) > config['max_input_bytes']:
            payload.pop('markets', None)
        return payload

    def _model(self, agent, payload):
        config = self.store.config()
        model_agent = {**agent, 'max_output_tokens': config['max_output_tokens'], 'max_input_bytes': config['max_input_bytes']}
        request_size = len(json.dumps(payload, ensure_ascii=False).encode())
        request_size += len(agent.get('instructions', '')[:6000].encode()) + len(agent.get('mandate', '')[:1000].encode())
        reservation = self.store.reserve(agent['model'], request_size, config['max_output_tokens'])
        try:
            result = self.provider.run(model_agent, payload)
        except Exception as error:
            self.store.settle(reservation, getattr(error, 'usage', None))
            raise
        self.store.settle(reservation, result.get('usage'))
        output = result.get('output')
        if not isinstance(output, dict):
            raise ValueError('Provider returned an invalid layer envelope.')
        if isinstance(output.get('summary'), str):
            self.store.set_document('report_' + payload['cycle_id'] + '_' + agent['id'], output['summary'][:1200])
        return output

    def _inputs_for(self, agent, cycle):
        depth = agent['depth']
        if depth == 0:
            return []
        previous = self.store.handoffs(cycle['id'], LAYER_ORDER[depth - 1])
        roster_by_id = {row['id']: row for row in cycle['roster']}
        selected = [item for item in previous if roster_by_id[item['producer']]['parent_id'] == agent['id']]
        if depth == 1:
            selected.sort(key=lambda row: float(row['payload'].get('importance', 0)), reverse=True)
            # Repeated coverage of the same source is one observation, not corroboration.
            unique = {}
            for row in selected:
                key = identity([row['evidence_ids'], sorted(row['payload'].get('symbols', []))])
                unique.setdefault(key, row)
            selected = list(unique.values())
        elif depth == 2:
            selected.sort(key=lambda row: row['payload'].get('metrics', {}).get('adjusted_p_value', 1))
        return selected[:self.store.config()['max_handoffs_per_agent']]

    @staticmethod
    def _objects(output, key, limit=32):
        values = output.get(key, [])
        return [row for row in values[:limit] if isinstance(row, dict)] if isinstance(values, list) else []

    def _collect(self, agent, cycle, generation):
        checkpoint = self.store.job(cycle['id'], agent['id'])
        collected = (checkpoint or {}).get('value')
        if not isinstance(collected, dict) or 'collected' not in collected:
            collected = {'collected': self.collector.collect(agent)}
            if not self._active(generation):
                return []
            self.store.save_job(cycle['id'], agent['id'], 'collected', collected)
        result = collected['collected']
        self._merge_markets(result.get('markets', []))
        evidence = result.get('evidence', [])[:self.store.config()['max_handoffs_per_agent']]
        evidence = [row for row in evidence if isinstance(row, dict) and all(isinstance(row.get(key), str) and row[key] for key in ('id', 'source_url', 'content', 'observed_at'))]
        with self._state_lock:
            key = 'cycle_evidence_' + cycle['id']
            retained = set(self.store.get_document(key, []))
            bounded = []
            for item in evidence:
                if item['id'] in retained or len(retained) < self.store.config()['max_evidence_per_cycle']:
                    retained.add(item['id'])
                    bounded.append(item)
            self.store.set_document(key, sorted(retained))
            evidence = bounded
        self.store.save_evidence(evidence)
        if result.get('errors'):
            self.store.event(agent['id'], f'{len(result["errors"])} source fetch(es) unavailable; other sources continued.', 'warning')
        if not evidence:
            return []
        payload = self._payload(agent, cycle, [])
        payload['evidence'] = [{**row, 'content': row['content'][:2200]} for row in evidence]
        while len(json.dumps(payload, ensure_ascii=False).encode()) > self.store.config()['max_input_bytes'] and payload['evidence']:
            payload['evidence'].pop()
        if not self._active(generation):
            return []
        output = self._model(agent, payload)
        allowed = {row['id']: row for row in payload['evidence']}
        items = []
        for finding in self._objects(output, 'findings'):
            refs = finding.get('evidence_ids', [])
            if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in allowed for ref in refs):
                continue
            symbols = finding.get('symbols', [])
            if not isinstance(symbols, list) or any(symbol not in self.store.config()['symbols'] for symbol in symbols):
                continue
            selected = {**finding, 'symbols': symbols or [agent['symbol']],
                        'sources': [{k: allowed[ref].get(k) for k in ('id', 'source_url', 'source_kind', 'title', 'content', 'observed_at', 'published_at', 'numeric')} for ref in refs[:8]]}
            for source in selected['sources']:
                source['content'] = (source['content'] or '')[:1800]
            items.append(handoff(cycle['id'], 'collector', agent['id'], selected, refs, []))
        return items

    def _analyze(self, agent, cycle, inputs, generation):
        payload = self._payload(agent, cycle, inputs)
        inputs = payload['inputs']
        if not inputs or not self._active(generation):
            return []
        output = self._model(agent, payload)
        refs = {ref for item in inputs for ref in item['evidence_ids']}
        proposals = []
        for row in self._objects(output, 'edges', 12):
            citations = row.get('evidence_ids')
            if isinstance(citations, list) and citations and all(isinstance(ref, str) and ref in refs for ref in citations):
                proposals.append({**row, 'parameters': {k: v for k, v in row.get('parameters', {}).items() if v is not None}})
        if not proposals:
            return []
        return [handoff(cycle['id'], 'analyst_proposal', agent['id'], proposal, proposal['evidence_ids'], [item['id'] for item in inputs]) for proposal in proposals]

    def _validate_analyst_batch(self, cycle):
        """Score the complete batch with one trial count, after every analyst finishes."""
        checkpoint = self.store.get_document('validation_' + cycle['id'])
        if checkpoint is None:
            proposed = self.store.handoffs(cycle['id'], 'analyst_proposal')
            with self._trial_lock:
                # One transaction-level checkpoint prevents repeated trial charges after restart.
                trials = self.store.get_document('statistical_trial_count', 0) + len(proposed)
                checkpoint = {'trial_count': max(1, trials), 'proposals': proposed, 'markets': self._research_markets(cycle)}
                checkpoint['validated_at'] = timestamp()
                self.store.commit_validation(cycle['id'], trials, checkpoint)
        proposed = checkpoint['proposals']
        results = analytics.validate_edges([item['payload'] for item in proposed], checkpoint['markets'], trial_count=checkpoint['trial_count'])
        for source, edge in zip(proposed, results):
            agent_id = source['producer']
            edge['agent_id'] = agent_id
            edge['created_at'] = checkpoint['validated_at']
            # Every rejected hypothesis remains visible in the existing Test UI.
            metrics = edge.get('metrics', {})
            display = {'id': 'edge_' + edge['id'], 'name': str(edge.get('hypothesis', 'Statistical hypothesis'))[:160],
                       'symbol': edge['symbol'], 'family': edge['kind'], 'status': edge['status'],
                       'reason': edge['reason'], 'created_at': edge['created_at'], 'agent_id': agent_id,
                       'spec': {'symbol': edge['symbol'], 'kind': edge['kind'], 'parameters': edge.get('parameters', {})},
                       **{key: metrics.get(key) for key in ('net_return', 'sharpe', 'max_drawdown', 'trades')}}
            self.store.save_strategy(display)
            if edge['status'] == 'validated':
                self.store.save_handoff(handoff(cycle['id'], 'analyst', agent_id, edge, edge['evidence_ids'], source['input_ids']))

    def _strategize(self, agent, cycle, inputs, generation):
        payload = self._payload(agent, cycle, inputs)
        inputs = payload['inputs']
        if not inputs or not self._active(generation):
            return []
        output = self._model(agent, payload)
        edges = [item['payload'] for item in inputs]
        allowed = {edge['id'] for edge in edges}
        items = []
        for proposal in self._objects(output, 'strategies', 8):
            edge_ids = proposal.get('edge_ids', [])
            if not isinstance(edge_ids, list) or not edge_ids or any(not isinstance(ref, str) or ref not in allowed for ref in edge_ids):
                continue
            proposal['parameters'] = {k: v for k, v in proposal.get('parameters', {}).items() if v is not None}
            assessed = analytics.assess_strategy(proposal, edges, self._research_markets(cycle))
            assessed['id'] = 'strategy_' + identity({'cycle': cycle['id'], 'proposal': proposal, 'agent': agent['id']})
            assessed['agent_id'] = agent['id']
            assessed['cycle_id'] = cycle['id']
            assessed['created_at'] = timestamp()
            self.store.save_strategy(assessed)
            if assessed['status'] == 'paper':
                items.append(handoff(cycle['id'], 'strategist', agent['id'], assessed, assessed.get('evidence_ids', []), [item['id'] for item in inputs]))
        return items

    def _authorize(self, agent, cycle, inputs, generation):
        payload = self._payload(agent, cycle, inputs)
        if not payload['inputs'] or not self._active(generation):
            return []
        output = self._model(agent, payload)
        allowed = {item['payload']['id'] for item in payload['inputs']}
        decisions = [row for row in self._objects(output, 'decisions') if row.get('strategy_id') in allowed and row.get('action') in {'activate', 'hold', 'close'}]
        with self._execution_lock:
            if not self._active(generation):
                return []
            self.store.set_document('active_decisions', decisions)
            self._execute(decisions)
        return [handoff(cycle['id'], 'executor', agent['id'], decision, [], [item['id'] for item in payload['inputs']]) for decision in decisions]

    def _execute(self, decisions):
        config = self.store.config()
        # Assessments are research snapshots; retire stale approvals before paper fills.
        strategies = []
        for strategy in self.store.strategies():
            if strategy.get('status') == 'paper':
                try:
                    from datetime import datetime
                    age = time.time() - datetime.fromisoformat(strategy['created_at'].replace('Z', '+00:00')).timestamp()
                    if 0 <= age <= config['risk']['position_ttl_seconds']:
                        strategies.append(strategy)
                except (KeyError, ValueError, TypeError):
                    continue
        before = self.store.get_document('portfolio')
        after = execution.execute(before, strategies, decisions, self._markets(), config)
        self.store.set_document('portfolio', after)
        added = len(after.get('trades', [])) - len(before.get('trades', []))
        if added > 0:
            self.store.event('executor', f'Recorded {added} simulated fill(s) under deterministic risk limits.')

    def _research_markets(self, cycle):
        return self.store.get_document('research_snapshot_' + cycle['id'], [])

    def _job(self, agent, cycle, generation):
        existing = self.store.job(cycle['id'], agent['id'])
        if existing and existing['status'] == 'completed':
            return 'completed'
        if not self._active(generation):
            return 'cancelled'
        start = time.monotonic()
        # Preserve a completed source fetch across model-budget deferrals.
        if not existing or existing['status'] != 'collected':
            self.store.save_job(cycle['id'], agent['id'], 'working')
        self.store.update_agent(agent['id'], status='working', task=f'{agent["layer"]} cycle {cycle["id"][:8]}')
        try:
            inputs = self._inputs_for(agent, cycle)
            if agent['layer'] == 'collector':
                results = self._collect(agent, cycle, generation)
            elif agent['layer'] == 'analyst':
                results = self._analyze(agent, cycle, inputs, generation)
            elif agent['layer'] == 'strategist':
                results = self._strategize(agent, cycle, inputs, generation)
            else:
                results = self._authorize(agent, cycle, inputs, generation)
            if not self._active(generation):
                return 'cancelled'
            for result in results:
                self.store.save_handoff(result)
            self.store.save_job(cycle['id'], agent['id'], 'completed', {'handoffs': [row['id'] for row in results]})
            count_summary = f'{len(results)} source-linked result(s) passed to the next layer.' if inputs or agent['layer'] == 'collector' else 'No qualified upstream evidence; no model call needed.'
            report = self.store.get_document('report_' + cycle['id'] + '_' + agent['id'], '')
            summary = (report + '\n' + count_summary).strip()
            self.store.update_agent(agent['id'], status='idle', task='', last_run_at=timestamp(), summary=summary,
                                    recent_tools=[{'id': identity([cycle['id'], agent['id']]), 'name': agent['layer'], 'status': 'completed', 'summary': summary, 'created_at': timestamp(), 'duration_ms': int((time.monotonic() - start) * 1000)}])
            return 'completed'
        except BudgetExceeded as error:
            self.store.update_agent(agent['id'], status='budget_limited', task='', summary=str(error))
            return 'budget_temporary' if error.temporary else 'budget_limited'
        except Exception as error:
            # Provider/source text, headers, credentials and raw errors never enter logs.
            message = f'{agent["layer"].title()} job failed ({type(error).__name__}); other jobs continue.'
            self.store.event(agent['id'], message, 'warning')
            self.store.update_agent(agent['id'], status='error', task='', last_run_at=timestamp(), summary=message)
            self.store.save_job(cycle['id'], agent['id'], 'failed', {'error_type': type(error).__name__})
            return 'failed'
        finally:
            if not self._active(generation):
                self.store.update_agent(agent['id'], status='interrupted', task='', summary='Stopped at a cascade boundary; no further execution authorized.')

    def run_cycle(self):
        """Run or resume one durable cycle. Failed jobs do not block unrelated sources."""
        if not self._cycle_lock.acquire(blocking=False):
            return False
        try:
            if self.paused or self._stop.is_set():
                return False
            generation = self._generation
            if not self.has_key():
                self.status = 'needs_credentials'
                self.last_error = 'Configure the runtime OpenAI credential to start cascade research.'
                self.next_cycle_at = time.time() + 30
                return False
            cycle = self.store.get_document('active_cycle')
            if not cycle or cycle.get('status') == 'completed':
                config = self.store.config()
                agents = roster(config)
                if not self._collector_injected:
                    self.collector = SourceCollector(config)
                self.store.seed_agents(agents)
                cycle = {'id': uuid.uuid4().hex, 'status': 'running', 'stage': 0, 'started_at': timestamp(), 'roster': agents,
                         'max_concurrency': config['max_concurrency']}
                self.store.set_document('active_cycle', cycle)
            for index in range(cycle['stage'], len(LAYER_ORDER)):
                if not self._active(generation):
                    return False
                layer = LAYER_ORDER[index]
                if layer == 'analyst' and self.store.get_document('research_snapshot_' + cycle['id']) is None:
                    self.store.set_document('research_snapshot_' + cycle['id'], self._markets())
                self.status = layer
                cycle.update(stage=index, status='running')
                self.store.set_document('active_cycle', cycle)
                agents = [agent for agent in cycle['roster'] if agent['layer'] == layer]
                outcomes = []
                with ThreadPoolExecutor(max_workers=min(cycle['max_concurrency'], len(agents)), thread_name_prefix=f'cascade-{layer}') as pool:
                    jobs = {pool.submit(self._job, agent, cycle, generation): agent for agent in agents}
                    for future in as_completed(jobs):
                        if not future.cancelled():
                            outcomes.append(future.result())
                        if not self._active(generation):
                            for job in jobs:
                                job.cancel()
                if not self._active(generation):
                    return False
                if any(value in {'budget_limited', 'budget_temporary'} for value in outcomes):
                    permanent = 'budget_limited' in outcomes
                    self.status = 'budget_limited'
                    self.next_cycle_at = (time.time() // 86400 + 1) * 86400 if permanent else time.time() + 3
                    self.store.event(layer, 'Cycle checkpoint retained while model-budget reservations are unavailable.', 'warning')
                    return False
                if layer == 'analyst':
                    self._validate_analyst_batch(cycle)
                cycle['stage'] = index + 1
                self.store.set_document('active_cycle', cycle)
                count = len(self.store.handoffs(cycle['id'], layer))
                self.store.event(layer, f'Layer complete: {len(agents)} agents, {count} qualified handoffs.')
            cycle.update(status='completed', finished_at=timestamp())
            self.store.set_document('active_cycle', cycle)
            self.store.set_document('last_cycle_at', cycle['finished_at'])
            self.status = 'running'
            self.last_error = None
            self.next_cycle_at = time.time() + self.store.config()['cycle_interval_seconds']
            return True
        finally:
            self._cycle_lock.release()

    def state(self):
        config = self.store.config()
        agents = self.store.agents()
        roles = []
        for layer in LAYER_ORDER:
            agent = next((row for row in agents if row['layer'] == layer), None)
            if agent:
                roles.append({**agent, 'id': layer, 'name': layer.title(), 'researcher': layer != 'executor'})
        markets = self._markets()
        cycle = self.store.get_document('active_cycle', {})
        return {
            'runtime': {'status': 'paused' if self.paused else self.status, 'mode': 'paper', 'started_at': self.started_at,
                        'next_cycle_at': None if self.paused else timestamp(self.next_cycle_at), 'openai_configured': self.has_key(),
                        'last_error': self.last_error, 'live_trading_available': False},
            'budget': self.store.budget(), 'agents': agents, 'agent_roles': roles,
            'strategies': self.store.strategies(), 'portfolio': self.store.get_document('portfolio'),
            'markets': [{key: row.get(key) for key in ('symbol', 'price', 'change_24h', 'funding_rate', 'updated_at')} for row in markets],
            'events': self.store.events(), 'config': config,
            'cascade': {'architecture': 'reverse_cascade', 'version': 1,
                        'layers': [{'id': layer, **config['layers'][layer]} for layer in LAYER_ORDER],
                        'cycle': {key: value for key, value in cycle.items() if key != 'roster'},
                        'handoff_counts': {layer: len(self.store.handoffs(cycle.get('id', ''), layer)) for layer in LAYER_ORDER},
                        'statistical_trial_count': self.store.get_document('statistical_trial_count', 0)},
        }
