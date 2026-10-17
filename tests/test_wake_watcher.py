"""Tests for hooks/wake_watcher.py and hooks/_wake.py: when the watcher fires, when it gives up, the heartbeat
file, the command line, the real process.

The clock and the sleep are injected: no test waits. A test describes what the status line would write at each
poll (a plan), the fake sleep applies the next step and advances the clock by the real poll interval.
"""
import json
import os
import stat
import subprocess
import sys

import pytest

import _config
import _context_state
import _usage
import _wake
import wake_watcher
from conftest import HOOKS

SCRIPT = os.path.join(HOOKS, 'wake_watcher.py')
SID = 'abc-123_X'
T0 = 1_800_000_000.0
MIN = 60


def cfg_with(from_k=200, lead_min=10):
    cfg = json.loads(json.dumps(_config.DEFAULTS))
    cfg['wake_watcher'].update(from_k=from_k, lead_min=lead_min)
    return cfg


def directory(tmp_path):
    return tmp_path / 'log' / 'context'


def write_state(tmp_path, now, used=210_000, ttl=None, expires=None, size=1_000_000, measured=None):
    """The state file as the status line writes it; `measured` overrides the time it claims."""
    _context_state.save(str(directory(tmp_path)), SID, size, used, T0 if measured is None else measured, ttl, expires)


def beat(tmp_path):
    return json.loads((directory(tmp_path) / f'{SID}.watch').read_text(encoding='utf-8'))


class Clock:
    """A clock that only moves when the watcher sleeps; each sleep also applies the next step of the plan."""

    def __init__(self, tmp_path, plan):
        self.tmp_path, self.plan, self.t, self.sleeps, self.step = tmp_path, plan, T0, [], 0
        self.apply()

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds
        self.step += 1
        self.apply()

    def apply(self):
        """Writes (or removes) the state file as the status line would at this moment."""
        item = self.plan[min(self.step, len(self.plan) - 1)]
        path = directory(self.tmp_path) / f'{SID}.json'
        if callable(item):           # a test that needs its own file content
            item(self)
        elif item == 'gone':
            if path.exists():
                path.unlink()
        elif item == 'old':          # a file that was written three minutes ago
            write_state(self.tmp_path, self.t, measured=self.t - 3 * MIN)
        elif item == 'fresh':        # fresh, warm cache, far from expiring
            write_state(self.tmp_path, self.t, ttl='1h', expires=int(self.t) + 50 * MIN, measured=self.t)
        elif item == 'fire':         # fresh, warm 1h cache, 8 minutes left
            write_state(self.tmp_path, self.t, ttl='1h', expires=int(self.t) + 8 * MIN, measured=self.t)
        else:
            raise AssertionError(item)


def run(tmp_path, plan, cfg=None, beat_before=None, **kwargs):
    """(message, clock) of a watcher run against the plan. `beat_before` = (state, age in seconds) of a heartbeat
    that exists when the watcher starts (written after the first state file: the clean-up of the state writer
    works with the fake clock, which is far ahead of the real file times)."""
    clock = Clock(tmp_path, plan)
    if beat_before is not None:
        _wake.write_heartbeat(str(directory(tmp_path)), SID, beat_before[0], T0 - beat_before[1])
    message = wake_watcher.watch(SID, cfg or cfg_with(), str(directory(tmp_path)), now=clock.now, sleep=clock.sleep,
                                 **kwargs)
    return message, clock


def state(**fields):
    base = {'session_id': SID, 'cache_ttl': '1h', 'cache_expires_at': int(T0) + 600}
    base.update(fields)
    return {key: value for key, value in base.items() if value is not ...}


# --- The constants -------------------------------------------------------------------------------------------

def test_the_constants_are_the_documented_ones():
    assert (wake_watcher.POLL_S, wake_watcher.STALE_MIN, wake_watcher.STALE_POLLS, wake_watcher.MAX_RUN_MIN,
            wake_watcher.IDLE_FIRE_AT_MAX_RUN_MIN) == (60, 2, 3, 115, 20)
    assert (_wake.ALIVE_MIN, _wake.REST_AFTER_FIRE_MIN) == (3, 60)


# --- Fire condition: the lead time ---------------------------------------------------------------------------

@pytest.mark.parametrize('ttl,rest_s,lead_min,fires', [
    ('1h', 10 * MIN, 10, True),             # 1 h cache: lead 10 min
    ('1h', 10 * MIN + 1, 10, False),
    ('1h', 11 * MIN, 10, False),
    ('1h', 1, 10, True),
    ('1h', 0, 10, False),                   # already expired: never
    ('1h', -30, 10, False),
    ('5m', 1 * MIN, 10, True),              # 5 min cache: a fifth of it, 1 min
    ('5m', 1 * MIN + 1, 10, False),
    ('5m', 2 * MIN, 10, False),
    ('5m', 1, 10, True),
    ('5m', 0, 10, False),
    ('1h', 5 * MIN, 5, True),               # the configured lead is the smaller one
    ('1h', 5 * MIN + 1, 5, False),
    ('1h', 12 * MIN, 30, True),             # the lifetime is the smaller one: 60 / 5 = 12 min
    ('1h', 12 * MIN + 1, 30, False),
    ('5m', 30, 0.5, True),                  # half a minute configured
    ('5m', 31, 0.5, False),
])
def test_the_lead_time_is_the_smaller_of_lead_min_and_a_fifth_of_the_lifetime(ttl, rest_s, lead_min, fires):
    given = state(cache_ttl=ttl, cache_expires_at=int(T0) + rest_s)
    assert (wake_watcher.due(given, 210_000, 200, lead_min, T0) is not None) is fires


