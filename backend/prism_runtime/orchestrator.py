"""Durable, narrowing research cascade with bounded parallel work at each layer."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
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
from . import temporal, simulation, experiments
from .search import expand_hypothesis
from .agent_runtime import ComponentBudget
from .research_memory import ResearchMemory


class Runtime:
    def __init__(self, store, *, key_loader=None, collector=None, provider=None):
        self.store = store
        self.key_loader = key_loader or get_openai_key
        self.collector = collector or SourceCollector(store.config())
        self._collector_injected = collector is not None
        self._collector_config_key = identity(store.config())
        self.provider = provider or ModelProvider(self.key_loader)
        self.component_budget = ComponentBudget(store)
        self.memory = ResearchMemory(store)
        self.continuous = None
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
        self._credentials = {'openai_configured': False}
        self._key_checked = None
        self._research_state_cache = None
        self._research_state_cache_at = 0.
        self._research_market_cache_id = None
        self._research_market_cache = None
        self.store.seed_agents(roster(self.store.config()))
        if self.store.get_document('portfolio') is None:
            self.store.set_document('portfolio', execution.initial_portfolio())
        if self.store.get_document('research_protocol_version') != 1:
            old_cycle = self.store.get_document('active_cycle')
            if old_cycle:
                self.store.set_document('active_cycle_before_prospective_protocol', old_cycle)
                self.store.set_document('active_cycle', None)
            self.store.set_document('active_decisions', [])
            old_account = self.store.get_document('portfolio', {})
            if old_account.get('positions') or old_account.get('trades'):
                self.store.set_document('portfolio_before_prospective_protocol', old_account)
                self.store.set_document('portfolio', execution.initial_portfolio(old_account.get('initial_equity', 100000.)))
                self.store.event('research', 'Archived the legacy simulated account; prospective paper accounting starts separately.')
            self.store.set_document('research_protocol_version', 1)
        self.store.event('runtime', 'Event-driven research and trading runtime loaded; paused until Start. Exchange execution requires explicit operator configuration.')

    def has_key(self):
        with self._state_lock:
            if self._key_checked is None or time.monotonic() - self._key_checked >= 30:
                try:
                    self._credentials['openai_configured'] = bool(self.key_loader())
                except Exception:
                    self._credentials['openai_configured'] = False
                self._key_configured = all(self._credentials.values())
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
                if self.continuous and getattr(self.continuous, 'requires_restart', False):
                    raise ValueError('Restart the paused runtime to apply the configured account/environment change')
                self.paused = False
                self.status = 'running'
                self.next_cycle_at = time.time()
            self.last_error = None
        self.store.event('control', f'Runtime {action}; configured Bybit mode is {self.store.config()["bybit"]["mode"]}.')
        if self.continuous:
            self.continuous.wake()
        self._wake.set()

    def update_configuration(self, changes):
        # Shared with ContinuousServices' mutation lock: once a config update
        # returns, no submit validated under the prior envelope can begin.
        with self._execution_lock:
            return self._update_configuration(changes)

    def _update_configuration(self, changes):
        """Reject an account/environment transition while state can be exposed."""
        from .config import validate_config_update
        from .operations_config import merge_settings
        from .risk_controls import ACTIVE_ORDERS
        validated = validate_config_update(changes)
        old = self.store.config()
        updated = merge_settings(old, validated)
        critical = any(old['bybit'][key] != updated['bybit'][key]
                       for key in ('mode', 'account_id', 'account_type', 'position_mode'))
        narrowing = (old['bybit']['enabled'] and not updated['bybit']['enabled'] or
                     old['bybit']['mainnet_enabled'] and not updated['bybit']['mainnet_enabled'] or
                     any(x not in updated['bybit']['allowed_instruments'] for x in old['bybit']['allowed_instruments']))
        with self._execution_lock, self._state_lock:
            if critical and not self.paused:
                raise ValueError('Pause before changing the account/environment; a process restart applies the new binding')
            if self.continuous and (critical or narrowing) and self.continuous.has_exposure():
                raise ValueError('Close exposure and reconcile outstanding orders before changing the exchange envelope')
            if critical or narrowing:
                with self.store.connection() as db:
                    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bybit_orders'").fetchone()
                    if exists:
                        rows = db.execute('SELECT status FROM bybit_orders').fetchall()
                        if any(row['status'] in ACTIVE_ORDERS for row in rows):
                            raise ValueError('Unresolved exchange orders prevent an environment or allowlist transition')
                    positions = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bybit_positions'").fetchone()
                    if positions:
                        rows = db.execute('SELECT value FROM bybit_positions').fetchall()
                        if any(float(json.loads(row['value']).get('quantity', 0)) > 0 for row in rows):
                            raise ValueError('Exchange positions prevent an environment or allowlist transition')
                # Spot inventory lives in the wallet and execution attribution,
                # not the derivatives position endpoint.
                paper = self.store.get_document('bybit_paper_exchange_v1', {})
                if any(float(x.get('quantity', 0)) > 0 for x in paper.get('positions', {}).values()):
                    raise ValueError('Paper positions must be closed before switching their execution environment')
            result = self.store.update_config(validated)
            if self.continuous:
                self.continuous.configuration_changed(result)
        self.configuration_changed()
        return result

    def configuration_changed(self):
        # Freeze a cycle's roster and inputs. New topology applies only at its next boundary.
        if not self._cycle_lock.locked():
            self.store.seed_agents(roster(self.store.config()))
            if not self._collector_injected:
                self.collector = SourceCollector(self.store.config())
                self._collector_config_key = identity(self.store.config())
        self._wake.set()
        if self.continuous:
            self.continuous.wake()

    def register_strategy(self, values):
        from .live_research import LiveResearch
        if not isinstance(values, dict) or set(values) != {'spec', 'market_policy', 'hypothesis', 'evidence_ids'}:
            raise ValueError('Registration requires spec, market_policy, hypothesis and existing evidence_ids')
        result = LiveResearch(self.store, clock=self.store._clock).register(**values)
        if self.continuous:
            strategy = next(x for x in self.store.strategies() if x['id'] == result['id'])
            self.continuous.market.register_strategy(strategy)
            self.continuous.wake()
        self._research_state_cache = None
        self._wake.set()
        return result

    def queue_review(self, strategy_id):
        if not isinstance(strategy_id, str) or not strategy_id:
            raise ValueError('An exact strategy version is required')
        result = self.memory.enqueue_review(strategy_id, trigger='manual', priority=50)
        self._wake.set()
        if self.continuous:
            self.continuous.wake()
        return result

    def import_development_tape(self, body):
        from .live_research import LiveResearch
        if (not isinstance(body, dict) or set(body) != {'strategy_id', 'observations', 'evaluate'}
                or not isinstance(body['strategy_id'], str) or not isinstance(body['observations'], list)
                or len(body['observations']) > 128 or type(body['evaluate']) is not bool):
            raise ValueError('An exact strategy_id, up to 128 measured observations, and evaluate boolean are required')
        live = LiveResearch(self.store, clock=self.store._clock)
        result = live.record_intrabar_development_tape(body['strategy_id'], body['observations']) if body['observations'] else {}
        if body['evaluate']:
            result['evaluation'] = live.evaluate_intrabar_development(body['strategy_id'])
            self.memory.enqueue_review(body['strategy_id'], trigger='initial_evaluation',
                                       material={'development': result['evaluation']})
        self._research_state_cache = None
        if self.continuous:
            self.continuous.wake()
        return result

    def import_calendar(self, body):
        if self.continuous:
            return self.continuous.import_calendar(body)
        from .scheduling import CalendarFeedProvider, Scheduler
        if not isinstance(body, dict) or set(body) - {'id', 'events', 'sessions', 'coverage', 'source_url', 'provenance'}:
            raise ValueError('Calendar import requires sourced events, sessions and coverage')
        clock = self.store._clock
        result = CalendarFeedProvider(clock=clock).load([{'id': 'operator-calendar', **body, 'type': 'operator'}])
        if result['errors']:
            raise ValueError('Invalid calendar document')
        scheduler = Scheduler(self.store, clock=clock)
        scheduler.upsert_events(result['events'])
        scheduler.upsert_coverage(result['coverage'])
        return result

    def operations_state(self):
        if self.continuous:
            return self.continuous.state()
        from .operations_console import projection
        config = self.store.config()
        return projection(mode=config['bybit']['mode'], enabled=False,
            schedule={'rationale': 'Runtime is paused; Start enables the independent services and validated event schedule.'},
            calendar=[], subscriptions=[], memory=self.memory.state(), execution={},
            resources=self.component_budget.state(), blocked=[{'kind': 'Calendar coverage',
                'reason': 'Calendar coverage has not yet been loaded and verified.'}])

    def _active(self, generation):
        return not self.paused and not self._stop.is_set() and self._generation == generation

    def run_forever(self):
        from .continuous import ContinuousServices
        if self.continuous is None:
            self.continuous = ContinuousServices(self)
        self.continuous.run()

    def shutdown(self):
        with self._execution_lock, self._state_lock:
            self.paused = True
            self._generation += 1
            self._stop.set()
            self._wake.set()
        if self.continuous:
            self.continuous.market.stop()
            self.continuous.wake()
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
        tracked = [row['symbol'] for row in self.store.strategies() if row.get('policy_version', 1) == 1 and row.get('status') in {'shadow', 'paper'}]
        symbols = list(dict.fromkeys(self._symbols() + tracked + self.store.config()['research']['benchmark_symbols']
            + [row['symbol'] for row in self.store.get_document('portfolio', {}).get('positions', [])]))
        markets = method(symbols)
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
            known = {item['symbol']: item for item in self.store.markets(history=False)}
            features = self.store.get_document('event_features_by_symbol', {})
            benchmarks = self.store.config()['research']['benchmark_symbols']
            tracked = {row['symbol'] for row in self.store.strategies() if row.get('policy_version', 1) == 1 and row.get('status') in {'shadow', 'paper'}}
            held = {row['symbol'] for row in self.store.get_document('portfolio', {}).get('positions', [])}
            universe = set(self._symbols())
            for market in markets:
                symbol = market.get('symbol')
                if symbol in benchmarks:
                    market = {**market, 'benchmark_kind': 'market' if symbol == benchmarks[0] else 'sector'}
                if symbol in universe or symbol in held or symbol in benchmarks or symbol in tracked:
                    # Failed/stale refreshes never replace a newer observation.
                    old = known.get(symbol, {})
                    if market.get('as_of_ms', 0) >= old.get('as_of_ms', 0):
                        known[symbol] = {**old, **market}
                        known[symbol]['event_features'] = features.get(symbol, old.get('event_features', []))
                    elif market.get('candles') and max(row.get('end_ms',0) for row in market['candles']) >= old.get('history_end_ms',0):
                        known[symbol] = {**old, **{key:market[key] for key in ('candles','raw_candles','history_adjustment','raw_history_basis','history_revision') if key in market}}
            self.store.set_document('markets', list(known.values()))

    def _markets(self):
        return self.store.markets()

    def _symbols(self, cycle=None):
        if cycle and cycle.get('symbols'):
            return cycle['symbols']
        universe = self.store.get_document('sector_universe', {})
        return universe.get('symbols') or self.store.config()['symbols']

    def _discover(self, generation):
        """Refresh sector membership once per cycle, before freezing assignments."""
        config = self.store.config()
        previous = self.store.get_document('sector_universe', {})
        if previous.get('discovered_at'):
            age = time.time() - datetime.fromisoformat(previous['discovered_at'].replace('Z', '+00:00')).timestamp()
            if 0 <= age < config['research']['discovery_interval_seconds']:
                return previous
        method = getattr(self.collector, 'discover_universe', None)
        result = {}
        if method and config.get('sector', {}).get('discovery_enabled', True):
            try:
                result = method()
            except Exception:
                self.store.event('discovery', 'Sector discovery unavailable; configured AI equity seeds retained.', 'warning')
        if not self._active(generation):
            return None
        from .sector import normalize_symbol
        companies = []
        seen = set()
        for row in result.get('companies', []):
            symbol = normalize_symbol(row.get('symbol')) if isinstance(row, dict) else None
            if symbol and symbol not in seen:
                companies.append({**row, 'symbol': symbol})
                seen.add(symbol)
        # Seeds are starting coverage, not a whitelist. Failed discovery is explicit.
        for symbol in config.get('sector', {}).get('seed_symbols', config['symbols']):
            if symbol not in seen:
                companies.append({'symbol': symbol, 'name': symbol, 'themes': [], 'source_urls': [],
                                  'evidence_ids': [], 'discovery_reason': 'Configured sector seed; discovery verification unavailable.'})
                seen.add(symbol)
        companies = companies[:config.get('sector', {}).get('max_symbols', 80)]
        evidence = self._valid_evidence(result.get('evidence', []))[:config['max_evidence_per_cycle']]
        self.store.save_evidence(evidence)
        universe = {'id': 'ai_infrastructure', 'name': 'AI infrastructure', 'companies': companies,
                    'symbols': [row['symbol'] for row in companies], 'discovered_at': timestamp(),
                    'discovery_status': 'partial' if result.get('errors') else 'completed' if result else 'seed_coverage',
                    'evidence_ids': [row['id'] for row in evidence]}
        self.store.set_document('sector_universe', universe)
        held = {row['symbol'] for row in self.store.get_document('portfolio', {}).get('positions', [])}
        tracked = {row['symbol'] for row in self.store.strategies() if row.get('policy_version', 1) == 1 and row.get('status') in {'shadow', 'paper'}}
        selected_symbols = set(universe['symbols']) | held | tracked | set(config['research']['benchmark_symbols'])
        self.store.set_document('markets', [row for row in self._markets() if row['symbol'] in selected_symbols])
        if result.get('errors'):
            self.store.event('discovery', f'{len(result["errors"])} discovery source(s) unavailable; {len(companies)} AI equities retained.', 'warning')
        return universe

    @staticmethod
    def _valid_evidence(items):
        return [row for row in items if isinstance(row, dict) and all(isinstance(row.get(key), str) and row[key]
                for key in ('id', 'source_url', 'content', 'observed_at'))]

    def _retain_evidence(self, cycle, items):
        config = self.store.config()
        with self._state_lock:
            key = 'cycle_evidence_' + cycle['id']
            retained = set(self.store.get_document(key, []))
            sources = set(self.store.get_document('cycle_sources_' + cycle['id'], []))
            bounded = []
            for item in self._valid_evidence(items):
                if item['id'] in retained or len(retained) < config['max_evidence_per_cycle']:
                    retained.add(item['id'])
                    sources.add(item['source_url'])
                    bounded.append(item)
            self.store.save_evidence(bounded)
            self.store.set_document(key, sorted(retained))
            self.store.set_document('cycle_sources_' + cycle['id'], sorted(sources))
        return bounded

    def _payload(self, agent, cycle, inputs):
        config = cycle.get('config_snapshot', self.store.config())
        cutoff = cycle.get('information_cutoff_ms', round(time.time() * 1000))
        citations = list(dict.fromkeys(ref for item in inputs for ref in item.get('evidence_ids', [])))
        eligible = {row['id'] for row in temporal.evidence_as_of(self.store.evidence(citations), cutoff)}
        inputs = [item for item in inputs if item.get('evidence_ids') and set(item['evidence_ids']) <= eligible]
        payload = {'cycle_id': cycle['id'], 'layer': agent['layer'], 'topic': agent['topic'],
                   'as_of': timestamp(cutoff / 1000), 'information_cutoff_ms': cutoff,
                   'research_phase': 'development', 'inputs': copy.deepcopy(inputs), 'allowed_symbols': self._symbols(cycle),
                   'sector': {'id': 'ai_infrastructure', 'scope': 'Listed AI infrastructure equities: chips, memory, networking, cloud compute, data centers, power and cooling.'},
                   'max_output_tokens': config['max_output_tokens']}
        if agent['layer'] in {'analyst', 'strategist', 'executor'}:
            # Upper layers can inspect the original records instead of relying
            # on progressively compressed summaries from the layer below.
            payload['source_documents'] = temporal.evidence_as_of(self.store.evidence(citations), cutoff)
            payload['source_coverage'] = {
                'referenced_documents': len(citations),
                'supplied_documents': len(payload['source_documents']),
                'source_kinds': sorted({str(row.get('source_kind', 'unknown')) for row in payload['source_documents']}),
                'source_families': sorted({str(row.get('source_family') or row.get('source_url', 'unknown'))
                                           for row in payload['source_documents']})[:32],
            }
            payload['prior_research'] = [row for row in self.store.research_memory(limit=20)
                                       if row.get('updated_at_ms', row.get('registered_at_ms', cutoff + 1)) <= cutoff]
            # Targeted durable memory accompanies the old discovery summary.
            # Selection is by strategy/instrument relevance, not global recency.
            targets = {item.get('payload', {}).get('experiment_id') for item in inputs}
            symbols = {symbol for item in inputs for symbol in item.get('payload', {}).get('symbols', [])}
            symbols.update(item.get('payload', {}).get('symbol') for item in inputs)
            relevant = [row for row in self.store.experiments(limit=10000)
                        if row['registered_at_ms'] <= cutoff and (row['id'] in targets or row['spec'].get('symbol') in symbols)]
            if relevant:
                payload['strategy_dossiers'] = [self.memory.dossier(row['id'], task=agent['layer'],
                    max_bytes=max(2000, min(16000, config['max_input_bytes'] // 8))) for row in relevant[:2]]
                payload['strategy_dossier_omissions'] = max(0, len(relevant) - 2)
            payload['research_capabilities'] = {
                'families': ['momentum', 'mean_reversion', 'breakout', 'event_surprise'],
                'historical_phase': 'development', 'execution': 'paper',
                'source_acquisition': 'host_collected_before_information_cutoff',
                'missing_evidence': 'Identify the gap; do not invent a source, transcript, or statistic.',
            }
        if agent['layer'] == 'analyst':
            symbols = set()
            for item in inputs:
                symbols.update(item['payload'].get('symbols', []))
            markets = [row for row in self._research_markets(cycle) if not symbols or row['symbol'] in symbols]
            payload['markets'] = [temporal.market_as_of(row, cutoff) for row in markets[:16]]
            payload['market_coverage'] = {'available_symbols': len(markets), 'supplied_symbols': min(16, len(markets))}
            for row in payload['markets']:
                row['candles'] = [{key: bar.get(key) for key in ('end_ms', 'open', 'high', 'low', 'close', 'volume')}
                                  for bar in row.get('candles', [])[-100:]]
                row.pop('raw_candles', None)
                row.pop('corporate_actions', None)
            payload['validation_policy'] = ('All historical data and current stories are DEVELOPMENT evidence. '
                'Do not claim an untouched holdout. Freeze a falsifiable rule before prospective shadow evaluation.')
            for item in payload['inputs']:
                for source in item.get('payload', {}).get('sources', []):
                    source.pop('content', None)  # The original passage is supplied once in source_documents.
        if agent['layer'] == 'executor':
            portfolio = self.store.get_document('portfolio')
            payload['portfolio'] = {k: v for k, v in portfolio.items() if k not in {'trades', 'pnl_history'}}
            payload['risk'] = config['risk']
        # Preserve original evidence ahead of optional prior results and large
        # candle packets. Drop whole records instead of corrupting source text.
        while len(json.dumps(payload, ensure_ascii=False).encode()) > config['max_input_bytes'] and payload.get('prior_research'):
            payload['prior_research'].pop()
            payload['prior_research_truncated'] = True
        while len(json.dumps(payload, ensure_ascii=False).encode()) > config['max_input_bytes'] and payload.get('markets'):
            payload['markets'].pop()
            payload['market_coverage']['supplied_symbols'] = len(payload['markets'])
            payload['market_coverage']['truncated'] = True
        while len(json.dumps(payload, ensure_ascii=False).encode()) > config['max_input_bytes'] and payload.get('source_documents'):
            payload['source_documents'].pop()
            payload['source_coverage_truncated'] = True
        while len(json.dumps(payload, ensure_ascii=False).encode()) > config['max_input_bytes'] and payload['inputs']:
            payload['inputs'] = payload['inputs'][:-1]
            payload['inputs_truncated'] = True
        if len(json.dumps(payload, ensure_ascii=False).encode()) > config['max_input_bytes']:
            payload.pop('markets', None)
        if 'source_coverage' in payload:
            supplied = {row['id'] for row in payload.get('source_documents', [])}
            payload['source_coverage']['supplied_documents'] = len(supplied)
            payload['source_coverage']['unavailable_document_count'] = len(set(citations) - supplied)
            payload['source_coverage']['unavailable_evidence_ids'] = sorted(set(citations) - supplied)[:32]
        # Prior research is optional context and must not make a bounded packet
        # fail before a useful source-backed task can be submitted.
        while len(json.dumps(payload, ensure_ascii=False).encode()) > config['max_input_bytes'] and payload.get('prior_research'):
            payload['prior_research'].pop()
            payload['prior_research_truncated'] = True
        return payload

    def _model(self, agent, payload):
        config = self.store.config()
        model_agent = {**agent, 'max_output_tokens': config['max_output_tokens'], 'max_input_bytes': config['max_input_bytes']}
        request_size = len(json.dumps(payload, ensure_ascii=False).encode())
        request_size += len(agent.get('instructions', '')[:6000].encode()) + len(agent.get('mandate', '')[:1000].encode())
        cache_payload = {key: value for key, value in payload.items() if key not in {'cycle_id', 'as_of'}}
        cache_key = 'model_cache_' + identity([agent['id'], agent.get('instructions', ''), cache_payload])
        cached = self.store.get_document(cache_key)
        if cached is not None:
            self.store.event(agent['id'], 'Reused unchanged research input; no model call.', 'info')
            return cached
        request_id = self.store.save_snapshot('model_request', {'agent': model_agent, 'payload': payload},
                                              payload.get('information_cutoff_ms', round(time.time() * 1000)))
        reservation = self.component_budget.reserve('discovery', agent['model'], request_size, config['max_output_tokens'],
                                          context={'cycle_id': payload['cycle_id'], 'agent_id': agent['id'], 'request_snapshot': request_id})
        try:
            result = self.provider.run(model_agent, payload)
        except Exception as error:
            self.store.settle(reservation, getattr(error, 'usage', None))
            raise
        self.store.settle(reservation, result.get('usage'))
        output = result.get('output')
        if not isinstance(output, dict):
            raise ValueError('Provider returned an invalid layer envelope.')
        self.store.save_snapshot('model_response', {'request_snapshot': request_id, 'output': output,
            'usage': result.get('usage'), 'model': result.get('model', agent['model'])}, round(time.time() * 1000))
        self.store.set_document(cache_key, output)
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

    def _gather_sources(self, cycle, generation):
        """Finish public acquisition before model fan-out, so every scout can use the pool."""
        imported = self.store.evidence(self.store.get_document("external_evidence_ids", []))
        self._retain_evidence(cycle, imported)
        agents = self._cycle_agents(cycle, 'collector')
        def gather(agent):
            if not self._active(generation):
                return
            existing = self.store.job(cycle['id'], agent['id'])
            if existing and existing['status'] in {'collected', 'completed'}:
                return
            self.store.update_agent(agent['id'], status='working', task='Gathering AI-sector sources and equity charts')
            try:
                result = self.collector.collect(agent)
                if not self._active(generation):
                    return
                self._merge_markets(result.get('markets', []))
                result['evidence'] = self._retain_evidence(cycle, result.get('evidence', []))
                self.store.save_job(cycle['id'], agent['id'], 'collected', {'collected': result})
            except Exception as error:
                self.store.save_job(cycle['id'], agent['id'], 'collected', {'collected': {
                    'evidence': [], 'markets': [], 'errors': [type(error).__name__]}})
                self.store.event(agent['id'], 'Source acquisition unavailable; shared sector evidence remains available.', 'warning')
            finally:
                self.store.update_agent(agent['id'], status='idle', task='')
        with ThreadPoolExecutor(max_workers=min(cycle['max_concurrency'], len(agents)), thread_name_prefix='cascade-acquisition') as pool:
            futures = [pool.submit(gather, agent) for agent in agents]
            for future in as_completed(futures):
                future.result()
                if not self._active(generation):
                    for pending in futures:
                        pending.cancel()
                    break

    def _collector_evidence(self, agent, cycle, own):
        retained = self.store.evidence(self.store.get_document('cycle_evidence_' + cycle['id'], []))
        own_ids = {row['id'] for row in own}
        keywords = set(agent.get('topic', '').lower().split())
        def score(row):
            return (row['id'] in own_ids, agent.get('symbol') in row.get('symbols', []),
                    len(keywords.intersection((row.get('title', '') + ' ' + row.get('content', '')[:500]).lower().split())))
        # Stable rotation gives different scouts access to different long-tail sources.
        retained.sort(key=lambda row: identity([agent['id'], row['id']]))
        retained.sort(key=score, reverse=True)
        maximum = self.store.config()['ingestion'].get('shared_evidence_limit', 256)
        return retained[:maximum]

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
        own = self._retain_evidence(cycle, result.get('evidence', []))
        cutoff = cycle.get('information_cutoff_ms', round(time.time() * 1000))
        evidence = temporal.evidence_as_of(self._collector_evidence(agent, cycle, own), cutoff)
        if result.get('errors'):
            self.store.event(agent['id'], f'{len(result["errors"])} source fetch(es) unavailable; other sources continued.', 'warning')
        if not evidence:
            return []
        payload = self._payload(agent, cycle, [])
        payload['assignment'] = {key: agent.get(key) for key in ('symbol', 'topic', 'subtheme', 'source_shard')}
        payload['coverage'] = {'shared_evidence_count': len(self.store.get_document('cycle_evidence_' + cycle['id'], [])),
                               'assigned_evidence_count': len(own), 'supplied_evidence_count': len(evidence)}
        payload['evidence'] = evidence
        symbols = self._symbols(cycle)
        assigned = set(symbols[agent.get('source_shard', 0)::max(1, agent.get('source_shard_count', 24))])
        assigned.add(agent.get('symbol'))
        payload['markets'] = [temporal.market_as_of(row, cutoff) for row in self._research_markets(cycle) if row['symbol'] in assigned]
        for market in payload['markets']:
            market['available_closed_bars'] = len(market.get('candles', []))
            market['candles'] = market.get('candles', [])[-256:]
        # Keep article evidence available even when a small operator packet cap
        # cannot accommodate every chart. The full series stays in the journal.
        limit = self.store.config()['max_input_bytes']
        while len(json.dumps(payload, ensure_ascii=False).encode()) > limit and payload['markets']:
            payload['markets'].pop()
        while len(json.dumps(payload, ensure_ascii=False).encode()) > limit and payload['evidence']:
            payload['evidence'].pop()
        payload['coverage']['supplied_evidence_count'] = len(payload['evidence'])
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
            if not isinstance(symbols, list) or any(symbol not in self._symbols(cycle) for symbol in symbols):
                continue
            inferred = list(dict.fromkeys(symbol for ref in refs for symbol in allowed[ref].get('symbols', []) if symbol in self._symbols(cycle)))
            selected = {**finding, 'symbols': symbols or inferred,
                        'sources': [{k: allowed[ref].get(k) for k in ('id', 'source_url', 'source_kind', 'title', 'observed_at', 'published_at', 'numeric')} for ref in refs[:8]]}
            items.append(handoff(cycle['id'], 'collector', agent['id'], selected, refs, []))
        return items

    def _cycle_agents(self, cycle, layer):
        agents = [copy.deepcopy(row) for row in cycle['roster'] if row['layer'] == layer]
        settings = cycle.get('config_snapshot', self.store.config())['research']
        active = min(len(agents), settings.get('active_' + layer + 's', len(agents)))
        agents = agents[:active]
        if layer == 'collector':
            for index, agent in enumerate(agents):
                agent.update(source_shard=index, source_shard_count=active)
        return agents

    def _material_fingerprint(self, cycle):
        evidence = self.store.evidence(self.store.get_document('cycle_evidence_' + cycle['id'], []))
        # Quote ticks, fetch times and regenerated IDs cannot trigger expensive daily research.
        documents = sorted({row.get('content_hash') or identity(row.get('content')) for row in evidence
                            if row.get('source_kind') not in {'chart', 'market'}})
        histories = [{key: row.get(key) for key in ('symbol', 'security_id', 'candles', 'raw_candles',
                     'raw_history_basis', 'corporate_actions', 'event_features')} for row in self._research_markets(cycle)]
        config = cycle.get('config_snapshot', self.store.config())
        return identity({'documents': documents, 'histories': histories, 'symbols': cycle['symbols'],
                         'policy': simulation.execution_policy(config), 'research': config['research'],
                         'roles': [identity(row.get('instructions', '')) for row in cycle['roster']]})

    @staticmethod
    def _active_roster(config, symbols):
        agents = roster({**config, 'symbols': symbols})
        analysts = [row['id'] for row in agents if row['layer'] == 'analyst'][:config['research']['active_analysts']]
        for index, row in enumerate(agent for agent in agents if agent['layer'] == 'collector'):
            row['parent_id'] = analysts[index % len(analysts)]
        return agents

    def _analyze(self, agent, cycle, inputs, generation):
        payload = self._payload(agent, cycle, inputs)
        inputs = payload['inputs']
        if not inputs or not self._active(generation):
            return []
        output = self._model(agent, payload)
        refs = {ref for item in inputs for ref in item['evidence_ids']}
        documents = {row['id']: row for row in payload.get('source_documents', [])}
        proposals = []
        for row in self._objects(output, 'edges', 12):
            citations = row.get('evidence_ids')
            if isinstance(citations, list) and citations and all(isinstance(ref, str) and ref in refs for ref in citations):
                claims = row.get('supporting_claims', [])
                horizon = row.get('prediction_horizon_sessions')
                supported = (isinstance(claims, list) and bool(claims) and
                    any(isinstance(claim, dict) and claim.get('stance') == 'support' for claim in claims) and
                    all(isinstance(claim, dict) and claim.get('evidence_id') in citations
                        and claim['evidence_id'] in documents and isinstance(claim.get('excerpt'), str)
                        and bool(claim['excerpt'].strip())
                        and claim['excerpt'] in documents[claim['evidence_id']].get('content', '') for claim in claims))
                if (row.get('symbol') in self._symbols(cycle) and supported
                    and isinstance(horizon, (int, float)) and not isinstance(horizon, bool)
                    and math.isfinite(horizon) and horizon == int(horizon) and 1 <= horizon <= 100
                    and isinstance(row.get('mechanism'), str) and row['mechanism'].strip()
                    and isinstance(row.get('invalidation'), str) and row['invalidation'].strip()):
                    market = next((market for market in self._research_markets(cycle) if market['symbol'] == row['symbol']), {})
                    if market.get('security_id'):
                        row = {**row, 'security_id': market['security_id']}
                    proposals.append({**row, 'parameters': {k: v for k, v in row.get('parameters', {}).items() if v is not None}})
                    continue
            self.store.save_strategy({'id': 'proposal_' + identity([cycle['id'], agent['id'], row]),
                'name': str(row.get('hypothesis', 'Unsupported proposal'))[:160], 'symbol': str(row.get('symbol', '')),
                'family': str(row.get('kind', 'unknown')), 'status': 'rejected', 'phase': 'development',
                'reason': 'Proposal lacks a supported source excerpt, mechanism, bounded horizon, invalidation, or allowed symbol.',
                'created_at': timestamp(), 'agent_id': agent['id'], 'spec': None,
                'net_return': None, 'sharpe': None, 'max_drawdown': None, 'trades': None})
        if not proposals:
            return []
        return [handoff(cycle['id'], 'analyst_proposal', agent['id'], proposal, proposal['evidence_ids'], [item['id'] for item in inputs]) for proposal in proposals]

    def _validate_analyst_batch(self, cycle, generation=None):
        """Register before evaluation. All historical outcomes are development evidence."""
        config = cycle.get('config_snapshot', self.store.config())
        self._research_state_cache = None
        checkpoint = self.store.get_document('validation_' + cycle['id'])
        if checkpoint is None:
            proposed = self.store.handoffs(cycle['id'], 'analyst_proposal')
            maximum = config['research']['max_candidates_per_cycle']
            deferred = proposed[maximum:]
            proposed = proposed[:maximum]
            # Keep each analyst attempt, but evaluate a bounded, predeclared
            # neighborhood of every distinct source-supported mechanism.
            expanded = []
            seen = set()
            variant_groups = []
            for source in proposed:
                try:
                    variants = expand_hypothesis(source['payload'],
                        max_candidates=config['research']['variants_per_hypothesis'])
                    digest = analytics.spec_hash(source['payload'])
                    if digest not in seen:
                        variant_groups.append((source, variants[1:]))
                    seen.add(digest)
                except (ValueError, TypeError, KeyError):
                    pass
            for index in range(config['research']['variants_per_hypothesis'] - 1):
                for source, variants in variant_groups:
                    if index >= len(variants) or len(proposed) + len(expanded) >= maximum:
                        continue
                    variant = variants[index]
                    digest = analytics.spec_hash(variant)
                    if digest in seen:
                        continue
                    seen.add(digest)
                    expanded.append(handoff(cycle['id'], 'analyst_proposal', source['producer'], variant,
                                            source['evidence_ids'], source['input_ids']))
            proposed += expanded
            with self._trial_lock:
                trials = self.store.get_document('statistical_trial_count', 0) + len(proposed) + len(deferred)
                checkpoint = {'trial_count': max(1, trials), 'proposals': proposed,
                              'generated_variants': len(expanded), 'deferred_proposal_ids': [row['id'] for row in deferred],
                              'market_snapshot': self.store.freeze_markets(self._research_markets(cycle), cycle['information_cutoff_ms']),
                              'validated_at': timestamp()}
                self.store.commit_validation(cycle['id'], trials, checkpoint)
        if 'markets' not in checkpoint:
            checkpoint['markets'] = self.store.market_snapshot(checkpoint['market_snapshot'])
        policy = simulation.execution_policy(config)
        code_hash = identity({p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in Path(__file__).parent.glob('*.py')})
        unique = set()
        for source in checkpoint['proposals']:
            try:
                unique.add(analytics.spec_hash(source['payload']))
            except (ValueError, TypeError):
                pass
        allocated_cost = self.store.cycle_cost(cycle['id']) / max(1, len(unique))
        prepared, snapshots = [], {}
        # Register the entire batch before any outcome is visible.
        for source in checkpoint['proposals']:
            proposal = source['payload']
            registered = None
            try:
                spec = analytics.canonical_spec(proposal)
                references = tuple(sorted(proposal['evidence_ids']))
                if references not in snapshots:
                    evidence = self.store.evidence(proposal['evidence_ids'])
                    snapshot = self.store.save_snapshot('experiment_inputs', {
                        'market_snapshot': checkpoint.get('market_snapshot'), 'evidence': evidence,
                        'universe': self.store.get_document('sector_universe', {})}, cycle['information_cutoff_ms'])
                    snapshots[references] = (snapshot, evidence)
                snapshot, evidence = snapshots[references]
                registered = self.store.register_experiment(spec, policy=policy, context={
                    'cycle_id': cycle['id'], 'agent_id': source['producer'], 'hypothesis': proposal.get('hypothesis'),
                    'input_snapshot': snapshot, 'evidence_ids': proposal['evidence_ids'], 'code_version': code_hash,
                    'data_fingerprint': checkpoint.get('market_snapshot'),
                    'source_families': sorted({row.get('source_family') or row['source_url'] for row in evidence}),
                    'research_arm': {'collectors': config['research']['active_collectors'], 'analysts': config['research']['active_analysts']},
                    'information_cutoff_ms': cycle['information_cutoff_ms'], 'config': config,
                    'prompt_versions': {row['id']: identity(row.get('instructions', '')) for row in cycle['roster']},
                    'models': {row['id']: row['model'] for row in cycle['roster']},
                    'historical_phase': 'development', 'universe_scope': 'current_membership_only'})
            except (ValueError, TypeError, KeyError) as error:
                self.store.event('research', 'Invalid candidate retained as a rejected development result: ' + str(error)[:200], 'warning')
            prepared.append((source, registered))
        charged = set()
        from .datasets import _dataset_alpha_allocation, _refresh_search_correction
        for source, registered in prepared:
            if generation is not None and not self._active(generation):
                return False
            proposal = source['payload']
            cache_key = identity([registered['id'], checkpoint.get('market_snapshot'), code_hash, config]) if registered else None
            edge = self.store.get_document('development_report_' + cache_key) if cache_key else None
            cache_hit = edge is not None
            alpha, dataset_index = (_dataset_alpha_allocation(self.store, registered, checkpoint.get('market_snapshot'))
                                    if registered else (None, None))
            if edge is None:
                edge = analytics.validate_edges([proposal], checkpoint['markets'], trial_count=checkpoint['trial_count'],
                    config=config, alpha_budget=alpha)[0]
                if registered:
                    _refresh_search_correction(edge, checkpoint['trial_count'], alpha)
                if cache_key:
                    self.store.set_document('development_report_' + cache_key, edge)
            else:
                _refresh_search_correction(edge, checkpoint['trial_count'], alpha)
                # A reused economic rule keeps the current attempt's evidence
                # and text while its measured outcomes remain unchanged.
                edge.update(evidence_ids=proposal['evidence_ids'], hypothesis=proposal.get('hypothesis', ''))
                edge['id'] = 'edge-' + hashlib.sha256(json.dumps(proposal, sort_keys=True, default=str).encode()).hexdigest()[:20]
            edge.update(agent_id=source['producer'], created_at=checkpoint['validated_at'], development_only=True)
            if registered:
                edge['experiment_id'] = registered['id']
                self.store.record_evaluation(registered['id'], {'phase': 'development', 'cycle_id': cycle['id'],
                    'edge': edge, 'input_ids': source['input_ids'],
                    'candidate_cache_key': cache_key, 'cache_hit': cache_hit,
                    'data_fingerprint': checkpoint.get('market_snapshot'),
                    'alpha_budget': alpha, 'dataset_alpha_allocation_index': dataset_index,
                    'input_snapshot': snapshots[tuple(sorted(proposal['evidence_ids']))][0],
                    'information_cutoff_ms': cycle['information_cutoff_ms'],
                    'research_cost_usd': 0. if registered['id'] in charged else allocated_cost})
                charged.add(registered['id'])
            metrics = edge.get('metrics', {})
            display = {'id': 'edge_' + source['id'], 'name': str(edge.get('hypothesis', 'Development hypothesis'))[:160],
                       'symbol': edge['symbol'], 'family': edge['kind'], 'status': 'development',
                       'reason': 'Development screen: ' + edge['reason'], 'created_at': edge['created_at'],
                       'agent_id': source['producer'], 'experiment_id': registered['id'] if registered else None,
                       'phase': 'development', 'development_only': True,
                       'spec': {'symbol': edge['symbol'], 'kind': edge['kind'], 'parameters': edge.get('parameters', {})},
                       **{key: metrics.get(key) for key in ('net_return', 'sharpe', 'max_drawdown', 'trades')}}
            self.store.save_strategy(display)
            # Shadow experiments test canonical, data-supported hypotheses, including historical failures.
            # No historical p-value can promote them into the aggregate paper account.
            if registered:
                try:
                    market = next(row for row in checkpoint['markets'] if row['symbol'] == registered['spec']['symbol'])
                    if len(analytics.closed_candles(market)) < analytics.MIN_CANDLES:
                        continue
                    self.store.save_handoff(handoff(cycle['id'], 'analyst', source['producer'], edge,
                                                   edge['evidence_ids'], source['input_ids']))
                except (ValueError, StopIteration):
                    continue
        return True

    def _strategize(self, agent, cycle, inputs, generation):
        payload = self._payload(agent, cycle, inputs)
        if not payload['inputs'] or not self._active(generation):
            return []
        if self.store.config()['research']['use_model_selector']:
            self._model(agent, payload)  # Logged advisory ablation; cannot rewrite frozen rules.
        items = []
        seen = set()
        for item in payload['inputs']:
            edge = item['payload']
            identifier = edge.get('experiment_id')
            experiment = self.store.experiment(identifier) if identifier else None
            if not experiment or identifier in seen or experiment['status'] == 'retired':
                continue
            seen.add(identifier)
            spec = experiment['spec']
            frozen_policy_hash = hashlib.sha256(json.dumps(experiment['policy'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            metrics = edge.get('metrics', {})
            strategy = {'id': 'strategy_' + identifier, 'experiment_id': identifier,
                'name': str(experiment['context'].get('hypothesis', 'Frozen prospective candidate'))[:120],
                'symbol': spec['symbol'], 'family': spec['kind'],
                'status': 'paper' if experiment['status'] == 'promoted' else 'shadow' if experiment['status'] == 'shadow' else 'queued', 'spec': spec,
                'policy': experiment['policy'], 'policy_hash': frozen_policy_hash,
                'registered_at_ms': experiment['registered_at_ms'],
                'created_at': timestamp(experiment['registered_at_ms'] / 1000), 'agent_id': agent['id'],
                'edge_ids': [edge['id']], 'evidence_ids': experiment['context'].get('evidence_ids', edge['evidence_ids']),
                'validation': {'passed': True, 'spec_hash': analytics.spec_hash(spec),
                    'policy_hash': frozen_policy_hash, 'development_only': True},
                'reason': 'Frozen shadow experiment; historical screens are development evidence.',
                'phase': 'prospective_shadow',
                **{key: metrics.get(key) for key in ('net_return', 'sharpe', 'max_drawdown', 'trades')}}
            self.store.save_strategy(strategy)
            items.append(handoff(cycle['id'], 'strategist', agent['id'], strategy, edge['evidence_ids'], [item['id']]))
        return items

    def _authorize(self, agent, cycle, inputs, generation):
        payload = self._payload(agent, cycle, inputs)
        if not self._active(generation):
            return []
        # Legacy daily-equity paper execution remains deterministic. Operational
        # Sol decisions belong to the independent Bybit execution lane, with
        # tool results and current host checks; never pay for ignored advice.
        with self._execution_lock:
            if not self._active(generation):
                return []
            self._execute([])
        return []

    def _execute(self, decisions):
        config = self.store.config()
        self._research_state_cache = None
        self._advance_shadows(config)
        strategies = []
        for strategy in self.store.strategies():
            identifier = strategy.get('experiment_id')
            experiment = self.store.experiment(identifier) if identifier else None
            if experiment and experiment['policy'].get('version') == 1 and experiment['status'] == 'promoted' and strategy.get('spec'):
                strategies.append({**strategy, 'status': 'paper', 'forward_eligible': True})
        decisions = [{'strategy_id': row['id'], 'action': 'activate', 'reason': 'Registered prospective admission'} for row in strategies]
        self.store.set_document('active_decisions', decisions)
        before = self.store.get_document('portfolio')
        after = execution.execute(before, strategies, decisions, self._markets(), config)
        self.store.set_document('portfolio', after)
        if len(after.get('trades', [])) > len(before.get('trades', [])):
            self.store.event('executor', 'Recorded simulated fills under the registered portfolio policy.')

    def _advance_shadows(self, config):
        markets = self._markets()
        all_strategies = [row for row in self.store.strategies() if row.get('policy_version', 1) == 1]
        active = [row for row in all_strategies if row.get('status') in {'shadow', 'paper'}
                  and row.get('experiment_id') and self.store.experiment(row['experiment_id'])['status'] in {'shadow', 'promoted'}]
        active.sort(key=lambda row: row['registered_at_ms'])
        for strategy in active[config['research']['max_shadow_candidates']:]:
            self.store.update_experiment_status(strategy['experiment_id'], 'retired', 'Operator reduced the active shadow budget.')
            self.store.save_strategy({**strategy, 'status': 'retired', 'reason': 'Operator reduced the active shadow budget.'})
        active = active[:config['research']['max_shadow_candidates']]
        slots = max(0, config['research']['max_shadow_candidates']-len(active))
        queued = sorted((row for row in all_strategies if row.get('status') == 'queued'), key=lambda row: row['registered_at_ms'])
        for strategy in queued[:slots]:
            if strategy['policy_hash'] != simulation.policy_hash(config):
                self.store.update_experiment_status(strategy['experiment_id'], 'retired', 'Queued policy changed before activation.')
                strategy.update(status='retired')
            else:
                self.store.update_experiment_status(strategy['experiment_id'], 'shadow', 'Activated in registration order within the shadow budget.')
                strategy.update(status='shadow')
                active.append(strategy)
            self.store.save_strategy(strategy)
        candidates = {row['experiment_id']: row for row in active}
        for identifier, strategy in candidates.items():
            experiment = self.store.experiment(identifier)
            if not experiment or experiment['status'] in {'retired', 'rejected'}:
                continue
            if strategy.get('policy_hash') != simulation.policy_hash(config):
                self.store.update_experiment_status(identifier, 'retired', 'Execution policy changed; new registration required.')
                self.store.save_strategy({**strategy, 'status': 'retired', 'reason': 'Execution policy changed; new registration required.'})
                continue
            key = 'shadow_portfolio_' + identifier
            before = self.store.get_document(key, execution.initial_portfolio())
            shadow_strategy = {**strategy, 'status': 'paper', 'shadow': True}
            decision = {'strategy_id': strategy['id'], 'action': 'activate', 'reason': 'Frozen independent shadow candidate'}
            after = execution.execute(before, [shadow_strategy], [decision], markets, config)
            self.store.set_document(key, after)
            if after.get('data_conflict'):
                self.store.update_experiment_status(identifier, 'retired', 'A corporate-action data revision requires account reconciliation.')
                self.store.save_strategy({**strategy, 'status':'retired', 'reason':'Corporate-action data conflict.'})
                continue
            if after.get('settled_securities'):
                self.store.update_experiment_status(identifier, 'retired', 'The researched security reached a terminal corporate settlement.')
                self.store.save_strategy({**strategy, 'status':'retired', 'reason':'Terminal corporate settlement.'})
                continue
            baselines = {}
            symbols = [strategy['symbol']] + config['research']['benchmark_symbols']
            for label, symbol in zip(('benchmark_return', 'market_return', 'sector_return'), symbols):
                baseline_key = 'shadow_baseline_book_' + identifier + '_' + label
                baseline_spec = {'symbol': symbol, 'kind': 'momentum', 'parameters': {'lookback': 3, 'threshold': 0.}}
                identity_key = 'baseline_identity_' + identifier + '_' + label
                source_market = next((market for market in markets if market['symbol'] == symbol), {})
                security = self.store.get_document(identity_key)
                if security is None and source_market.get('security_id'):
                    security = source_market['security_id']
                    self.store.set_document(identity_key, security)
                if security:
                    baseline_spec['security_id'] = security
                baseline_strategy = {**shadow_strategy, 'id': 'baseline_' + identifier + '_' + label,
                    'spec': baseline_spec, '_benchmark_buy_hold': True,
                    'validation': {**shadow_strategy['validation'], 'spec_hash': analytics.spec_hash(baseline_spec)}}
                baseline_book = self.store.get_document(baseline_key, execution.initial_portfolio())
                baseline_book = execution.execute(baseline_book, [baseline_strategy], [], markets, config)
                self.store.set_document(baseline_key, baseline_book)
                if any(row['symbol'] == symbol for row in markets):
                    baselines[label] = baseline_book
            self._record_shadow_session(experiment, strategy, after, baselines, markets, config)

    def _record_shadow_session(self, experiment, strategy, portfolio, baselines, markets, config):
        market = next((row for row in markets if row['symbol'] == strategy['symbol']), None)
        if not market or not portfolio.get('valuation_complete', False):
            return
        session = market.get('session') or {}
        close_ms = session.get('close_ms')
        quote_ms = market.get('as_of_ms', 0)
        now_ms = round(time.time() * 1000)
        if (not close_ms or not experiment['registered_at_ms'] < close_ms <= now_ms
            or not close_ms-120000 <= quote_ms <= close_ms+120000 or not 0 <= now_ms-quote_ms <= 120000):
            return
        observation_key = 'shadow_session_' + experiment['id']
        prior = self.store.get_document(observation_key)
        if prior and prior['session_ms'] >= close_ms:
            return
        equity = portfolio['equity']
        previous_equity = prior['equity'] if prior else portfolio['initial_equity']
        source_cost, source_cost_complete = self.store.research_source_cost(experiment['id'])
        research_cost = self.store.research_cost(experiment['id']) + source_cost
        previous_cost = prior.get('research_cost_paid_usd', 0.) if prior else 0.
        elapsed_days = (close_ms - (prior['session_ms'] if prior else experiment['registered_at_ms'])) / 86400000
        observation = {'session_ms': close_ms, 'timestamp_ms': close_ms, 'observed_at_ms': now_ms,
            'equity': equity, 'initial_equity': portfolio['initial_equity'],
            'return': equity / previous_equity - 1, 'net_return': equity / previous_equity - 1,
            'baseline_equities': {label: book['equity'] for label, book in baselines.items()},
            'completed_trades': sum(row.get('side') in {'sell', 'settlement'} and row.get('position_closed', True)
                and row.get('strategy_id') == strategy['id'] and row.get('timestamp_ms', 0) > experiment['registered_at_ms']
                for row in portfolio.get('trades', [])),
            'valuation_complete': True, 'fees_paid': portfolio.get('fees_paid', 0),
            'research_cost_usd': max(0., research_cost-previous_cost), 'research_cost_paid_usd': research_cost,
            'risk_halted': portfolio.get('risk_halted', False), 'source_cost_complete': source_cost_complete,
            'data_cost_usd': config['research']['data_cost_usd_per_day'] * elapsed_days,
            'infrastructure_cost_usd': config['research']['infrastructure_cost_usd_per_day'] * elapsed_days}
        for label, baseline in baselines.items():
            index = ('benchmark_return', 'market_return', 'sector_return').index(label)
            symbol = ([strategy['symbol']] + config['research']['benchmark_symbols'])[index]
            baseline_market = next((row for row in markets if row['symbol'] == symbol), {})
            if (baseline.get('valuation_complete') and (baseline_market.get('session') or {}).get('close_ms') == close_ms
                and close_ms-120000 <= baseline_market.get('as_of_ms', 0) <= close_ms+120000
                and 0 <= now_ms-baseline_market.get('as_of_ms', 0) <= 120000):
                previous = prior.get('baseline_equities', {}).get(label, portfolio['initial_equity']) if prior else portfolio['initial_equity']
                observation[label] = baseline['equity'] / previous - 1
        captured_baselines = {label: book for label, book in baselines.items() if label in observation}
        observation['baseline_equities'] = {label: book['equity'] for label, book in captured_baselines.items()}
        try:
            observation = self.store.record_captured_forward(
                experiment['id'], observation, portfolio, captured_baselines, markets)
        except ValueError as error:
            self.store.event('research', 'Forward session capture rejected: ' + str(error), 'warning')
            return
        self.store.set_document(observation_key, observation)
        updated = self.store.experiment(experiment['id'])
        report = experiments.forward_report(updated.get('observations', []),
            {**config, 'research': {**config['research'], 'alpha_budget': experiment['alpha_budget'],
                'forward_block_sessions': max(config['research']['forward_block_sessions'],
                    config['research']['max_holding_sessions'], strategy['spec']['parameters']['lookback'])}})
        if (experiment['status'] == 'promoted' and report['status'] == 'inconclusive'
            and report['sessions'] % config['research']['review_interval_sessions'] == 0):
            report['status'] = 'retired'
            report['reasons'].insert(0, 'Previously promoted candidate no longer passes its scheduled prospective review')
        self.store.record_evaluation(experiment['id'], {'phase': 'forward', 'session_ms': close_ms, 'report': report})
        self.store.set_document('forward_report_' + experiment['id'], report)
        outcome = report.get('status')
        if outcome in {'promoted', 'retired'}:
            self.store.update_experiment_status(experiment['id'], outcome, '; '.join(report.get('reasons', [])))
            strategy.update(status='paper' if outcome == 'promoted' else 'retired', forward_report=report,
                            reason='; '.join(report.get('reasons', [])))
            self.store.save_strategy(strategy)

    def _research_markets(self, cycle):
        snapshot = self.store.get_document('research_snapshot_' + cycle['id'], [])
        if not isinstance(snapshot, dict):
            return snapshot
        identifier = snapshot['snapshot_id']
        # Content-addressed snapshots cannot change. Decode their large candle
        # series once per cycle; callers must treat this private view as read-only.
        with self._state_lock:
            if self._research_market_cache_id != identifier:
                markets = self.store.market_snapshot(identifier)
                self._research_market_cache = markets
                self._research_market_cache_id = identifier
            return self._research_market_cache

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
                self.last_error = 'Configure the runtime OpenAI credential to start AI equity research.'
                self.next_cycle_at = time.time() + 30
                return False
            cycle = self.store.get_document('active_cycle')
            if not cycle or cycle.get('status') == 'completed':
                config = self.store.config()
                if not self._collector_injected and self._collector_config_key != identity(config):
                    self.collector = SourceCollector(config)
                    self._collector_config_key = identity(config)
                self.status = 'discovery'
                universe = self._discover(generation)
                if universe is None:
                    return False
                agents = self._active_roster(config, universe['symbols'])
                self.store.seed_agents(agents)
                cycle = {'id': uuid.uuid4().hex, 'status': 'running', 'stage': 0, 'started_at': timestamp(), 'roster': agents,
                         'max_concurrency': config['max_concurrency'], 'symbols': universe['symbols'],
                         'protocol_version': 1, 'config_snapshot': config}
                self.store.set_document('active_cycle', cycle)
                self.store.set_document('cycle_evidence_' + cycle['id'], universe.get('evidence_ids', []))
                discovery_evidence = self.store.evidence(universe.get('evidence_ids', []))
                self.store.set_document('cycle_sources_' + cycle['id'], sorted({row['source_url'] for row in discovery_evidence}))
            for index in range(cycle['stage'], len(LAYER_ORDER)):
                if not self._active(generation):
                    return False
                layer = LAYER_ORDER[index]
                if layer == 'collector':
                    self.status = 'acquisition'
                    self._gather_sources(cycle, generation)
                    if not self._active(generation):
                        return False
                    if 'information_cutoff_ms' not in cycle:
                        cycle['information_cutoff_ms'] = round(time.time() * 1000)
                        self.store.set_document('active_cycle', cycle)
                        cutoff = cycle['information_cutoff_ms']
                        evidence = temporal.evidence_as_of(self.store.evidence(
                            self.store.get_document('cycle_evidence_' + cycle['id'], [])), cutoff)
                        selected = set(self._symbols(cycle)) | set(self.store.config()['research']['benchmark_symbols'])
                        snapshot = [temporal.market_as_of(row, cutoff) for row in self._markets() if row['symbol'] in selected]
                        for market in snapshot:
                            market['event_features'] = temporal.event_features(
                                [row for row in evidence if market['symbol'] in row.get('symbols', [])], cutoff)
                        feature_index = self.store.get_document('event_features_by_symbol', {})
                        for market in snapshot:
                            existing = {feature['id']: feature for feature in feature_index.get(market['symbol'], [])}
                            existing.update({feature['id']: feature for feature in market['event_features']})
                            feature_index[market['symbol']] = list(existing.values())[-2000:]
                        self.store.set_document('event_features_by_symbol', feature_index)
                        self._merge_markets(self._markets())
                        market_snapshot = self.store.freeze_markets(snapshot, cutoff)
                        self.store.set_document('research_snapshot_' + cycle['id'], {'snapshot_id':market_snapshot})
                        self.store.set_document('coverage_' + cycle['id'], temporal.coverage_report(evidence, snapshot))
                        self.store.save_snapshot('research_cycle', {'cycle': cycle, 'evidence': evidence,
                            'market_snapshot': market_snapshot}, cutoff)
                    fingerprint = self._material_fingerprint(cycle)
                    cycle['material_fingerprint'] = fingerprint
                    if self.store.get_document('last_material_fingerprint') == fingerprint:
                        cycle.update(status='completed', finished_at=timestamp(), skipped_reason='No material change to source content, daily history, rules or policy.')
                        self.store.set_document('active_cycle', cycle)
                        self.store.event('research', cycle['skipped_reason'] + ' Model work reused.')
                        self.next_cycle_at = 0.  # The persisted dynamic plan chooses subsequent work.
                        self.status = 'running'
                        return True
                self.status = layer
                cycle.update(stage=index, status='running')
                self.store.set_document('active_cycle', cycle)
                agents = self._cycle_agents(cycle, layer)
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
                    if not self._validate_analyst_batch(cycle, generation):
                        return False
                cycle['stage'] = index + 1
                self.store.set_document('active_cycle', cycle)
                count = len(self.store.handoffs(cycle['id'], layer))
                self.store.event(layer, f'Layer complete: {len(agents)} agents, {count} qualified handoffs.')
            cycle.update(status='completed', finished_at=timestamp())
            self.store.set_document('active_cycle', cycle)
            self.store.set_document('last_cycle_at', cycle['finished_at'])
            self.store.set_document('last_material_fingerprint', cycle.get('material_fingerprint'))
            self.status = 'running'
            self.last_error = None
            self.next_cycle_at = 0.  # Compatibility field; no interval drives another cycle.
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
        markets = self.store.markets(history=False)
        cycle = self.store.get_document('active_cycle', {}) or {}
        evidence_ids = self.store.get_document('cycle_evidence_' + cycle.get('id', ''), [])
        source_urls = self.store.get_document('cycle_sources_' + cycle.get('id', ''), [])
        universe = self.store.get_document('sector_universe', {'id': 'ai_infrastructure', 'name': 'AI infrastructure',
                    'symbols': config['symbols'], 'companies': [], 'discovery_status': 'pending'})
        research_summary = self._research_state_cache
        if research_summary is None or time.monotonic()-self._research_state_cache_at > 30:
            summary = self.store.research_summary()
            identifiers = {row.get('experiment_id') for row in self.store.strategies() if row.get('experiment_id')}
            summary['forward_reports'] = [{'experiment_id': ident, **self.store.get_document('forward_report_'+ident,{})}
                                          for ident in sorted(identifiers)]
            self._research_state_cache = summary
            research_summary = summary
            self._research_state_cache_at = time.monotonic()
        return {
            'runtime': {'status': 'paused' if self.paused else self.status, 'mode': config['bybit']['mode'], 'started_at': self.started_at,
                        'next_cycle_at': None if self.paused or not self.next_cycle_at else timestamp(self.next_cycle_at), **self._credentials,
                        'credentials_ready': self._key_configured,
                        'last_error': self.last_error, 'live_trading_available': True},
            'operations': self.operations_state(),
            'budget': self.store.budget(), 'agents': agents, 'agent_roles': roles,
            'research': {**research_summary,
                'protocol': config['research'], 'phase': 'prospective_shadow',
                'coverage': self.store.get_document('coverage_'+cycle.get('id','')) or temporal.coverage_report([], markets),
                'historical_results_are_development': True},
            'strategies': [row for row in self.store.strategies() if row.get('symbol') in self._symbols() or row.get('policy_version') == 2], 'portfolio': self.store.get_document('portfolio'),
            'markets': [{key: row.get(key) for key in ('symbol', 'price', 'change_24h', 'funding_rate', 'updated_at',
                                                      'asset_class', 'currency', 'exchange')} for row in markets],
            'events': self.store.events(), 'config': config,
            'cascade': {'architecture': 'reverse_cascade', 'version': 2, 'sector': universe,
                        'collection': {'evidence_count': len(evidence_ids), 'source_count': len(source_urls),
                                       'active_collector_count': min(config['research']['active_collectors'], config['layers']['collector']['count']),
                                       'collector_count': config['layers']['collector']['count']},
                        'layers': [{'id': layer, **config['layers'][layer]} for layer in LAYER_ORDER],
                        'cycle': {key: value for key, value in cycle.items() if key != 'roster'},
                        'handoff_counts': {layer: len(self.store.handoffs(cycle.get('id', ''), layer)) for layer in LAYER_ORDER},
                        'statistical_trial_count': self.store.get_document('statistical_trial_count', 0)},
        }
