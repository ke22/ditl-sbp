"""Opt-in generation timing; never used as a research input or retry policy."""
from __future__ import annotations

import contextvars
import json
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from uuid import uuid4

_CURRENT = contextvars.ContextVar('generation_performance_trace', default=None)


class Trace:
    def __init__(self, stage: str, clock=None):
        self.clock = clock or time.perf_counter
        self.start = self.clock()
        self.stage = stage
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.events = []
        self.attempts = []
        self.outcome = 'failed'

    def begin_attempt(self, number, scope):
        if self.attempts:
            self.attempts[-1]['retry_scope'] = scope
        self.attempts.append({'attempt': number, 'scope': scope, 'status': 'calling'})

    def validation(self, errors):
        if self.attempts:
            self.attempts[-1].update(status='rejected' if errors else 'accepted',
                                     error_count=len(errors), errors=[str(e) for e in errors[:20]])

    def report(self):
        elapsed = self.clock() - self.start
        def total(name, predicate=lambda e: True):
            return sum(e['seconds'] for e in self.events if e['phase'] == name and predicate(e))
        phases = {name: total(name) for name in ('prepare', 'model_wait', 'validation', 'write', 'retry_prepare')}
        attempts = []
        for saved in self.attempts:
            attempt = dict(saved)
            number = attempt['attempt']
            attempt['model_wait_seconds'] = total('model_wait', lambda e: e.get('attempt') == number)
            attempt['validation_seconds'] = total('validation', lambda e: e.get('attempt') == number)
            attempt['model_call_count'] = sum(e['phase'] == 'model_call' and e.get('attempt') == number
                                              for e in self.events)
            if attempt['status'] == 'calling' and self.outcome != 'succeeded':
                attempt['status'] = 'failed_before_validation'
            attempts.append(attempt)
        return {'schema_version': 1, 'stage': self.stage, 'started_at': self.started_at,
                'outcome': self.outcome, 'elapsed_seconds': elapsed,
                'scope': 'structure CLI after argument parsing; excludes interpreter startup and lock acquisition',
                'call_boundary': 'llm.call; provider-internal queueing and retries are not separately observed',
                'phase_seconds': phases,
                'other_local_seconds': max(0.0, elapsed - sum(phases.values())),
                'first_generation_wait_seconds': total('model_wait', lambda e: e.get('attempt') == 1),
                'retry_generation_wait_seconds': total('model_wait', lambda e: e.get('attempt', 0) > 1),
                'model_call_count': sum(e['phase'] == 'model_call' for e in self.events),
                'transport_backoff_seconds': total('transport_backoff'),
                'attempts': attempts, 'events': self.events}


def current():
    return _CURRENT.get()


@contextmanager
def phase(name, **metadata):
    trace = current()
    if trace is None:
        yield
        return
    started = trace.clock()
    event = {'phase': name, 'start_seconds': started - trace.start,
             **({'attempt': trace.attempts[-1]['attempt']} if trace.attempts else {}), **metadata}
    try:
        yield event
    except BaseException as exc:
        event['exception_type'] = type(exc).__name__
        raise
    finally:
        event['seconds'] = trace.clock() - started
        trace.events.append(event)


def timed(name):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            with phase(name):
                return fn(*args, **kwargs)
        return wrapped
    return decorate


@contextmanager
def capture(stage, directory, *, clock=None):
    trace = Trace(stage, clock)
    token = _CURRENT.set(trace)
    try:
        yield trace
    except BaseException as exc:
        trace.outcome = 'cancelled' if isinstance(exc, KeyboardInterrupt) else 'failed'
        raise
    finally:
        _CURRENT.reset(token)
        try:
            path = Path(directory)
            path.mkdir(parents=True, exist_ok=True)
            target = path / f'{stage}-performance-{uuid4().hex}.json'
            target.write_text(json.dumps(trace.report(), ensure_ascii=False, indent=2), encoding='utf-8')
            print(f'效能量測紀錄：{target}', file=sys.stderr)
        except Exception as exc:
            print(f'效能量測紀錄未寫入：{exc}', file=sys.stderr)