# --- Fire condition: the context -----------------------------------------------------------------------------

@pytest.mark.parametrize('used,fires', [(199_999, False), (200_000, True), (200_001, True), (1_000, False)])
def test_the_context_threshold_is_from_k_times_1000_inclusive(used, fires):
    assert (wake_watcher.due(state(), used, 200, 10, T0) is not None) is fires


def test_the_threshold_may_be_fractional():
    assert wake_watcher.due(state(), 12_499, 12.5, 10, T0) is None
    assert wake_watcher.due(state(), 12_500, 12.5, 10, T0) is not None


# --- Fire condition: cold or unknown cache waits -------------------------------------------------------------

@pytest.mark.parametrize('given', [
    state(cache_ttl=...), state(cache_expires_at=...), state(cache_ttl=..., cache_expires_at=...),
    state(cache_ttl='2h'), state(cache_ttl=None), state(cache_ttl=60), state(cache_ttl='1H'), state(cache_ttl=['1h']),
    state(cache_expires_at=None), state(cache_expires_at='soon'), state(cache_expires_at=True),
    state(cache_expires_at=float('nan')), state(cache_expires_at=float('inf')), state(cache_expires_at=[1]),
    state(cache_expires_at=0), state(cache_expires_at=-5)])
def test_a_missing_or_unusable_cache_clock_never_fires(given):
    assert wake_watcher.due(given, 900_000, 200, 10, T0) is None


def test_due_returns_what_the_message_needs():
    assert wake_watcher.due(state(), 210_500, 200, 10, T0) == {'ttl_min': 60, 'rest_s': 600, 'used': 210_500}
    assert wake_watcher.due(state(cache_ttl='5m', cache_expires_at=int(T0) + 45), 210_500, 200, 10, T0) == {
        'ttl_min': 5, 'rest_s': 45, 'used': 210_500}


# --- The loop ------------------------------------------------------------------------------------------------

def test_the_first_check_is_immediate_and_fires_without_sleeping(tmp_path):
    message, clock = run(tmp_path, ['fire'])
    assert 'Run your session-end routine now' in message
    assert clock.sleeps == []


def test_it_waits_one_poll_at_a_time_until_the_lead_time_is_reached(tmp_path):
    message, clock = run(tmp_path, ['fresh', 'fresh', 'fire'])
    assert 'Run your session-end routine now' in message
    assert clock.sleeps == [60, 60]


def test_below_the_threshold_it_waits_even_with_the_cache_about_to_expire(tmp_path):
    message, clock = run(tmp_path, ['fire'], cfg_with(from_k=300))
    assert 'start it again' in message and len(clock.sleeps) == 115


def test_a_cold_cache_waits_and_never_fires(tmp_path):
    message, _ = run(tmp_path, [lambda c: write_state(c.tmp_path, c.t, measured=c.t)])   # no cache clock at all
    assert 'start it again' in message and 'session-end routine' not in message


def test_a_cache_that_runs_out_before_it_is_used_is_not_woken(tmp_path):
    message, _ = run(tmp_path, [lambda c: write_state(c.tmp_path, c.t, ttl='1h', expires=int(c.t) - 1, measured=c.t)])
    assert 'session-end routine' not in message and 'start it again' in message


def test_when_the_user_writes_the_expiry_moves_away_and_the_watcher_waits_on(tmp_path):
    def step(c):     # the expiry is 50 minutes away for 5 polls (the user keeps writing), then 8 minutes
        write_state(c.tmp_path, c.t, ttl='1h', expires=int(c.t) + (8 * MIN if c.step >= 5 else 50 * MIN), measured=c.t)
    message, clock = run(tmp_path, [step])
    assert 'session-end routine' in message and clock.sleeps == [60] * 5


# --- Session gone --------------------------------------------------------------------------------------------

def test_three_missing_polls_in_a_row_mean_the_session_is_gone(tmp_path):
    message, clock = run(tmp_path, ['gone'])
    assert message.startswith(f'Wake-up watcher stopped: session {SID} is no longer active (cleared or closed).')
    assert 'If you are a different session, there is nothing to do.' in message
    assert clock.sleeps == [60, 60]               # the third poll ends it


def test_two_missing_polls_are_not_enough(tmp_path):
    message, clock = run(tmp_path, ['gone', 'gone', 'fire'])
    assert 'Run your session-end routine now' in message and clock.sleeps == [60, 60]


def test_a_fresh_file_resets_the_counter_the_machine_slept_and_woke(tmp_path):
    message, clock = run(tmp_path, ['gone', 'gone', 'fresh', 'gone', 'gone', 'fire'])
    assert 'Run your session-end routine now' in message and len(clock.sleeps) == 5


def test_three_in_a_row_after_a_reset_end_it(tmp_path):
    message, clock = run(tmp_path, ['gone', 'gone', 'fresh', 'gone', 'gone', 'gone'])
    assert 'no longer active' in message and len(clock.sleeps) == 5


def test_a_file_older_than_two_minutes_counts_as_missing(tmp_path):
    message, clock = run(tmp_path, ['old'])
    assert 'no longer active' in message and clock.sleeps == [60, 60]


@pytest.mark.parametrize('age_s,gone', [(120, False), (121, True), (0, False), (3600, True)])
def test_a_file_exactly_two_minutes_old_is_still_alive_one_second_more_is_not(tmp_path, age_s, gone):
    message, _ = run(tmp_path, [lambda c: write_state(c.tmp_path, c.t, measured=c.t - age_s)])
    assert ('no longer active' in message) is gone


