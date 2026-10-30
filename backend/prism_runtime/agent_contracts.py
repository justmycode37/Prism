"""Strict wire contracts for dynamic agents; host memory validates semantic outcomes."""
from __future__ import annotations

import copy


def review_response_schema():
    from .research_memory import REVIEW_SCHEMA
    schema = copy.deepcopy(REVIEW_SCHEMA)
    # Runtime reviewers propose bounded rule revisions. Policy changes require
    # explicit new registration; an open-ended object is not a strict Responses
    # schema and must never become a route around a host evidence protocol.
    proposal = {
        'symbol': {'type': 'string', 'maxLength': 32},
        'kind': {'type': 'string', 'enum': ['momentum', 'mean_reversion', 'breakout', 'event_surprise']},
        'parameters': _object({'lookback': {'type': 'integer', 'minimum': 3, 'maximum': 100},
                               'threshold': {'type': 'number', 'minimum': 0, 'maximum': 10}}),
        'hypothesis': {'type': 'string', 'maxLength': 4000},
        'evidence_ids': {'type': 'array', 'items': {'type': 'string', 'maxLength': 160}, 'maxItems': 32},
    }
    schema['properties']['revision'] = {'anyOf': [
        {'type': 'null'}, _object({'proposal': _object(proposal), 'policy': {'type': 'null'}})]}
    return schema


def _object(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}
