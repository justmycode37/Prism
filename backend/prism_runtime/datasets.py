"""Offline, immutable dataset import and reproducible development evaluation.

An imported vendor declaration is retained as provenance, never upgraded into a
claim that Prism independently verified historical universe or availability data.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import time
from pathlib import Path

from . import analytics, temporal, simulation
from . import search as strategy_search
from .search import DEFAULT_MAX_CANDIDATES, SEARCH_PROTOCOL, candidate_space_size, generate_candidates


def import_dataset(store, document):
    if not isinstance(document, dict) or not isinstance(document.get('id'), str) or not document['id'].strip():
        raise ValueError('Dataset requires a nonempty id.')
    if not isinstance(document.get('source'), str) or not document['source'].strip():
        raise ValueError('Dataset requires named source provenance.')
    cutoff = analytics.timestamp_ms(document.get('as_of'))
    if cutoff > round(time.time() * 1000):
        raise ValueError('Dataset availability cannot be future dated.')
    markets = document.get('markets')
    evidence = document.get('evidence', [])
    universe = document.get('universe', [])
    if not isinstance(markets, list) or not markets or len(markets) > 256:
        raise ValueError('Dataset needs one to 256 market histories.')
    if not isinstance(evidence, list) or not isinstance(universe, list):
        raise ValueError('Evidence and dated universe membership must be lists.')
    symbols = set()
    evidence_ids = {row.get('id') for row in evidence if isinstance(row, dict)}
    for market in markets:
        symbol = market.get('symbol') if isinstance(market, dict) else None
        if not isinstance(symbol, str) or not symbol or symbol in symbols:
            raise ValueError('Market symbols must be unique.')
        symbols.add(symbol)
        analytics.closed_candles(market, now_ms=cutoff)
        for feature in market.get('event_features', []):
            if not isinstance(feature, dict) or not feature.get('evidence_ids') or not set(feature['evidence_ids']) <= evidence_ids:
                raise ValueError('Event features require source evidence lineage.')
            if analytics.timestamp_ms(feature.get('available_at_ms')) > cutoff:
                raise ValueError('Event feature was unavailable at the dataset cutoff.')
    for membership in universe:
        if not isinstance(membership, dict) or not membership.get('security_id') or not membership.get('symbol'):
            raise ValueError('Universe membership requires security_id and symbol.')
        start = analytics.timestamp_ms(membership.get('valid_from'))
        known = analytics.timestamp_ms(membership.get('available_at'))
        end = analytics.timestamp_ms(membership['valid_to']) if membership.get('valid_to') else None
        if known > cutoff or (end is not None and end <= start):
            raise ValueError('Universe membership has invalid availability or dates.')
    body = deepcopy(document)
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    key = 'dataset_' + document['id']
    existing = store.get_document(key)
    if existing:
        if existing['fingerprint'] != digest:
            raise ValueError('Dataset id is immutable; import a new version id for revised data.')
        return existing
    snapshot = store.save_snapshot('dataset', body, cutoff)
    record = {'id': document['id'], 'snapshot_id': snapshot, 'fingerprint': digest, 'as_of_ms': cutoff,
              'source': document['source'], 'symbols': sorted(symbols),
              'coverage': temporal.coverage_report(evidence, markets),
              'universe_provenance': 'provider_declared' if universe else 'current_symbols_only',
              'independently_verified': False}
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        prior = db.execute('SELECT value FROM cascade_meta WHERE key=?', (key,)).fetchone()
        if prior:
            saved = json.loads(prior[0])
            if saved['fingerprint'] != digest:
                raise ValueError('Dataset version cannot be overwritten concurrently')
            return saved
        db.execute('INSERT INTO cascade_meta(key,value) VALUES (?,?)', (key, json.dumps(record, allow_nan=False)))
    return record


def evaluate_dataset(store, document, proposals, config=None):
    """All outputs are development experiments; forward promotion stays separate."""
    record = import_dataset(store, document)
    if not isinstance(proposals, list) or not proposals:
        raise ValueError('Supply explicit frozen proposals.')
    frozen_document = store.snapshot(record['snapshot_id'])['value']
    frozen_proposals = deepcopy(proposals)
    config = deepcopy(config if config is not None else store.config())
    policy = simulation.execution_policy(config)
    policy_digest = simulation.policy_hash(config)
    config_digest = hashlib.sha256(json.dumps(config, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    code_digest = _search_implementation_hash()
    reports = []
    for proposal in frozen_proposals:
        source_market = next((market for market in frozen_document['markets'] if market['symbol'] == proposal.get('symbol')), {})
        if source_market.get('security_id'):
            proposal = {**proposal, 'security_id': source_market['security_id']}
        spec = analytics.canonical_spec(proposal)
        refs = proposal.get('evidence_ids', [])
        if not refs or not set(refs) <= {row.get('id') for row in frozen_document.get('evidence', [])}:
            raise ValueError('Proposal requires evidence present in the dataset')
        experiment = store.register_experiment(spec, policy=policy, context={
            'dataset_id': record['id'], 'input_snapshot': record['snapshot_id'], 'data_fingerprint': record['fingerprint'],
            'information_cutoff_ms': record['as_of_ms'], 'historical_phase': 'development', 'config': config,
            'hypothesis': proposal.get('hypothesis'), 'evidence_ids': refs})
        markets = [temporal.market_as_of(row, record['as_of_ms']) for row in frozen_document['markets']]
        cache_key = _candidate_cache_key(record['fingerprint'], spec, policy_digest, config_digest, code_digest)
        saved_evaluation = next((row for row in (store.experiment(experiment['id']) or {}).get('evaluations', [])
                                 if row.get('candidate_cache_key') == cache_key), None)
        cache_hit = saved_evaluation is not None
        result = deepcopy(saved_evaluation['report']) if cache_hit else analytics.validate_edges(
            [proposal], markets, config=config, alpha_budget=experiment['alpha_budget'])[0]
        evaluation = {'phase': 'development', 'dataset_id': record['id'], 'data_fingerprint': record['fingerprint'],
                      'policy_hash': policy_digest, 'candidate_cache_key': cache_key,
                      'cache_hit': cache_hit, 'report': result}
        store.record_evaluation(experiment['id'], evaluation)
        market = next((market for market in markets if market['symbol'] == spec['symbol']), {})
        existing = next((row for row in store.strategies() if row.get('experiment_id') == experiment['id']), None)
        if existing is None and len(analytics.closed_candles(market)) >= analytics.MIN_CANDLES:
            from .domain import timestamp
            store.save_strategy({'id': 'strategy_'+experiment['id'], 'experiment_id': experiment['id'],
                'name': str(proposal.get('hypothesis', 'Frozen dataset candidate'))[:120],
                'symbol': spec['symbol'], 'family': spec['kind'], 'status': 'queued', 'phase': 'prospective_shadow',
                'spec': spec, 'policy': experiment['policy'], 'policy_hash': simulation.policy_hash(config),
                'registered_at_ms': experiment['registered_at_ms'], 'created_at': timestamp(experiment['registered_at_ms']/1000),
                'edge_ids': [result['id']], 'evidence_ids': refs,
                'validation': {'passed': True, 'spec_hash': analytics.spec_hash(spec), 'policy_hash': simulation.policy_hash(config), 'development_only': True},
                'reason': 'Frozen for future shadow observations; historical results remain development evidence.',
                **{key: result.get('metrics', {}).get(key) for key in ('net_return', 'sharpe', 'max_drawdown', 'trades')}})
        reports.append({'experiment_id': experiment['id'], **result})
    try:
        unique = {analytics.spec_hash(proposal): analytics.canonical_spec(proposal) for proposal in frozen_proposals}
        portfolio = simulation.replay_portfolio(list(unique.values()),
            [temporal.market_as_of(row, record['as_of_ms']) for row in document['markets']], config)
    except ValueError as error:
        portfolio = {'status': 'inconclusive', 'reason': str(error)}
    return {'dataset': record, 'phase': 'development', 'results': reports, 'portfolio_replay': portfolio}


def search_dataset(store, document, *, max_candidates=DEFAULT_MAX_CANDIDATES, config=None,
                   families=None, lookbacks=None):
    """Pre-register and score a deterministic offline candidate grid.

    Every unique candidate is registered before the first call to the analytics
    screen. The registry's summable alpha allocation is supplied to that screen;
    historical returns remain development evidence and never promote a rule.
    Exact runs are cached as a whole, while per-candidate reports are reused for
    a broader later search over the same immutable dataset and protocol.
    """
    if isinstance(max_candidates, bool) or not isinstance(max_candidates, int) or not 1 <= max_candidates <= 5000:
        raise ValueError('max_candidates must be an integer from 1 to 5000')
    if families is not None:
        families = tuple(families)
    if lookbacks is not None:
        lookbacks = tuple(lookbacks)
    record = import_dataset(store, document)
    frozen_document = store.snapshot(record['snapshot_id'])['value']
    config = deepcopy(config if config is not None else store.config())
    policy = simulation.execution_policy(config)
    policy_hash = simulation.policy_hash(config)
    config_hash = hashlib.sha256(json.dumps(config, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    benchmark_symbols = config.get('research', {}).get('benchmark_symbols', ['SPY', 'SMH'])
    grid_size = candidate_space_size(frozen_document['markets'], families=families, lookbacks=lookbacks,
                                     exclude_symbols=benchmark_symbols)
    implementation_hash = _search_implementation_hash()
    search_settings = {'max_candidates': max_candidates, 'families': list(families) if families is not None else None,
                       'lookbacks': list(lookbacks) if lookbacks is not None else None}
    run_payload = [SEARCH_PROTOCOL, record['fingerprint'], policy_hash, config_hash, implementation_hash, search_settings]
    search_id = hashlib.sha256(json.dumps(run_payload, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    run_key = 'research_search_' + search_id
    evidence = temporal.evidence_as_of(frozen_document.get('evidence', []), record['as_of_ms'])
    evidence_ids = sorted({row['id'] for row in evidence if isinstance(row.get('id'), str) and row['id']})[:16]
    if not evidence_ids:
        raise ValueError('Search requires evidence with a valid availability time at the dataset cutoff.')

    proposals = generate_candidates(frozen_document['markets'], evidence_ids=evidence_ids,
                                   max_candidates=max_candidates, families=families, lookbacks=lookbacks,
                                   exclude_symbols=benchmark_symbols)
    cached_run = _claim_or_read_search(store, run_key, search_id)
    if cached_run is not None:
        if cached_run.get('status') == 'complete':
            cached_run = deepcopy(cached_run['report'])
            cached_run['cache_hit'] = True
            return cached_run
        return {'search_id': search_id, 'phase': 'development', 'status': 'in_progress',
                'development_only': True, 'cache_hit': True,
                'reason': 'An identical frozen search is already being evaluated.'}

    markets = [temporal.market_as_of(row, record['as_of_ms']) for row in frozen_document['markets']]
    proposal_context = []
    # Freeze every rule and its alpha allocation before running even the first backtest.
    for index, proposal in enumerate(proposals):
        market = next((row for row in frozen_document['markets'] if row['symbol'].upper() == proposal['symbol']), {})
        if market.get('security_id') and not proposal.get('security_id'):
            proposal['security_id'] = market['security_id']
        spec = analytics.canonical_spec(proposal)
        candidate_key = _candidate_cache_key(record['fingerprint'], spec, policy_hash, config_hash, implementation_hash)
        context = {'dataset_id': record['id'], 'input_snapshot': record['snapshot_id'],
                   'data_fingerprint': record['fingerprint'], 'information_cutoff_ms': record['as_of_ms'],
                   'historical_phase': 'development', 'phase': 'development', 'config': config,
                   'policy_hash': policy_hash, 'search_protocol': SEARCH_PROTOCOL, 'search_id': search_id,
                   'candidate_index': index, 'candidate_count': len(proposals), 'candidate_key': candidate_key,
                   'hypothesis': proposal['hypothesis'], 'evidence_ids': proposal['evidence_ids']}
        experiment = store.register_experiment(spec, policy=policy, context=context)
        effective_alpha, dataset_allocation_index = _dataset_alpha_allocation(
            store, experiment, record['fingerprint'])
        proposal_context.append({'index': index, 'proposal': proposal, 'spec': spec,
                                 'experiment': experiment, 'candidate_key': candidate_key,
                                 'effective_alpha': effective_alpha,
                                 'dataset_allocation_index': dataset_allocation_index})

    with store.connection() as db:
        attempt_count = db.execute('SELECT COUNT(*) FROM experiment_attempts').fetchone()[0]
        experiment_count = db.execute('SELECT COUNT(*) FROM experiments').fetchone()[0]
    prior_trial_count = store.get_document('statistical_trial_count', 0)
    trial_count = max(len(proposals), attempt_count, experiment_count, prior_trial_count if isinstance(prior_trial_count, int) else 0, 1)

    results = []
    for item in proposal_context:
        experiment = item['experiment']
        existing = store.experiment(experiment['id']) or {}
        saved = next((row for row in existing.get('evaluations', [])
                      if row.get('candidate_cache_key') == item['candidate_key']), None)
        if saved is not None:
            edge = deepcopy(saved['report'])
            _refresh_search_correction(edge, trial_count, item['effective_alpha'])
            cache_hit = True
        else:
            edge = analytics.validate_edges([item['proposal']], markets, trial_count=trial_count,
                                             config=config, alpha_budget=item['effective_alpha'])[0]
            _refresh_search_correction(edge, trial_count, item['effective_alpha'])
            cache_hit = False
        edge['experiment_id'] = experiment['id']
        edge['alpha_budget'] = item['effective_alpha']
        edge['registry_alpha_budget'] = experiment['alpha_budget']
        edge['dataset_alpha_allocation_index'] = item['dataset_allocation_index']
        edge['search'] = {'id': search_id, 'candidate_index': item['index'],
                          'candidate_count': len(proposals), 'trial_count': trial_count,
                          'evaluation_cache_hit': cache_hit, 'development_only': True}
        store.record_evaluation(experiment['id'], {'phase': 'development',
            'search_id': search_id, 'candidate_cache_key': item['candidate_key'],
            'candidate_index': item['index'], 'trial_count': trial_count,
            'alpha_budget': item['effective_alpha'], 'registry_alpha_budget': experiment['alpha_budget'],
            'dataset_alpha_allocation_index': item['dataset_allocation_index'], 'cache_hit': cache_hit,
            'dataset_id': record['id'], 'data_fingerprint': record['fingerprint'],
            'policy_hash': policy_hash, 'report': edge})
        results.append(edge)

    results.sort(key=lambda row: (
        row.get('metrics', {}).get('net_return') is not None,
        row.get('metrics', {}).get('net_return') if row.get('metrics', {}).get('net_return') is not None else float('-inf'),
        -row['search']['candidate_index']), reverse=True)
    for rank, row in enumerate(results, 1):
        row['rank'] = rank

    report = {'search_id': search_id, 'phase': 'development', 'development_only': True,
              'status': 'complete', 'cache_hit': False,
              'dataset': record,
              'search_settings': search_settings,
              'provenance': {'protocol': SEARCH_PROTOCOL, 'dataset_fingerprint': record['fingerprint'],
                  'input_snapshot': record['snapshot_id'], 'information_cutoff_ms': record['as_of_ms'],
                  'dataset_source': record['source'], 'policy_hash': policy_hash,
                  'frozen_policy': policy, 'frozen_config': config, 'config_fingerprint': config_hash,
                  'search_implementation_hash': implementation_hash,
                  'alpha_allocation': ('Existing SQLite experiment registry allocation: 0.05 / (ordinal * (ordinal + 1)); '
                      'each rule allocation is split across distinct dataset fingerprints using 1 / (k * (k + 1)).'),
                  'historical_performance_is_not_forward_evidence': True},
              'accounting': {'requested_limit': max_candidates,
                  'available_grid_size': grid_size,
                  'generated_unique_candidates': len(proposals), 'registered_attempts_this_run': len(proposals),
                  'registered_experiments_total': experiment_count, 'all_registry_attempts_total': attempt_count,
                  'trial_count_used': trial_count, 'candidate_backtests': sum(not row['search']['evaluation_cache_hit'] for row in results),
                  'candidate_cache_hits': sum(row['search']['evaluation_cache_hit'] for row in results),
                  'duplicate_candidates_suppressed': 0,
                  'breadth_truncated': grid_size > len(proposals)},
              'ranking': {'metric': 'chronological holdout net return after modeled costs',
                  'order': 'descending; tie breaks preserve deterministic candidate order',
                  'interpretation': 'Development ranking only; rejection and inconclusive results remain visible.'},
              'results': results}
    store.set_document(run_key, {'status': 'complete', 'search_id': search_id, 'report': report})
    return report


def _refresh_search_correction(edge, trial_count, alpha_budget):
    """Keep cached reports conservative when a later search has more trials."""
    metrics = edge.get('metrics', {})
    p_value = metrics.get('p_value')
    if isinstance(p_value, (int, float)) and not isinstance(p_value, bool):
        metrics['trial_count'] = trial_count
        metrics['adjusted_p_value'] = min(1.0, p_value * trial_count)
        validation = edge.get('validation', {})
        validation['trial_count'] = trial_count
        reasons = [reason for reason in edge.get('reason', '').split('; ') if reason]
        significance_reason = 'Holdout evidence fails the multiple-testing corrected significance threshold'
        if p_value > alpha_budget and significance_reason not in reasons:
            reasons.append(significance_reason)
        elif p_value <= alpha_budget and significance_reason in reasons:
            reasons.remove(significance_reason)
        if metrics['adjusted_p_value'] > analytics.ALPHA:
            family_reason = 'Search-adjusted p-value exceeds the 5% family-wise screen'
            if family_reason not in reasons:
                reasons.append(family_reason)
        edge['reason'] = '; '.join(reasons)
        if reasons:
            edge['status'] = 'rejected'
        elif edge.get('status') == 'rejected':
            edge['status'] = 'validated'
    return edge


def _dataset_alpha_allocation(store, experiment, data_fingerprint):
    """Split one frozen rule's registry allocation over distinct search datasets.

    The registry allocates alpha once per canonical rule/policy. Reusing that
    allocation on several historical datasets would overstate evidence, so each
    data fingerprint gets a summable nested share. Repeating the same dataset
    does not consume another share.
    """
    with store.connection() as db:
        row = db.execute('SELECT spec_hash FROM experiments WHERE id=?', (experiment['id'],)).fetchone()
        attempts = db.execute('SELECT payload FROM experiment_attempts WHERE spec_hash=? ORDER BY id',
                              (row['spec_hash'],)).fetchall()
    fingerprints = []
    for attempt in attempts:
        try:
            fingerprint = json.loads(attempt['payload']).get('context', {}).get('data_fingerprint')
        except (ValueError, TypeError):
            fingerprint = None
        if isinstance(fingerprint, str) and fingerprint not in fingerprints:
            fingerprints.append(fingerprint)
    if data_fingerprint not in fingerprints:
        fingerprints.append(data_fingerprint)
    allocation_index = fingerprints.index(data_fingerprint) + 1
    alpha = experiment['alpha_budget'] / (allocation_index * (allocation_index + 1))
    return alpha, allocation_index


def _candidate_cache_key(data_fingerprint, spec, policy_hash, config_hash, implementation_hash):
    payload = [data_fingerprint, analytics.spec_hash(spec), policy_hash, config_hash, implementation_hash]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _search_implementation_hash():
    return hashlib.sha256(Path(__file__).read_bytes() + Path(strategy_search.__file__).read_bytes()).hexdigest()


def _claim_or_read_search(store, key, search_id):
    """Atomically reuse a completed report or claim one in-progress execution."""
    now = round(time.time() * 1000)
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT value FROM cascade_meta WHERE key=?', (key,)).fetchone()
        if row:
            saved = json.loads(row['value'])
            if saved.get('status') == 'complete':
                return saved
            started = saved.get('started_at_ms', now)
            if saved.get('status') == 'running' and now - started < 3_600_000:
                return {'status': 'running', 'search_id': search_id}
        saved = {'status': 'running', 'search_id': search_id, 'started_at_ms': now}
        db.execute('INSERT INTO cascade_meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                   (key, json.dumps(saved, sort_keys=True, separators=(',', ':'), allow_nan=False)))
    return None


def observe_dataset(store, document):
    """Ingest a newly observed vendor snapshot; never replay old fills as forward."""
    from .orchestrator import Runtime
    record = import_dataset(store, document)
    if any(market.get('asset_class') != 'equity' for market in document['markets']):
        raise ValueError('Prospective observations require explicit equity metadata')
    cutoff = record['as_of_ms']
    evidence = temporal.evidence_as_of(document.get('evidence', []), cutoff)
    store.save_evidence(evidence)
    markets = [temporal.market_as_of({**market, 'session_known_at': market.get('session_known_at', cutoff)}, cutoff)
               for market in document['markets']]
    features = temporal.event_features(evidence, cutoff)
    index = store.get_document('event_features_by_symbol', {})
    for market in markets:
        known = {feature['id']: feature for feature in index.get(market['symbol'], [])}
        known.update({feature['id']: feature for feature in features if feature['symbol'] == market['symbol']})
        index[market['symbol']] = list(known.values())[-2000:]
    store.set_document('event_features_by_symbol', index)
    benchmarks = store.config()['research']['benchmark_symbols']
    symbols = [market['symbol'] for market in markets if market['symbol'] not in benchmarks]
    if not symbols:
        raise ValueError('Observation snapshot requires a research equity')
    store.set_document('sector_universe', {'id':'ai_infrastructure','name':'Imported research universe',
        'symbols':symbols,'companies':[{'symbol':symbol,'name':symbol,'themes':[],
            'source_urls':[],'evidence_ids':[],'discovery_reason':'Operator-provided versioned dataset; membership unverified.'} for symbol in symbols],
        'discovery_status':'provider_declared'})
    runtime = Runtime(store, key_loader=lambda: None)
    try:
        runtime._merge_markets(markets)
        runtime._execute([])
        return {'dataset': record, 'research': store.research_summary(),
                'portfolio': store.get_document('portfolio'), 'mode':'paper', 'provider_calls_made':False}
    finally:
        runtime.shutdown()