@pytest.mark.parametrize('ahead_s,gone', [(60, False), (61, True), (3 * MIN, True)])
def test_a_state_more_than_a_minute_from_the_future_counts_as_missing(tmp_path, ahead_s, gone):
    message, _ = run(tmp_path, [lambda c: write_state(c.tmp_path, c.t, measured=c.t + ahead_s)])
    assert ('no longer active' in message) is gone


def test_a_fresh_file_with_implausible_numbers_is_alive_but_never_a_reason_to_fire(tmp_path):
    def step(c):     # more tokens used than the window holds
        write_state(c.tmp_path, c.t, used=900_000, size=1_000, ttl='1h', expires=int(c.t) + 60, measured=c.t)
    message, _ = run(tmp_path, [step])
    assert 'session-end routine' not in message and 'start it again' in message


@pytest.mark.posix_only
def test_it_does_not_follow_a_link_in_place_of_the_state_file(tmp_path):
    other = tmp_path / 'elsewhere.json'
    other.write_text(json.dumps(dict(state(), context_window_size=1_000_000, used_tokens=900_000,
                                     measured_at='2027-01-15T08:00:00Z', cache_expires_at=int(T0) + 60)),
                     encoding='utf-8')
    directory(tmp_path).mkdir(parents=True)
    os.symlink(other, directory(tmp_path) / f'{SID}.json')
    message, _ = run(tmp_path, [lambda c: None])
    assert 'no longer active' in message


# --- Maximum run time ----------------------------------------------------------------------------------------

def test_it_ends_after_115_minutes_with_a_message_to_start_again(tmp_path):
    message, clock = run(tmp_path, ['fresh'])
    assert len(clock.sleeps) == 115 and clock.t == T0 + 115 * MIN
    assert message == (f'Wake-up watcher stopped: session {SID} reached its maximum run time of 115 minutes. The '
                       'reminder that comes with this message offers a new watcher; if it does, start it again.')


def test_the_end_after_the_maximum_run_time_leaves_an_ended_heartbeat(tmp_path):
    run(tmp_path, ['fresh'])
    assert beat(tmp_path) == {'session_id': SID, 'state': 'ended', 'at': '2027-01-15T09:55:00Z'}


def test_the_end_after_the_maximum_run_time_frees_the_offer_of_the_reporter_at_once(tmp_path):
    """Seen 13.10.2026: the turn that delivers the end message had no offer, because the last `watching`
    heartbeat of the ending watcher was seconds old. The session then went idle without a watcher."""
    run(tmp_path, ['fresh'])
    end = T0 + 115 * MIN
    assert _wake.blocks_hint(_wake.read_heartbeat(str(directory(tmp_path)), SID), end) is False


def test_an_ended_heartbeat_that_cannot_be_written_still_gives_the_end_message(tmp_path, monkeypatch):
    real = _wake.write_heartbeat

    def only_watching(directory_, session_id, state_, now):
        if state_ == 'ended':
            raise OSError('disk full')
        real(directory_, session_id, state_, now)
    monkeypatch.setattr(_wake, 'write_heartbeat', only_watching)
    message, _ = run(tmp_path, ['fresh'])
    path = directory(tmp_path) / f'{SID}.watch'     # the clean-up of the state writer may have removed it
    assert 'reached its maximum run time' in message and (not path.exists() or beat(tmp_path)['state'] != 'ended')


def test_just_before_the_limit_it_still_polls(tmp_path):
    message, clock = run(tmp_path, ['fresh'] * 114 + ['fire'])
    assert 'Run your session-end routine now' in message and len(clock.sleeps) == 114


# --- Maximum run time: a long idle time with a warm cache wakes instead of restarting ------------------------
#
# Measured 14.10.2026: the restart message is a model turn that renews the cache, the idle time starts again, and
# the user is long gone. So at the limit a warm cache that has been idle for IDLE_FIRE_AT_MAX_RUN_MIN wakes now.

def idle_for(idle_s, ttl='1h', ttl_s=3600, used=210_000):
    """A plan step: the state of a session that has been idle for `idle_s` seconds, at every poll (the expiry
    moves with the clock, so the watcher's own lead-time check never fires)."""
    return lambda c: write_state(c.tmp_path, c.t, used=used, ttl=ttl, expires=int(c.t) + ttl_s - idle_s, measured=c.t)


def raw_state(fields):
    """A plan step that writes the state file by hand, also with values the state writer would drop. `fields`
    is a dict or a function of the clock that returns one; it overrides the defaults."""
    def step(c):
        body = {'session_id': SID, 'context_window_size': 1_000_000, 'used_tokens': 210_000,
                'measured_at': _usage._measured_at(c.t)}
        body.update(fields(c) if callable(fields) else fields)
        _context_state.write_private(str(directory(c.tmp_path)), f'{SID}.json', json.dumps(body))
    return step


def ended_at_the_limit(message, clock):
    return len(clock.sleeps) == 115 and message.startswith('Wake-up watcher stopped:') and 'start it again' in message


@pytest.mark.parametrize('idle_min', [20, 21, 30, 45, 49])
def test_at_the_limit_a_long_idle_time_with_a_warm_cache_wakes_instead_of_restarting(tmp_path, idle_min):
    message, clock = run(tmp_path, [idle_for(idle_min * MIN)])
    assert len(clock.sleeps) == 115 and clock.t == T0 + 115 * MIN
    rest_min = 60 - idle_min
    assert message == (
        f'Wake-up call for session {SID}: the user has been away for about {idle_min} min, the prompt cache has '
        f'about {rest_min} min left, the context is 210k tokens (wake-up threshold 200k). The user is away. Run your '
        'session-end routine now, without asking (save progress, commit, push, summary), then end the turn. Do not '
        'start a new watcher.')


