"""Independent event-driven runtime lanes with durable work and model boundaries.

Construction is offline. Only ``run`` starts services, and discretionary work
requires the runtime's explicit Start control. Account protection does not
depend on the scheduler, discovery, review or execution model finishing.
"""
from __future__ import annotations

import copy
import json
import math
from datetime import datetime
import threading
import time

from .agent_runtime import ResponsesAgent
from .bybit import BybitAdapter
from .bybit_discovery import BybitDiscovery
from .domain import identity, timestamp
from .event_bus import EventBus
from .live_research import LiveResearch
from .market_data import MarketDataService, _PublicRESTTransport
from .operations_console import projection
from .paper_exchange import PaperExchange
from .research_service import ResearchWorker
from .research_tape import ResearchTape
from .risk_controls import ACTIVE_ORDERS
from .store import BudgetExceeded
from .support_swarm import SupportSwarm
from .trading_modes import is_sol_mode, mode_settings


def epoch(value):
    if isinstance(value, (int, float)):
        return value / 1000 if value > 100_000_000_000 else value
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() if value else 0.


class ContinuousServices:
    def __init__(self, runtime, *, scheduler=None, market=None, adapter=None, execution=None,
                 agents=None, calendar_loader=None, clock=None):
        self.runtime, self.store = runtime, runtime.store
        self.clock = clock or self.store._clock or time.time
        self.stop = runtime._stop
        self._wake = threading.Event()
        self._plan_wake = threading.Event()
        self._account_wake = threading.Event()
        self._threads = []
        self._errors = {}
        self._error_lock = threading.RLock()
        self._config = self.store.config()
        self.agents = agents or ResponsesAgent(self.store, runtime.provider, clock=self.clock)
        self.events = EventBus(self.store, clock=self.clock)
        self.memory = runtime.memory
        self.memory.recover()
        self.live = LiveResearch(self.store, clock=self.clock)
        self.tape = ResearchTape(self.store)
        self.market = market or MarketDataService(self.store, clock=self.clock, on_event=self.on_market)
        self.market.on_event = self.on_market
        for strategy in self.live.strategies():
            self.market.register_strategy(strategy)
        self.paper = None
        if adapter is None and self._config['bybit']['mode'] == 'paper':
            self.paper = PaperExchange(self.store, clock=self.clock, public_transport=_PublicRESTTransport())
        self.adapter = adapter or BybitAdapter(self.store, self._config, transport=self.paper, clock=self.clock)
        if isinstance(getattr(self.adapter, 'transport', None), PaperExchange):
            self.paper = self.adapter.transport
        if execution is None:
            from .live_execution import ExecutionController
            execution = ExecutionController(self.store, self.adapter, self.market, self.memory, self.agents, clock=self.clock)
        self.execution = execution
        # Operator changes and exchange mutations share one linearization
        # boundary. Never hold it while waiting for a model response.
        self.execution._mutation_lock = runtime._execution_lock
        self.execution.permission_check = self.enabled
        self.support = None
        self._retired_support = []
        self._configure_trading_mode()
        self.execution.support_context_provider = self.support_context
        if scheduler is None:
            from .scheduling import Scheduler
            scheduler = Scheduler(self.store, clock=self.clock, config=self._config)
        self.scheduler = scheduler
        self.calendar_loader = calendar_loader
        self.research = ResearchWorker(self.store, runtime.provider, memory=self.memory,
                                       market=self.market, agents=self.agents, clock=self.clock)
        self.discovery = BybitDiscovery(self.store, self.market._get_adapter(), self.agents,
                                        self.memory, self.market, clock=self.clock)
        self._last_plan_fingerprint = self.store.get_document('scheduler_model_fingerprint')
        self._planner_requested = False
        self._last_scan = 0.
        self._last_initial_evaluation = 0.
        self._last_deadline = {}
        self.requires_restart = False
        self._last_host_signature = None
        self._planner_lock = threading.RLock()
        self._calendar_wake = threading.Event()

    def configuration_changed(self, config):
        if any(self._config['bybit'][key] != config['bybit'][key]
               for key in ('mode', 'account_id', 'account_type', 'position_mode')):
            self.requires_restart = True
            self._errors['configuration'] = {'id': 'configuration', 'kind': 'Configuration',
                'status': 'blocked', 'reason': 'Restart the paused runtime to apply the new account/environment binding.'}
            return
        self._config = copy.deepcopy(config)
        self.adapter.config = copy.deepcopy(config['bybit'])
        self.adapter.live_config = copy.deepcopy(config['live_execution'])
        self.scheduler.config.update(config['scheduler'])
        self.scheduler.config['calendar_required_coverage'] = config['operations']['calendar_required_coverage']
        self._configure_trading_mode()
        self.wake()

    def _configure_trading_mode(self):
        config = self.store.config()
        if self.support is not None:
            self.support.close(wait=False)
            self._retired_support.append(self.support)
            self.support = None
        if is_sol_mode(config):
            sid = self.runtime.ensure_sol_strategy()
            strategy = next(x for x in self.live.strategies() if x['id'] == sid)
            self.market.register_strategy(strategy)
            settings = mode_settings(config)
            self.support = SupportSwarm(self.store, self.agents, clock=self.clock,
                swarm_size=settings['swarm_size'], timeout_seconds=settings['support_timeout_seconds'],
                permission_check=lambda: self.enabled() and is_sol_mode(self.store.config()))

    def selected_strategies(self):
        selected = is_sol_mode(self.store.config())
        sid = self.store.get_document('sol_mode_strategy_id')
        return [x for x in self.live.strategies()
                if (x['id'] == sid if selected else x.get('trading_mode') != 'sol_perpetual')]

    def support_context(self, strategy_id, snapshot):
        if not self.support or not is_sol_mode(self.store.config()):
            return {'status': 'inactive', 'reports': []}
        result = self.support.context(strategy_id, snapshot,
            max_age_seconds=mode_settings(self.store.config())['support_max_age_seconds'])
        verifier = getattr(self.market, 'snapshot_status', None)
        if callable(verifier):
            result['reports'] = [report for report in result.get('reports', [])
                if verifier(report['source_snapshot_id']).get('valid') is True]
            if not result['reports']:
                result['status'] = 'unavailable'
        return result

    def support_step(self):
        event = self.events.claim(['support'])
        if not event:
            return None
        try:
            swarm = self.support
            if (not swarm or not is_sol_mode(self.store.config()) or
                    event['target'] != self.store.get_document('sol_mode_strategy_id')):
                self.events.finish(event['id'], {'reason': 'Trading mode changed'}, status='superseded')
                return None
            frozen = self.store.snapshot(event['payload'].get('snapshot_id'))
            source = frozen['value'] if frozen else {}
            reason = self._sol_minute_obsolete(event['target'], frozen)
            if reason:
                self.events.finish(event['id'], {'reason': reason}, status='superseded')
                return None
            snapshot = {**source, 'snapshot_id': frozen['id']}
            preflight = getattr(self.execution, 'support_preflight', None)
            admission = preflight(event['target']) if callable(preflight) else {'needed': True}
            if admission.get('needed') is False:
                report = {'status': 'skipped', 'reason': admission.get('reason'), 'reports': []}
            else:
                report = swarm.refresh(event['target'], snapshot,
                    max_age_seconds=mode_settings(self.store.config())['support_max_age_seconds'])
            # Support may outlive its minute, Pause, or a mode reconfiguration.
            # A late result is journalled, but must not create new attention.
            reason = self._sol_minute_obsolete(event['target'], frozen)
            if (self.enabled() and swarm is self.support and is_sol_mode(self.store.config()) and
                    event['target'] == self.store.get_document('sol_mode_strategy_id') and not reason):
                self.events.publish('candle_closed', event['target'], event['payload'],
                    key='supported-' + event['id'], priority=60)
            self.events.finish(event['id'], {'report_count': len(report.get('reports', [])),
                'reason': reason or report.get('reason')}, status='superseded' if reason else 'completed')
            return report
        except Exception as error:
            self.events.finish(event['id'], {'reason': type(error).__name__}, status='blocked')
            self._error('support', error)
            return {'status': 'blocked'}

    def _sol_minute_obsolete(self, strategy_id, frozen):
        """Check source identity and the current minute before buying model work."""
        source = frozen['value'] if frozen else {}
        deadline = source.get('attention_deadline_ms')
        if (type(deadline) not in (int, float) or not math.isfinite(deadline) or
                self.clock() * 1000 >= deadline):
            return 'Minute observation attention deadline expired or unavailable'
        verifier = getattr(self.market, 'snapshot_status', None)
        if callable(verifier) and verifier(frozen['id']).get('valid') is not True:
            return 'Minute observation was invalidated or expired'
        current = self.market.sample(strategy_id)
        if (not current.get('ready') or not source.get('candles') or not current.get('candles') or
                source['candles'][-1]['end_ms'] != current['candles'][-1]['end_ms'] or
                source.get('signal_input_id') != current.get('signal_input_id')):
            return 'Minute observation superseded or unavailable'
        return None

    def enabled(self):
        return not self.runtime.paused and not self.stop.is_set()

    def has_exposure(self):
        state = self.execution.state()
        if (any(abs(float(x.get('quantity', 0))) > 0 for x in state.get('positions', []))
                or any(x.get('status') in ACTIVE_ORDERS for x in state.get('orders', []))):
            return True
        # The UI projection may lag a crash after submission. The durable
        # adapter journal, including ambiguous outcomes, remains authoritative.
        reader = getattr(self.adapter, 'active_orders', None)
        unresolved = reader() if callable(reader) else self.adapter.orders(limit=5000)
        if any(x.get('status') in ACTIVE_ORDERS for x in unresolved):
            return True
        with self.store.connection() as db:
            row = db.execute('SELECT value FROM bybit_account_snapshots WHERE mode=? ORDER BY id DESC LIMIT 1',
                             (self.adapter.mode,)).fetchone()
        account = json.loads(row['value']) if row else {}
        return (any(abs(float(x.get('quantity', 0))) > 0 for x in account.get('positions', []))
                or any(x.get('currency') != 'USDT' and abs(float(x.get('wallet_balance', 0))) > 0
                       for x in account.get('currencies', [])))

    def monitoring_enabled(self):
        # An explicitly enabled account reconciles even on a paused restart;
        # otherwise an exchange fill during downtime could remain undiscovered.
        return not self.stop.is_set() and (self.enabled() or self._config['bybit']['enabled'] or self.has_exposure())

    def wake(self):
        self._wake.set()
        self._plan_wake.set()
        self._account_wake.set()
        self._calendar_wake.set()

    def _error(self, component, error):
        # Never persist provider/HTTP response bodies or exception text that may
        # contain keys, headers, account identifiers or external instructions.
        value = {'id': component, 'kind': component, 'status': 'blocked',
                 'reason': component + ' unavailable (' + type(error).__name__ + ')', 'at': timestamp(self.clock())}
        with self._error_lock:
            old = self._errors.get(component)
            self._errors[component] = value
        if not old or old['reason'] != value['reason']:
            self.store.event(component, value['reason'], 'warning')

    def _clear_error(self, component):
        with self._error_lock:
            self._errors.pop(component, None)

    def on_market(self, event):
        # WebSocket callbacks only enqueue. Evidence evaluation, models and
        # private REST requests never hold up a public socket reader.
        key = event.get('id') or event.get('key') or identity(event)
        self.events.publish('market_observation', event['strategy_id'], event,
                            key='observe-' + key, priority=event.get('priority', 50))
        self._wake.set()

    def observe_step(self):
        event = self.events.claim(['market_observation'])
        if not event:
            return None
        payload, sid = event['payload'], event['target']
        try:
            if sid not in {x['id'] for x in self.selected_strategies()}:
                self.events.finish(event['id'], {'reason': 'Inactive trading mode'}, status='superseded')
                return None
            snapshot = self.market.snapshot(sid)
            self.tape.record(snapshot)
            if payload['kind'] in {'candle_closed', 'signal', 'intrabar'}:
                self.live.observe(sid, snapshot)
            key = payload.get('id') or payload.get('key') or event['id']
            kind = 'support' if is_sol_mode(self.store.config()) and payload['kind'] == 'candle_closed' else payload['kind']
            attention = {**payload, 'snapshot_id': snapshot.get('snapshot_id')}
            if kind == 'support':
                # Observation evidence has already been written to the tape
                # and research store. Only stale SOL model attention can be
                # coalesced when later source minutes are queued.
                attention.update(sol_attention=True,
                    attention_deadline_ms=snapshot.get('attention_deadline_ms'),
                    captured_at_ms=snapshot.get('captured_at_ms'),
                    signal_input_id=snapshot.get('signal_input_id'))
            self.events.publish(kind, sid, attention,
                key='execute-' + key, priority=payload.get('priority', 50),
                delay=min(self.store.config()['operations']['event_coalesce_seconds'],
                          max(0., (snapshot.get('attention_deadline_ms', 0) / 1000 - self.clock()) / 4)))
            self.events.finish(event['id'], {'snapshot_id': snapshot.get('snapshot_id')})
            return snapshot
        except Exception as error:
            self.events.finish(event['id'], {'reason': type(error).__name__}, status='blocked')
            self._error('observation', error)
            return None

    def research_observation_step(self):
        """Measured quote accounting and tape capture; this never calls a model."""
        for strategy in self.selected_strategies():
            sample = self.market.sample(strategy['id'])
            self.tape.record(sample)
            with self.store.connection() as db:
                established = db.execute('SELECT 1 FROM live_research_v2_accounts WHERE experiment_id=?', (strategy['id'],)).fetchone()
            if not sample.get('ready') and not established:
                continue
            snapshot = self.market.snapshot(strategy['id'], transient=True)
            if snapshot.get('ready'):
                self.live.observe(strategy['id'], snapshot)
            else:
                self.live.observe_quote(strategy['id'], snapshot)

    def research_step(self):
        if self.clock() - self._last_initial_evaluation >= self._config['operations']['watchdog_seconds']:
            self._last_initial_evaluation = self.clock()
            self.research.evaluate_initial(strategy_ids={x['id'] for x in self.selected_strategies()})
        if is_sol_mode(self.store.config()):
            return None
        return self.research.step()

    def on_private(self, message, changed=True):
        if not changed:
            return
        self._account_wake.set()
        topic = str(message.get('topic', '')).split('.')[0]
        kind = {'execution': 'fill', 'order': 'order', 'position': 'position', 'wallet': 'risk'}.get(topic)
        if not kind:
            return
        self.scheduler.interrupt({'id': identity(['private', message]), 'kind': kind,
                                  'target': 'portfolio', 'event_at': self.clock()})
        rows = message.get('data', [])
        lookup = getattr(self.adapter, 'local_order', None)
        known = [] if callable(lookup) else self.adapter.orders(limit=5000)
        for row in rows:
            symbols = [x for x in self.live.strategies() if not row.get('symbol') or x['symbol'] == row.get('symbol')]
            link = row.get('orderLinkId')
            if link:
                local = lookup(link) if callable(lookup) else next(
                    (x for x in known if x['order_link_id'] == link), None)
                owner = local.get('strategy_id') if local else None
                if owner:
                    symbols = [x for x in symbols if x['id'] == owner]
            for strategy in symbols:
                self.events.publish(kind, strategy['id'], {'kind': kind, 'strategy_id': strategy['id'],
                    'exchange_event': row}, key=identity([kind, strategy['id'], row]), priority=90)
        self._plan_wake.set()
        self._wake.set()

    def execution_step(self):
        event = self.events.claim(['candle_closed', 'intrabar', 'signal', 'fill', 'order', 'position',
                                  'risk', 'scheduled_event', 'decision_deadline', 'data_health'])
        if not event:
            return None
        try:
            if event['payload'].get('sol_attention') is True and (
                    not is_sol_mode(self.store.config()) or
                    event['target'] != self.store.get_document('sol_mode_strategy_id')):
                self.events.finish(event['id'], {'reason': 'SOL trading mode or strategy changed'},
                                   status='superseded')
                return {'status': 'superseded'}
            if is_sol_mode(self.store.config()) and event['kind'] in {'candle_closed', 'signal', 'intrabar'}:
                frozen = self.store.snapshot(event['payload'].get('snapshot_id'))
                reason = self._sol_minute_obsolete(event['target'], frozen)
                if reason:
                    self.events.finish(event['id'], {'reason': reason}, status='superseded')
                    return {'status': 'superseded'}
            result = self.execution.handle_event({**event['payload'], 'id': event['id'],
                'kind': event['kind'], 'strategy_id': event['target']})
            self.events.finish(event['id'], result)
            return result
        except Exception as error:
            # Orders/model work are journalled by the execution lane. A new
            # meaningful observation may retry; uncertain work is never replayed.
            self.events.finish(event['id'], {'reason': type(error).__name__}, status='blocked')
            self._error('execution', error)
            return {'status': 'blocked'}

    def discovery_step(self):
        if is_sol_mode(self.store.config()):
            return None
        event = self.events.claim(['discovery'])
        if not event:
            return None
        try:
            instrument = event['payload'].get('instrument')
            if instrument:
                result = self.discovery.run(instrument, task_id=event['id'])
            else:
                completed = self.runtime.run_cycle()
                if not completed and self.enabled():
                    self.events.finish(event['id'], {'reason': self.runtime.status}, status='pending',
                                       retry_at=max(self.clock() + 5, self.runtime.next_cycle_at))
                    return None
                result = {'completed': completed, 'cycle': self.store.get_document('active_cycle', {}).get('id')}
            self.events.finish(event['id'], result)
            self._plan_wake.set()
            return result
        except BudgetExceeded as error:
            retry = self.clock() + 5 if error.temporary else (self.clock() // 86400 + 1) * 86400
            self.events.finish(event['id'], {'reason': str(error)}, status='pending', retry_at=retry)
            return {'status': 'deferred'}
        except Exception as error:
            self.events.finish(event['id'], {'reason': type(error).__name__}, status='blocked')
            self._error('discovery', error)
            return {'status': 'blocked'}

    def protection_step(self):
        if self.paper:
            snapshots = []
            for strategy in self.live.strategies():
                try:
                    snapshots.append(self.market.sample(strategy['id']))
                except ValueError:
                    continue
            for message in self.paper.advance(snapshots):
                changed = self.adapter._handle_private_message(message)
                self.on_private(message, changed)
        return self.execution.protect()

    def _loop(self, component, step, *, predicate=None, interval=None, wake=None):
        wake = wake or self._wake
        while not self.stop.is_set():
            if (predicate or self.enabled)():
                try:
                    value = step()
                    self._clear_error(component)
                    if value is not None and interval is None:
                        continue
                except Exception as error:
                    self._error(component, error)
            delay = interval() if callable(interval) else interval
            wake.wait(delay if delay is not None else .5)
            wake.clear()

    def _private_loop(self):
        while not self.stop.is_set():
            if self.adapter.mode != 'paper' and self._config['bybit']['enabled'] and self.monitoring_enabled():
                try:
                    self.adapter.run_private_stream(on_event=self.on_private, stop_event=self.stop)
                except Exception as error:
                    self._error('private_stream', error)
            self.stop.wait(5)

    def run(self):
        workers = [
            ('market', lambda: self.market.run(self.stop, self.monitoring_enabled)),
            ('observation', lambda: self._loop('observation', self.observe_step, predicate=self.monitoring_enabled)),
            ('research_observation', lambda: self._loop('research_observation', self.research_observation_step,
                predicate=self.monitoring_enabled, interval=1)),
            ('execution', lambda: self._loop('execution', self.execution_step)),
            ('support', lambda: self._loop('support', self.support_step)),
            ('discovery', lambda: self._loop('discovery', self.discovery_step)),
            ('review', lambda: self._loop('review', self.research_step)),
            ('protection', lambda: self._loop('protection', self.protection_step,
                predicate=self.monitoring_enabled, interval=lambda: self.store.config()['operations']['watchdog_seconds'])),
            ('reconciliation', lambda: self._loop('reconciliation', self.execution.reconcile,
                predicate=lambda: self.monitoring_enabled() and self._config['bybit']['enabled'],
                interval=lambda: self.store.config()['operations']['reconcile_seconds'], wake=self._account_wake)),
            ('private_stream', self._private_loop),
            ('planner', self._planner_loop),
            ('calendar', self._calendar_loop),
            ('equity_market', self.runtime._market_loop),
        ]
        self._threads = [threading.Thread(target=fn, name='prism-' + name, daemon=True) for name, fn in workers]
        for thread in self._threads:
            thread.start()
        try:
            while not self.stop.is_set():
                if self.enabled():
                    try:
                        self.coordinator_step()
                        self._clear_error('coordinator')
                    except Exception as error:
                        self._error('coordinator', error)
                self._wake.wait(.5)
                self._wake.clear()
        finally:
            self.market.stop()
            self.wake()
            for thread in self._threads:
                # Retain the state-directory process lock until all bounded
                # in-flight I/O has settled; a successor must not replay a call
                # while an old worker can still commit its result.
                thread.join()
            for swarm in [self.support, *self._retired_support]:
                if swarm is not None:
                    swarm.close(wait=True)

    # Scheduler-specific methods below execute only validated durable plans;
    # the planner's model call runs exclusively in its own worker.
    def coordinator_step(self):
        if is_sol_mode(self.store.config()):
            self.runtime.next_cycle_at = (int(self.clock()) // 60 + 1) * 60
            return {'mode': 'sol_perpetual', 'next_boundary_at': self.runtime.next_cycle_at}
        if self.clock() - self._last_scan >= self.store.config()['operations']['watchdog_seconds']:
            self._last_scan = self.clock()
            self.research.scan()
        context = self.scheduling_context()
        self.scheduler_watch(context)
        for task in self.scheduler.due_tasks():
            self.dispatch_task(task)
        for strategy in self.live.strategies():
            if 'decision_deadline' not in strategy['market_policy']['attention_events']:
                continue
            snapshot = self.market.sample(strategy['id'])
            deadline = snapshot.get('attention_deadline_ms', 0) / 1000
            halfway = deadline - strategy['market_policy']['max_decision_latency_seconds'] / 2
            signal_id = snapshot.get('signal_input_id')
            if (snapshot.get('ready') and halfway <= self.clock() < deadline
                    and self._last_deadline.get(strategy['id']) != signal_id):
                snapshot = self.market.snapshot(strategy['id'])
                self.events.publish('decision_deadline', strategy['id'], {'kind': 'decision_deadline',
                    'strategy_id': strategy['id'], 'snapshot_id': snapshot['snapshot_id'], 'expires_at': deadline},
                    key=identity(['deadline', strategy['id'], snapshot.get('signal_input_id')]), priority=80)
                self._last_deadline[strategy['id']] = snapshot.get('signal_input_id')
        return context

    def scheduling_context(self):
        execution = self.execution.state()
        memory, health = self.memory.state(), self.market.state()
        backlog = [{k: row.get(k) for k in ('id', 'experiment_id', 'strategy_id', 'trigger', 'priority', 'status')}
                   for row in memory.get('queued', [])]
        resources, events = self.agents.budget.state(), self.events.state()
        remaining = sum(max(0, r['daily_calls'] - r['calls']) for r in resources.values())
        usd = sum(max(0., r['daily_usd'] - r['spent'] - r['reserved']) for r in resources.values())
        workload = {'capacity_remaining': max(0, self.store.config()['operations']['max_pending_events'] -
            sum(events['counts'].get(k, 0) for k in ('pending', 'working'))), 'counts': events['counts']}
        freshness = {'stale': any(x['health'].get('stale') or x['health'].get('quote_stale')
                                 for x in health.get('subscriptions', [])), 'subscriptions': health.get('subscriptions', []),
                     'connections': health.get('connections', {})}
        budget = {'calls_remaining': remaining, 'usd_remaining': usd, 'components': resources}
        # Persist the exact host inputs used by due_tasks' final resource check.
        for key, value in {'positions': execution.get('positions', []), 'orders': execution.get('orders', []),
                'review_backlog': backlog, 'data_freshness': freshness,
                'scheduler_budget': budget, 'scheduler_workload': workload}.items():
            self.store.set_document(key, value)
        context = self.scheduler.context(positions=execution.get('positions', []), orders=execution.get('orders', []),
            review_backlog=backlog, freshness=freshness, budget=budget, workload=workload)
        strategies = self.store.strategies()[:256]
        context.update(strategies=strategies, recent_reviews=memory.get('recent_reviews', [])[:16],
            recent_revisions=memory.get('recent_revisions', [])[:16], risk=execution.get('risk', {}),
            last_discovery=self.store.get_document('last_cycle_at'),
            continuation=self.store.get_document('scheduler_continuation'))
        limits = self.store.config()['live_execution']
        context['execution_envelope'] = {'mode': self.adapter.mode, 'enabled': limits['enabled'],
            'kill_switch': limits['kill_switch'], 'operator_versions': limits['eligible_strategy_versions'],
            'allocations': limits['allocations'], 'requires_reconciliation': execution.get('risk', {}).get('reconciliation_required', True)}
        from .market_policy import INTERVAL_MS
        def next_boundary(frame):
            width = INTERVAL_MS[frame]
            offset = 3 * 86400000 if frame == 'W' else 0  # Unix epoch Thursday; Bybit weeks begin Monday.
            return ((int(self.clock() * 1000) + offset) // width + 1) * width / 1000 - offset / 1000
        context['strategy_boundaries'] = [{'strategy_id': x['id'], 'timeframe': x['market_policy']['primary_timeframe'],
            'next_boundary_at': next_boundary(x['market_policy']['primary_timeframe']),
            'latency_seconds': x['market_policy']['max_decision_latency_seconds']} for x in strategies if x.get('policy_version') == 2]
        context['allowed_targets'] = sorted(set(context['allowed_targets']) | {x['id'] for x in strategies} |
            {x['category'] + ':' + x['symbol'] for x in self.store.config()['bybit']['allowed_instruments']})
        context['evidence_refs'] += ['context:strategies', 'context:strategy_boundaries', 'context:recent_reviews', 'context:last_discovery', 'context:execution_envelope']
        return context

    def _plan_signature(self, context):
        # Raw ticks, candle counters, wall time and self-generated call costs
        # cannot cause an unbounded loop of scheduler requests.
        return identity({'events': [(x['event_id'], x['revision'], x['status'], x.get('stale')) for x in context['events']],
            'coverage': [(k, v['status']) for k, v in context['coverage']['categories'].items()],
            'strategies': [(x['id'], x.get('status'), x.get('policy_version')) for x in context['strategies']],
            'positions': [{k: r.get(k) for k in ('strategy_id', 'category', 'symbol', 'quantity', 'protection_status')} for r in context['positions']],
            'orders': [{k: r.get(k) for k in ('order_link_id', 'status', 'quantity', 'filled_quantity')} for r in context['orders'] if r.get('status') in ACTIVE_ORDERS],
            'review_ids': [x['id'] for x in context['review_backlog']],
            'conclusions': [(x['id'], x.get('status')) for x in context['recent_reviews'][:8]],
            'last_discovery': context['last_discovery'],
            'execution_envelope': context['execution_envelope'],
            'health': [(x['category'], x['symbol'], x['interval'], x.get('connected'), x.get('ready')) for x in context['freshness']['subscriptions']],
            'budget_day': timestamp(self.clock())[:10],
            'budget_exhausted': {k: r['calls'] >= r['daily_calls'] or r['spent'] + r['reserved'] >= r['daily_usd']
                                 for k, r in context['budget']['components'].items()},
            'workload_full': context['workload']['capacity_remaining'] == 0})

    def scheduler_watch(self, context):
        signature = self._plan_signature(context)
        if signature != self._last_host_signature:
            self.scheduler.decide(context=context)  # Immediate deterministic event preparation.
            self._last_host_signature = signature
        last = self.store.get_document('scheduler_agent_decision', {})
        wake = last.get('next_wake_at')
        retry_at = last.get('retry_at')
        due = (wake is not None and self.clock() >= wake and last.get('status') == 'accepted'
               or retry_at is not None and self.clock() >= retry_at and last.get('status') in {'blocked', 'fallback'})
        with self._planner_lock:
            if not self._planner_requested and (signature != self._last_plan_fingerprint or due):
                self._planner_requested = True
                self._planner_context = context
                self._planner_signature = signature
                self._planner_generation = self.scheduler.state()['generation']
                self._planner_task_id = identity(['scheduler', signature, last.get('id'), (retry_at or wake) if due else None])
                self._plan_wake.set()
        pending = [x['trigger_at'] for x in self.scheduler.state()['tasks'] if x['status'] == 'pending']
        self.runtime.next_cycle_at = min(pending + ([wake] if wake and wake > self.clock() else []), default=0.)

    def dispatch_task(self, task):
        if task.get('status') == 'blocked':
            return
        target, kind = task['target'], task['kind']
        result = {'reason': task['rationale'], 'queued': []}
        try:
            if target == 'calendar-coverage':
                self._calendar_wake.set()
                result['reason'] = 'Calendar refresh requested; absent coverage remains explicitly blocked.'
            elif target == 'data-freshness':
                # The independent market supervisor already owns bounded gap
                # recovery. A schedule never performs blocking REST here.
                result['reason'] = 'Continuous market recovery is active; stale inputs cannot authorize exposure.'
            elif target == 'review-backlog':
                self._wake.set()
                result['reason'] = 'Independent review worker notified.'
            elif kind == 'discovery':
                instruments = [x for x in self.store.config()['bybit']['allowed_instruments']
                               if target in {x['symbol'], x['category'] + ':' + x['symbol']}]
                for instrument in instruments or [None]:
                    key = identity(['scheduled-discovery', task['id'], instrument])
                    result['queued'].append(self.events.publish('discovery', target,
                        {'instrument': instrument, 'schedule_task': task}, key=key, priority=task['priority']))
            else:
                strategies = [x for x in self.store.strategies() if target in {x['id'], x['symbol'], 'portfolio', 'market', 'research'}
                              or target in {'XNYS', 'XNAS', 'NYSE', 'NASDAQ', 'US'} and x.get('policy_version', 1) == 1]
                if kind == 'review':
                    for strategy in strategies[:64]:
                        result['queued'].append(self.memory.enqueue_review(strategy['id'], trigger='scheduler',
                            material={'schedule_task_id': task['id'], 'rationale': task['rationale'],
                                      'evidence_refs': task['evidence_refs'], 'phase': task.get('phase')}, priority=task['priority']))
                    if not strategies:
                        instruments = [x for x in self.store.config()['bybit']['allowed_instruments']
                                       if target in {x['symbol'], x['category'] + ':' + x['symbol']}]
                        for instrument in instruments or [None]:
                            result['queued'].append(self.events.publish('discovery', target,
                                {'schedule_task': task, 'instrument': instrument},
                                key=identity(['scheduled-discovery', task['id'], instrument]), priority=task['priority']))
                # Calendar preparation/reviews also alert already deployed
                # strategies. Eligible versions and actual snapshot freshness
                # are checked independently by the execution host.
                for strategy in strategies:
                    if strategy.get('policy_version') == 2 and 'scheduled_event' in strategy['market_policy']['attention_events']:
                        event_id = task.get('event_id')
                        calendar_event = next((row for row in self.scheduler.context()['events']
                                               if row.get('event_id') == event_id), None) if event_id else None
                        result['queued'].append(self.events.publish('scheduled_event', strategy['id'],
                            {'schedule_task': task, 'calendar_event': calendar_event, 'rationale': task['rationale']},
                            key=identity(['scheduled-execution', task['id'], strategy['id']]), priority=task['priority']))
                if kind == 'execution':
                    self._account_wake.set()
            self.scheduler.complete_task(task['id'], result)
            return result
        except Exception as error:
            self.scheduler.complete_task(task['id'], {'reason': type(error).__name__}, status='blocked')
            self._error('scheduled_task', error)

    def planner_step(self):
        from .scheduling import scheduler_output_schema
        if not self.enabled() or is_sol_mode(self.store.config()) or not self._planner_requested:
            return None
        with self._planner_lock:
            context = copy.deepcopy(self._planner_context)
            signature, generation, task_id = self._planner_signature, self._planner_generation, self._planner_task_id
        record = {'id': task_id, 'input_fingerprint': signature, 'at': self.clock(),
                  'expiry': self.clock() + self.scheduler.config['max_delay_seconds'],
                  'fallback': 'Source-derived event tasks remain active; protection is independent.'}
        previous = self.store.get_document('scheduler_agent_decision', {})
        failures = previous.get('failure_count', 0) if previous.get('input_fingerprint') == signature else 0
        try:
            output = self.agents.structured('scheduler', context, scheduler_output_schema(), task_id=task_id)
            if not self.enabled() or generation != self.scheduler.state()['generation']:
                record.update(status='superseded', rationale='New event or pause preempted this planning request.')
            else:
                current = self.scheduling_context()
                normalized = copy.deepcopy(output)
                for task in normalized['tasks']:
                    if context['now'] <= task['trigger_at'] < self.clock() < task['expires_at']:
                        task['trigger_at'] = self.clock()
                normalized['next_wake_at'] = max(normalized['next_wake_at'], self.clock() + self.scheduler.config['min_delay_seconds'])
                decision = self.scheduler.decide(normalized, context=current)
                record.update(status='accepted' if decision['accepted'] else 'fallback', raw_output=output,
                    tasks=normalized['tasks'], next_wake_at=normalized['next_wake_at'],
                    rationale='; '.join(x['rationale'] for x in normalized['tasks']) or 'No new model work; watch the persisted conditions.',
                    evidence_refs=sorted({ref for x in normalized['tasks'] for ref in x['evidence_refs']}),
                    trigger_conditions=sorted({trigger for x in normalized['tasks'] for trigger in x['triggers']}),
                    validation=decision)
                if not decision['accepted']:
                    record['failure_count'] = failures + 1
                    record['retry_at'] = self.clock() + min(self.scheduler.config['max_delay_seconds'],
                        self.scheduler.config['watchdog_seconds'] * 2 ** min(failures, 10))
                self._last_plan_fingerprint = signature
                self.store.set_document('scheduler_model_fingerprint', signature)
        except Exception as error:
            record.update(status='blocked', rationale='Planning unavailable (' + type(error).__name__ + '); deterministic event fallback remains active.')
            retry = self.clock() + min(self.scheduler.config['max_delay_seconds'],
                self.scheduler.config['watchdog_seconds'] * 2 ** min(failures, 10))
            if isinstance(error, BudgetExceeded) and not error.temporary:
                retry = (self.clock() // 86400 + 1) * 86400
            record.update(retry_at=retry, failure_count=failures + 1)
            self._last_plan_fingerprint = signature
            self.store.set_document('scheduler_model_fingerprint', signature)
            self._error('scheduler', error)
        self.store.set_document('scheduler_decision_' + task_id, record)
        self.store.set_document('scheduler_agent_decision', record)
        self.store.set_document('scheduler_continuation', {k: record.get(k) for k in
            ('id', 'status', 'rationale', 'tasks', 'trigger_conditions', 'evidence_refs', 'next_wake_at', 'fallback')})
        with self._planner_lock:
            self._planner_requested = False
        self._wake.set()
        return record

    def _planner_loop(self):
        while not self.stop.is_set():
            self.planner_step()
            self._plan_wake.wait(.5)
            self._plan_wake.clear()

    def _calendar_loop(self):
        while not self.stop.is_set():
            if self.enabled() and not is_sol_mode(self.store.config()):
                try:
                    self.refresh_calendar()
                except Exception as error:
                    self._error('calendar', error)
            self._calendar_wake.wait(self.store.config()['operations']['calendar_refresh_seconds'])
            self._calendar_wake.clear()

    def refresh_calendar(self):
        from .scheduling import CalendarFeedProvider
        sources = [{'id': identity(source), **({'type': 'https_json', 'url': source} if source.startswith('https://')
                    else {'type': 'file', 'path': source})} for source in self.store.config()['operations']['calendar_sources']]
        provider = self.calendar_loader or CalendarFeedProvider(clock=self.clock)
        result = provider.load(sources)
        self.scheduler.upsert_events(result['events'])
        self.scheduler.upsert_coverage(result['coverage'])
        self.store.set_document('calendar_feed_health', {'at': timestamp(self.clock()), 'errors': result['errors'], 'source_count': len(sources)})
        if result['errors']:
            self._errors['calendar'] = {'id': 'calendar', 'kind': 'Calendar', 'reason': 'One or more configured calendar feeds failed; coverage is not assumed.'}
        else:
            self._clear_error('calendar')
        self._wake.set()
        self._plan_wake.set()
        return result

    def import_calendar(self, body):
        from .scheduling import CalendarFeedProvider
        if not isinstance(body, dict) or set(body) - {'id', 'events', 'sessions', 'coverage', 'source_url', 'provenance'}:
            raise ValueError('Calendar import requires sourced events/sessions and explicit coverage')
        result = CalendarFeedProvider(clock=self.clock).load([{'id': 'operator-calendar', **body, 'type': 'operator'}])
        if result['errors']:
            raise ValueError('Invalid calendar source document')
        self.scheduler.upsert_events(result['events'])
        self.scheduler.upsert_coverage(result['coverage'])
        self.wake()
        return result

    def state(self):
        sol = is_sol_mode(self.store.config())
        if sol:
            # The Research planner and sourced calendar are suspended in SOL
            # mode. Their old wake times and coverage failures do not describe
            # this event-driven one-minute trader.
            schedule = {'tasks': [], 'events': [], 'coverage': {'missing': []},
                'next_wake_at': (int(self.clock()) // 60 + 1) * 60,
                'rationale': 'Watching the next live closed one-minute SOLUSDT observation; host eligibility and order controls apply.'}
        else:
            schedule = self.scheduler.state()
            agent_plan = self.store.get_document('scheduler_agent_decision', {})
            schedule['rationale'] = agent_plan.get('rationale', 'Source-derived fallback is watching events and required observations.')
            pending_wakes = [x['trigger_at'] for x in schedule['tasks'] if x['status'] == 'pending']
            if agent_plan.get('status') == 'accepted' and agent_plan.get('next_wake_at', 0) > self.clock():
                pending_wakes.append(agent_plan['next_wake_at'])
            elif agent_plan.get('retry_at', 0) > self.clock():
                pending_wakes.append(agent_plan['retry_at'])
            schedule['next_wake_at'] = min(pending_wakes, default=None)
        memory = self.memory.state()
        sid = self.store.get_document('sol_mode_strategy_id') if sol else None
        if sol:
            memory = {**memory, **{key: [row for row in memory.get(key, []) if any(
                row.get(field) == sid for field in ('strategy_id', 'experiment_id', 'target_id',
                    'parent_experiment_id', 'child_experiment_id'))]
                for key in ('queued', 'recent_reviews', 'recent_revisions')}}
        market = self.market.state()
        execution = self.execution.state()
        with self._error_lock:
            blocked = [value for key, value in self._errors.items()
                       if not sol or key not in {'planner', 'discovery', 'calendar'}]
        blocked += [{'id': x['id'], 'kind': x['kind'], 'status': x['status'],
                     'reason': (x.get('result') or {}).get('reason', 'Deferred by bounded work capacity')}
                    for x in self.events.state()['events'] if x['status'] in {'blocked', 'failed', 'deferred'}
                    and (not sol or x.get('target') == sid)]
        blocked += [{'id': 'coverage-' + row['category'], 'kind': 'Calendar coverage', 'reason': str(row)}
                    for row in schedule.get('coverage', {}).get('missing', [])]
        blocked += [{'id': row['id'], 'kind': 'Strategy review', 'status': row['status'],
                     'reason': row['deferred_reason']} for row in memory.get('queued', []) if row.get('deferred_reason')]
        attempts = []
        if not sol:
            with self.store.connection() as db:
                attempts = db.execute("SELECT key,value FROM cascade_meta WHERE key LIKE 'development_attempt_%' "
                                      "OR key LIKE 'revision_transition_%' ORDER BY key LIMIT 128").fetchall()
        for row in attempts:
            value = json.loads(row['value'])
            if value.get('status') in {'blocked', 'deferred'}:
                blocked.append({'id': row['key'], 'kind': 'Development / revision', **value})
        return projection(mode=self.adapter.mode, enabled=self.enabled(), schedule=schedule,
            calendar=schedule.get('events', []), subscriptions=market.get('subscriptions', []),
            memory=memory, execution=execution, resources=self.agents.budget.state(), blocked=blocked)