def test_at_the_limit_the_wake_up_call_leaves_a_fired_marker_at_the_time_of_the_limit(tmp_path):
    run(tmp_path, [idle_for(45 * MIN)])
    assert beat(tmp_path) == {'session_id': SID, 'state': 'fired', 'at': '2027-01-15T09:55:00Z'}


def test_at_the_limit_the_fired_marker_holds_the_next_offer_back_for_an_hour(tmp_path):
    run(tmp_path, [idle_for(45 * MIN)])
    end = T0 + 115 * MIN
    assert _wake.blocks_hint(_wake.read_heartbeat(str(directory(tmp_path)), SID), end) is True


@pytest.mark.parametrize('idle_min', [0, 1, 5, 10, 19])
def test_at_the_limit_a_short_idle_time_restarts_with_an_ended_marker(tmp_path, idle_min):
    message, clock = run(tmp_path, [idle_for(idle_min * MIN)])
    assert ended_at_the_limit(message, clock) and 'session-end routine' not in message
    assert beat(tmp_path) == {'session_id': SID, 'state': 'ended', 'at': '2027-01-15T09:55:00Z'}


def test_at_the_limit_exactly_twenty_minutes_of_idle_time_wakes(tmp_path):
    message, _ = run(tmp_path, [idle_for(20 * MIN)])
    assert message.startswith(f'Wake-up call for session {SID}: the user has been away for about 20 min')
    assert beat(tmp_path)['state'] == 'fired'


def test_at_the_limit_one_second_short_of_twenty_minutes_restarts(tmp_path):
    message, clock = run(tmp_path, [idle_for(20 * MIN - 1)])      # 19:59
    assert ended_at_the_limit(message, clock) and beat(tmp_path)['state'] == 'ended'


def test_at_the_limit_one_second_over_twenty_minutes_wakes(tmp_path):
    message, _ = run(tmp_path, [idle_for(20 * MIN + 1)])          # 20:01
    assert message.startswith('Wake-up call') and beat(tmp_path)['state'] == 'fired'


@pytest.mark.parametrize('rest_s', [0, -1, -600, -3000])
def test_at_the_limit_a_cold_cache_restarts_however_long_the_idle_time(tmp_path, rest_s):
    message, clock = run(tmp_path, [lambda c: write_state(c.tmp_path, c.t, ttl='1h', expires=int(c.t) + rest_s,
                                                          measured=c.t)])
    assert ended_at_the_limit(message, clock) and beat(tmp_path)['state'] == 'ended'


@pytest.mark.parametrize('cache', [
    {'cache_ttl': '2h', 'cache_expires_at': 1_800_000_000 + 40 * 60 + 115 * 60},      # unknown lifetime
    {'cache_ttl': None, 'cache_expires_at': 1_800_000_000 + 40 * 60 + 115 * 60},
    {'cache_ttl': '1H', 'cache_expires_at': 1_800_000_000 + 40 * 60 + 115 * 60},
    {'cache_expires_at': 1_800_000_000 + 40 * 60 + 115 * 60},                          # no lifetime
    {'cache_ttl': '1h'},                                                               # no expiry
    {'cache_ttl': '1h', 'cache_expires_at': None},
    {'cache_ttl': '1h', 'cache_expires_at': 'soon'},
    {'cache_ttl': '1h', 'cache_expires_at': True},
    {'cache_ttl': '1h', 'cache_expires_at': 0},
    {'cache_ttl': '1h', 'cache_expires_at': -5},
    {}])                                                                               # no cache clock at all
def test_at_the_limit_an_unknown_or_unusable_cache_clock_restarts(tmp_path, cache):
    message, clock = run(tmp_path, [raw_state(cache)])
    assert ended_at_the_limit(message, clock) and beat(tmp_path)['state'] == 'ended'


def test_at_the_limit_an_expiry_that_is_not_finite_restarts(tmp_path):
    # json.dumps writes the non-finite number as Infinity, which Python reads back
    message, clock = run(tmp_path, [raw_state({'cache_ttl': '1h', 'cache_expires_at': float('inf')})])
    assert ended_at_the_limit(message, clock) and beat(tmp_path)['state'] == 'ended'


def test_at_the_limit_the_five_minute_cache_never_has_twenty_minutes_of_idle_time(tmp_path):
    message, clock = run(tmp_path, [idle_for(2 * MIN, ttl='5m', ttl_s=5 * MIN)])
    assert ended_at_the_limit(message, clock) and beat(tmp_path)['state'] == 'ended'


def test_at_the_limit_an_expiry_further_away_than_the_lifetime_has_no_idle_time_and_restarts(tmp_path):
    message, clock = run(tmp_path, [idle_for(-10 * MIN)])      # expiry 70 minutes away: clock skew
    assert ended_at_the_limit(message, clock) and beat(tmp_path)['state'] == 'ended'


@pytest.mark.parametrize('used,wakes', [(199_999, False), (200_000, True), (200_001, True), (1_000, False)])
def test_at_the_limit_the_context_must_have_reached_from_k(tmp_path, used, wakes):
    message, _ = run(tmp_path, [idle_for(45 * MIN, used=used)])
    assert message.startswith('Wake-up call') is wakes
    assert beat(tmp_path)['state'] == ('fired' if wakes else 'ended')


def test_at_the_limit_the_threshold_of_the_config_counts_not_a_fixed_one(tmp_path):
    message, _ = run(tmp_path, [idle_for(45 * MIN, used=250_000)], cfg_with(from_k=300))
    assert message.startswith('Wake-up watcher stopped:') and beat(tmp_path)['state'] == 'ended'
    message, _ = run(tmp_path, [idle_for(45 * MIN, used=250_000)], cfg_with(from_k=250))
    assert message.startswith('Wake-up call')


def test_at_the_limit_an_implausible_context_restarts(tmp_path):
    message, _ = run(tmp_path, [raw_state(lambda c: {'used_tokens': 900_000, 'context_window_size': 1_000,
                                                     'cache_ttl': '1h', 'cache_expires_at': int(c.t) + 15 * MIN})])
    assert message.startswith('Wake-up watcher stopped:') and beat(tmp_path)['state'] == 'ended'


def test_at_the_limit_a_stale_state_file_restarts_even_with_a_long_idle_time(tmp_path):
    """The state file is read again at the limit with the same freshness rule as in the loop: a file that was
    written three minutes ago says nothing about now."""
    def step(c):
        if c.step < 115:
            idle_for(45 * MIN)(c)
        else:
            write_state(c.tmp_path, c.t, ttl='1h', expires=int(c.t) + 15 * MIN, measured=c.t - 3 * MIN)
    message, clock = run(tmp_path, [step])
    assert ended_at_the_limit(message, clock) and beat(tmp_path)['state'] == 'ended'


def test_at_the_limit_a_missing_state_file_restarts(tmp_path):
    def step(c):
        if c.step < 115:
            idle_for(45 * MIN)(c)
        else:
            (directory(c.tmp_path) / f'{SID}.json').unlink()
    message, clock = run(tmp_path, [step])
    assert ended_at_the_limit(message, clock) and beat(tmp_path)['state'] == 'ended'


def test_at_the_limit_the_state_of_the_moment_counts_not_an_earlier_one(tmp_path):
    """Idle for 45 minutes until the last poll, then the user writes: the expiry is an hour away, so the idle time
    is zero and the watcher must not wake the session under the user's hands."""
    def step(c):
        idle_for(45 * MIN)(c) if c.step < 115 else idle_for(0)(c)
    message, clock = run(tmp_path, [step])
    assert ended_at_the_limit(message, clock) and beat(tmp_path)['state'] == 'ended'


def test_at_the_limit_a_fired_marker_that_cannot_be_written_still_wakes(tmp_path, monkeypatch):
    real = _wake.write_heartbeat

    def only_watching(directory_, session_id, state_, now):
        if state_ == 'fired':
            raise OSError('disk full')
        real(directory_, session_id, state_, now)
    monkeypatch.setattr(_wake, 'write_heartbeat', only_watching)
    message, clock = run(tmp_path, [idle_for(45 * MIN)])
    assert len(clock.sleeps) == 115 and message.startswith(f'Wake-up call for session {SID}:')
    assert 'cannot be written' not in message
    assert beat(tmp_path)['state'] == 'watching'         # the last poll's beat still stands and blocks a second offer


def test_at_the_limit_an_ended_marker_that_cannot_be_written_still_restarts(tmp_path, monkeypatch):
    real = _wake.write_heartbeat

    def only_watching(directory_, session_id, state_, now):
        if state_ == 'ended':
            raise OSError('disk full')
        real(directory_, session_id, state_, now)
    monkeypatch.setattr(_wake, 'write_heartbeat', only_watching)
    message, clock = run(tmp_path, [idle_for(5 * MIN)])
    assert ended_at_the_limit(message, clock)


def test_the_wake_up_call_at_the_limit_starts_as_the_context_reporter_expects_and_brings_the_routine(tmp_path):
    import context_reporter
    routine = tmp_path / 'routine.md'
    routine.write_text('1. Save progress.\n2. Commit and push.\n', encoding='utf-8')
    os.chmod(routine, 0o600)
    cfg = cfg_with()
    cfg['wake_watcher']['routine_file'] = str(routine)
    message, _ = run(tmp_path, [idle_for(45 * MIN)], cfg)
    assert message.startswith(f'Wake-up call for session {SID}:')
    assert 'wake_watcher.routine_file' in message
    output = tmp_path / 'task.output'
    output.write_text(message + '\n', encoding='utf-8')
    prompt = f'<task-notification>\n<output-file>{output}</output-file>\n<status>completed</status>\n</task-notification>'
    assert context_reporter.is_wake_up_call(prompt, SID) is True
    assert context_reporter.wake_routine({'prompt': prompt, 'session_id': SID}, cfg) == (
        context_reporter.ROUTINE_HEADER + '1. Save progress.\n2. Commit and push.')


def test_the_restart_message_at_the_limit_is_not_taken_for_a_wake_up_call(tmp_path):
    import context_reporter
    message, _ = run(tmp_path, [idle_for(5 * MIN)])
    output = tmp_path / 'task.output'
    output.write_text(message + '\n', encoding='utf-8')
    prompt = f'<task-notification>\n<output-file>{output}</output-file>\n<status>completed</status>\n</task-notification>'
    assert context_reporter.is_wake_up_call(prompt, SID) is False


def test_the_limit_check_returns_the_facts_in_the_format_of_due_without_the_lead_time():
    given = state(cache_expires_at=int(T0) + 15 * MIN)        # idle 45 min, 15 min left: not yet in the lead time
    assert wake_watcher.due(given, 210_000, 200, 10, T0) is None
    assert wake_watcher.idle_at_limit(given, 210_000, 200, T0) == {'ttl_min': 60, 'rest_s': 900, 'used': 210_000}
    short = state(cache_ttl='5m', cache_expires_at=int(T0) + 30)
    assert wake_watcher.idle_at_limit(short, 210_000, 200, T0) is None        # 4:30 idle of a 5 min cache


@pytest.mark.parametrize('given', [
    state(cache_ttl=...), state(cache_expires_at=...), state(cache_ttl='2h'), state(cache_ttl=None),
    state(cache_expires_at=None), state(cache_expires_at='soon'), state(cache_expires_at=True),
    state(cache_expires_at=float('nan')), state(cache_expires_at=float('inf')), state(cache_expires_at=0),
    state(cache_expires_at=-5)])
def test_the_limit_check_and_due_reject_the_same_unusable_cache_clocks(given):
    assert wake_watcher.due(given, 900_000, 200, 10, T0) is None
    assert wake_watcher.idle_at_limit(given, 900_000, 200, T0) is None


def test_the_limit_check_needs_the_context_and_a_warm_cache():
    warm = state(cache_expires_at=int(T0) + 15 * MIN)
    assert wake_watcher.idle_at_limit(warm, 199_999, 200, T0) is None
    assert wake_watcher.idle_at_limit(warm, 200_000, 200, T0) is not None
    assert wake_watcher.idle_at_limit(state(cache_expires_at=int(T0)), 210_000, 200, T0) is None      # rest 0
    assert wake_watcher.idle_at_limit(state(cache_expires_at=int(T0) + 1), 210_000, 200, T0) is not None


# --- Only one watcher per session ----------------------------------------------------------------------------

def test_a_living_foreign_heartbeat_ends_the_start_at_once(tmp_path):
    message, clock = run(tmp_path, ['fire'], beat_before=('watching', 60))
    before = (directory(tmp_path) / f'{SID}.watch').read_bytes()
    assert message == (f'Wake-up watcher for session {SID}: already running (another watcher for this session is '
                       'alive). Nothing to do.')
    assert clock.sleeps == [] and (directory(tmp_path) / f'{SID}.watch').read_bytes() == before


@pytest.mark.parametrize('age_s,running', [(0, True), (180, True), (181, False), (3600, False)])
def test_a_heartbeat_counts_as_alive_up_to_exactly_three_minutes(tmp_path, age_s, running):
    message, _ = run(tmp_path, ['fire'], beat_before=('watching', age_s))
    assert ('already running' in message) is running
    assert ('session-end routine' in message) is (not running)


@pytest.mark.parametrize('state_', ['fired', 'ended'])
def test_a_fired_or_ended_heartbeat_does_not_stop_a_start(tmp_path, state_):
    message, _ = run(tmp_path, ['fire'], beat_before=(state_, 0))
    assert 'session-end routine' in message


def test_a_broken_heartbeat_does_not_stop_a_start(tmp_path):
    directory(tmp_path).mkdir(parents=True)
    (directory(tmp_path) / f'{SID}.watch').write_text('not json', encoding='utf-8')
    message, _ = run(tmp_path, ['fire'])
    assert 'session-end routine' in message


# --- Heartbeat file ------------------------------------------------------------------------------------------

def test_every_poll_writes_a_watching_heartbeat_with_the_time_of_the_poll(tmp_path):
    seen = []

    class Spy(Clock):
        def sleep(self, seconds):
            seen.append(beat(self.tmp_path))
            super().sleep(seconds)
    clock = Spy(tmp_path, ['fresh', 'fresh', 'fire'])
    wake_watcher.watch(SID, cfg_with(), str(directory(tmp_path)), now=clock.now, sleep=clock.sleep)
    assert seen == [{'session_id': SID, 'state': 'watching', 'at': '2027-01-15T08:00:00Z'},
                    {'session_id': SID, 'state': 'watching', 'at': '2027-01-15T08:01:00Z'}]


def test_firing_leaves_a_fired_marker_written_before_the_message(tmp_path):
    message, _ = run(tmp_path, ['fresh', 'fire'])
    assert 'session-end routine' in message
    assert beat(tmp_path) == {'session_id': SID, 'state': 'fired', 'at': '2027-01-15T08:01:00Z'}


@pytest.mark.posix_only
def test_the_heartbeat_is_private_and_leaves_no_temporary_file(tmp_path):
    run(tmp_path, ['fresh', 'fire'])
    assert stat.S_IMODE(os.stat(directory(tmp_path) / f'{SID}.watch').st_mode) == 0o600
    assert sorted(os.listdir(directory(tmp_path))) == [f'{SID}.json', f'{SID}.watch']


def test_the_poll_that_finds_the_session_gone_writes_no_heartbeat(tmp_path):
    run(tmp_path, ['gone'])
    assert beat(tmp_path)['at'] == '2027-01-15T08:01:00Z'      # the second poll, not the third that ended it


@pytest.mark.posix_only
def test_the_heartbeat_is_never_written_through_a_link(tmp_path):
    victim = tmp_path / 'victim.txt'
    victim.write_text('keep me', encoding='utf-8')
    directory(tmp_path).mkdir(parents=True)
    os.symlink(victim, directory(tmp_path) / f'{SID}.watch')
    run(tmp_path, ['fresh', 'fire'])
    assert victim.read_text(encoding='utf-8') == 'keep me'
    assert not (directory(tmp_path) / f'{SID}.watch').is_symlink() and beat(tmp_path)['state'] == 'fired'


@pytest.mark.posix_only
def test_a_planted_temporary_link_is_removed_not_written_through(tmp_path):
    victim = tmp_path / 'victim.txt'
    victim.write_text('keep me', encoding='utf-8')
    directory(tmp_path).mkdir(parents=True)
    os.symlink(victim, directory(tmp_path) / f'{SID}.watch.tmp.{os.getpid()}')
    _wake.write_heartbeat(str(directory(tmp_path)), SID, 'watching', T0)
    assert victim.read_text(encoding='utf-8') == 'keep me' and beat(tmp_path)['state'] == 'watching'
    assert sorted(os.listdir(directory(tmp_path))) == [f'{SID}.watch']


@pytest.mark.posix_only
def test_a_link_in_place_of_the_folder_is_never_written_through(tmp_path):
    real = tmp_path / 'real'
    real.mkdir()
    (tmp_path / 'log').mkdir()
    os.symlink(real, directory(tmp_path))
    _wake.write_heartbeat(str(directory(tmp_path)), SID, 'watching', T0)
    assert os.listdir(real) == []


@pytest.mark.parametrize('session_id', ['../x', '', 'a/b', 'x' * 129, None, 5])
def test_a_bad_session_id_writes_no_heartbeat(tmp_path, session_id):
    _wake.write_heartbeat(str(directory(tmp_path)), session_id, 'watching', T0)
    assert not (tmp_path / 'log').exists() and not (tmp_path / 'x.watch').exists()


def test_a_heartbeat_that_cannot_be_written_stops_the_watcher_with_a_message(tmp_path, monkeypatch):
    def boom(*_a):
        raise OSError('disk full')
    monkeypatch.setattr(_wake, 'write_heartbeat', boom)
    message, clock = run(tmp_path, ['fire'])
    assert message == (f'Wake-up watcher stopped: session {SID}: the heartbeat file cannot be written (disk full). '
                       'Nothing to do.')
    assert clock.sleeps == []


def test_a_fired_marker_that_cannot_be_written_stops_the_watcher_without_waking(tmp_path, monkeypatch):
    real = _wake.write_heartbeat

    def only_watching(directory_, session_id, state_, now):
        if state_ == 'fired':
            raise OSError('disk full')
        real(directory_, session_id, state_, now)
    monkeypatch.setattr(_wake, 'write_heartbeat', only_watching)
    message, _ = run(tmp_path, ['fire'])
    assert 'session-end routine' not in message and 'cannot be written' in message


# --- Reading the heartbeat -----------------------------------------------------------------------------------

def heartbeat_file(tmp_path, content):
    directory(tmp_path).mkdir(parents=True, exist_ok=True)
    path = directory(tmp_path) / f'{SID}.watch'
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding='utf-8')
    return path


def read(tmp_path):
    return _wake.read_heartbeat(str(directory(tmp_path)), SID)


@pytest.mark.parametrize('state_', ['watching', 'fired', 'ended'])
def test_a_written_heartbeat_reads_back_as_state_and_epoch_seconds(tmp_path, state_):
    _wake.write_heartbeat(str(directory(tmp_path)), SID, state_, T0)
    assert read(tmp_path) == {'state': state_, 'at': T0}


@pytest.mark.parametrize('content', [
    '', 'not json', '[1]', 'null', '{"x": ' * 5000, '\xff',
    {'session_id': SID, 'state': 'sleeping', 'at': '2027-01-15T08:00:00Z'},
    {'session_id': SID, 'state': None, 'at': '2027-01-15T08:00:00Z'},
    {'session_id': SID, 'state': 'watching'},
    {'session_id': SID, 'state': 'watching', 'at': None},
    {'session_id': SID, 'state': 'watching', 'at': 'yesterday'},
    {'session_id': SID, 'state': 'watching', 'at': '2027-01-15T08:00:00'},         # no time zone
    {'session_id': SID, 'state': 'watching', 'at': 1800000000},
    {'session_id': 'someone-else', 'state': 'watching', 'at': '2027-01-15T08:00:00Z'},
    {'state': 'watching', 'at': '2027-01-15T08:00:00Z'},
    {'session_id': SID, 'state': 'watching', 'at': '2027-01-15T08:00:00Z', 'pad': 'x' * 10_000}])
def test_a_broken_or_odd_heartbeat_reads_as_none(tmp_path, content):
    heartbeat_file(tmp_path, content)
    assert read(tmp_path) is None


def test_a_missing_heartbeat_or_folder_reads_as_none(tmp_path):
    assert read(tmp_path) is None
    directory(tmp_path).mkdir(parents=True)
    assert read(tmp_path) is None


@pytest.mark.posix_only
def test_a_link_in_place_of_the_heartbeat_is_not_followed(tmp_path):
    other = tmp_path / 'elsewhere.watch'
    other.write_text(json.dumps({'session_id': SID, 'state': 'watching', 'at': '2027-01-15T08:00:00Z'}),
                     encoding='utf-8')
    directory(tmp_path).mkdir(parents=True)
    os.symlink(other, directory(tmp_path) / f'{SID}.watch')
    assert read(tmp_path) is None


@pytest.mark.posix_only
def test_a_named_pipe_in_place_of_the_heartbeat_reads_as_none_and_does_not_hang(tmp_path):
    directory(tmp_path).mkdir(parents=True)
    os.mkfifo(directory(tmp_path) / f'{SID}.watch')
    assert read(tmp_path) is None


@pytest.mark.parametrize('session_id', ['../x', '', 'a/b', None, 5])
def test_a_bad_session_id_reads_nothing(tmp_path, session_id):
    assert _wake.read_heartbeat(str(directory(tmp_path)), session_id) is None


def test_the_state_reader_still_reads_state_files_and_never_a_heartbeat_by_default(tmp_path):
    import context_reporter
    _wake.write_heartbeat(str(directory(tmp_path)), SID, 'watching', T0)
    assert context_reporter.read_state(str(directory(tmp_path)), SID) is None
    assert context_reporter.read_state(str(directory(tmp_path)), SID, '.watch')['state'] == 'watching'


# --- The hint rule shared with the reporter ------------------------------------------------------------------

@pytest.mark.parametrize('state_,age_s,blocks', [
    ('watching', 0, True), ('watching', 180, True), ('watching', 181, False), ('watching', -60, True),
    ('watching', -61, False), ('watching', 7200, False),
    ('fired', 0, True), ('fired', 3600, True), ('fired', 3601, False), ('fired', -60, True), ('fired', -61, False),
    ('ended', 0, False), ('ended', 60, False), ('ended', -60, False), ('ended', 3600, False)])
def test_a_living_watcher_or_a_recent_fire_blocks_a_new_hint(state_, age_s, blocks):
    assert _wake.blocks_hint({'state': state_, 'at': T0 - age_s}, T0) is blocks


def test_no_heartbeat_blocks_nothing():
    assert _wake.blocks_hint(None, T0) is False


# --- The command line ----------------------------------------------------------------------------------------

@pytest.mark.parametrize('argv', [[], ['a', 'b'], ['../x'], [''], ['a/b'], ['x' * 129], ['a b'], ['ä']])
def test_a_missing_or_invalid_session_id_prints_a_message_and_exits_with_2(capsys, argv):
    assert wake_watcher.main(argv) == 2
    out = capsys.readouterr().out
    assert out.startswith('Wake-up watcher: ') and 'session_id' in out and out.count('\n') == 1


def test_main_prints_the_closing_message_and_exits_with_0(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(wake_watcher, 'watch', lambda session_id, cfg, folder, **_: f'closing {session_id} {folder}')
    assert wake_watcher.main([SID]) == 0
    assert capsys.readouterr().out == f'closing {SID} {tmp_path / "log" / "context"}\n'


def test_main_without_arguments_reads_sys_argv(monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['wake_watcher.py'])
    assert wake_watcher.main() == 2
    monkeypatch.setattr(sys, 'argv', ['wake_watcher.py', SID])
    assert wake_watcher.main() == 0
    assert 'disabled' in capsys.readouterr().out


def test_from_k_zero_ends_at_once_with_a_message_and_no_heartbeat(tmp_path):
    message, clock = run(tmp_path, ['fire'], cfg_with(from_k=0))
    assert message == (f'Wake-up watcher for session {SID}: disabled (wake_watcher.from_k is 0 in config.json). '
                       'Nothing to do.')
    assert clock.sleeps == [] and not (directory(tmp_path) / f'{SID}.watch').exists()


def test_the_default_config_is_off(tmp_path):
    message, _ = run(tmp_path, ['fire'], json.loads(json.dumps(_config.DEFAULTS)))
    assert 'disabled' in message


def test_the_closing_message_when_it_fires_has_all_the_facts_and_the_instruction(tmp_path):
    message, _ = run(tmp_path, ['fire'])
    assert message == (
        f'Wake-up call for session {SID}: the user has been away for about 52 min, the prompt cache has about 8 min '
        'left, the context is 210k tokens (wake-up threshold 200k). The user is away. Run your session-end routine '
        'now, without asking (save progress, commit, push, summary), then end the turn. Do not start a new watcher.')


def test_with_a_routine_file_the_message_points_to_the_routine_that_comes_with_it(tmp_path):
    cfg = cfg_with()
    cfg['wake_watcher']['routine_file'] = '~/routine.md'
    message, _ = run(tmp_path, ['fire'], cfg)
    assert message == (
        f'Wake-up call for session {SID}: the user has been away for about 52 min, the prompt cache has about 8 min '
        'left, the context is 210k tokens (wake-up threshold 200k). The user is away. Run your session-end routine '
        'now, without asking: it comes with this message (wake_watcher.routine_file); work through every step of it '
        '(if it is missing: save progress, commit, push, summary), then end the turn. Do not start a new watcher.')
    assert message.startswith(f'Wake-up call for session {SID}:')        # what the context reporter looks for


def test_the_message_for_the_five_minute_cache_uses_its_lifetime(tmp_path):
    message, _ = run(tmp_path, [lambda c: write_state(c.tmp_path, c.t, ttl='5m', expires=int(c.t) + 45, measured=c.t)])
    assert 'away for about 4 min, the prompt cache has about 1 min left' in message


def test_the_message_is_one_paragraph_in_english(tmp_path):
    message, _ = run(tmp_path, ['fire'])
    assert '\n' not in message and SID in message


# --- The real process ----------------------------------------------------------------------------------------

def run_process(args, env_extra=None):
    env = dict(os.environ, **(env_extra or {}))
    r = subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, env=env, timeout=30)
    return r.returncode, r.stdout.decode('utf-8'), r.stderr.decode('utf-8')


def test_the_process_ends_at_once_with_exit_0_when_it_is_switched_off(tmp_path):
    code, out, err = run_process([SID])
    assert (code, err) == (0, '') and 'disabled' in out and out.count('\n') == 1


def test_the_process_rejects_an_invalid_session_id_with_exit_2(tmp_path):
    code, out, _ = run_process(['../etc'])
    assert code == 2 and 'session_id' in out


def test_the_process_fires_on_a_fresh_state_with_an_expiring_cache_and_leaves_a_fired_marker(tmp_path):
    import time
    now = time.time()
    _context_state.save(str(directory(tmp_path)), SID, 1_000_000, 250_000, now, '1h', int(now) + 5 * MIN)
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'wake_watcher': {'from_k': 200}}), encoding='utf-8')
    code, out, err = run_process([SID], {'CCUC_CONFIG': str(config)})
    assert (code, err) == (0, '') and 'Run your session-end routine now' in out
    assert beat(tmp_path)['state'] == 'fired'


def test_the_process_does_not_use_the_real_clock_or_sleep_in_the_tests_above():
    import inspect
    signature = inspect.signature(wake_watcher.watch)
    assert signature.parameters['now'].default is wake_watcher.time.time
    assert signature.parameters['sleep'].default is wake_watcher.time.sleep
