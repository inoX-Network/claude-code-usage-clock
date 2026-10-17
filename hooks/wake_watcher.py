#!/usr/bin/env python3
"""Wake-up watcher: wakes an idle session shortly before its prompt cache expires, so the session-end routine
runs while the cache is still warm. Usage: `python3 wake_watcher.py <session_id>`.

The context reporter offers it (see context_reporter.py); the model starts it once in the background. It polls
the per-session state file of the status line (`<log_dir>/context/<session_id>.json`, see _context_state.py)
every minute and ends with one message, which Claude Code hands to the model as the result of the background
task. That message is the wake-up call, or says why the watcher stopped.

It fires when the context has reached `wake_watcher.from_k` thousand tokens, the cache is warm, and the cache
expires within the lead time: the smaller of `wake_watcher.lead_min` and a fifth of the cache lifetime (10 min
for the 1-hour cache, 1 min for the 5-minute cache). A cold or unknown cache never fires: a wake-up on a cold
cache would write the whole context to the cache again. When the user writes, the expiry moves and the watcher
waits on.

It stops quietly (message, exit 0) when
- `wake_watcher.from_k` is 0 (the default: off),
- another watcher for the session is alive (heartbeat file `<session_id>.watch`, see _wake.py),
- the session is gone: the state file is missing or older than STALE_MIN minutes on STALE_POLLS polls in a row
  (after `/clear` or closing the session the status line stops writing; a sleeping machine does not count, the
  counter starts again with the first fresh file),
- it has run MAX_RUN_MIN minutes (Claude Code stops background tasks after two hours); it first writes the
  heartbeat `ended`, so the turn that delivers its message already offers the next watcher (exception below),
- the heartbeat file cannot be written.

The one exception at MAX_RUN_MIN: a restart message is a model turn that renews the prompt cache, and the idle time
starts again while the user is long gone. So if the state file is fresh, the cache is warm, the context has reached
`from_k` and the user has been idle for IDLE_FIRE_AT_MAX_RUN_MIN minutes (the cache lifetime minus what is left),
the watcher fires the wake-up call (heartbeat `fired`) instead of the restart message.

An invalid session id is exit 2. Nothing else is read or written; only the standard library is used.
"""
from __future__ import annotations

import math
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _context_state  # noqa: E402
import _usage  # noqa: E402
import _wake  # noqa: E402
import context_reporter  # noqa: E402

POLL_S = 60
STALE_MIN = 2
STALE_POLLS = 3
MAX_RUN_MIN = 115
IDLE_FIRE_AT_MAX_RUN_MIN = 20
TTL_MIN = {'1h': 60, '5m': 5}


def _warm_cache(state: dict, used: int, from_k: float) -> tuple[int, float] | None:
    """(cache lifetime in minutes, expiry) if the context has reached `from_k` thousand tokens and the cache clock
    is usable (known lifetime, positive finite expiry), else None. Whether the cache is still warm is up to the
    caller: it depends on the clock."""
    ttl = state.get('cache_ttl')
    expires = state.get('cache_expires_at')
    if used < from_k * 1000 or ttl not in _context_state.CACHE_TTLS or not _usage._number(expires) or expires <= 0:
        return None
    return TTL_MIN[ttl], expires


def due(state: dict, used: int, from_k: float, lead_min: float, now: float) -> dict | None:
    """The facts for the wake-up call if it is time (enough context, warm cache, expiry within the lead time),
    else None. `used` is the measured context of the same state."""
    cache = _warm_cache(state, used, from_k)
    if cache is None:
        return None
    ttl_min, expires = cache
    lead_s = min(lead_min, ttl_min / 5) * 60
    rest = expires - now
    if not 0 < rest <= lead_s:
        return None
    return {'ttl_min': ttl_min, 'rest_s': rest, 'used': used}


def idle_at_limit(state: dict, used: int, from_k: float, now: float) -> dict | None:
    """The facts for the wake-up call at the maximum run time (same format as `due`) if the cache is still warm and
    the user has been idle for at least IDLE_FIRE_AT_MAX_RUN_MIN minutes (the lifetime minus what is left), else
    None. The lead time does not matter here: the watcher cannot wait any longer."""
    cache = _warm_cache(state, used, from_k)
    if cache is None:
        return None
    ttl_min, expires = cache
    rest = expires - now
    if rest <= 0 or ttl_min * 60 - rest < IDLE_FIRE_AT_MAX_RUN_MIN * 60:
        return None
    return {'ttl_min': ttl_min, 'rest_s': rest, 'used': used}


def _fired_text(session_id: str, facts: dict, from_k: float, routine_file: str) -> str:
    """The wake-up call. Its start is what the context reporter looks for in the task output before it adds the
    routine of `wake_watcher.routine_file`; with that file set, the call points to it."""
    rest_min = math.ceil(facts['rest_s'] / 60)
    routine = ('it comes with this message (wake_watcher.routine_file); work through every step of it (if it is '
               'missing: save progress, commit, push, summary)') if routine_file else (
               '(save progress, commit, push, summary)')
    return (f'Wake-up call for session {session_id}: the user has been away for about {facts["ttl_min"] - rest_min} '
            f'min, the prompt cache has about {rest_min} min left, the context is {facts["used"] // 1000}k tokens '
            f'(wake-up threshold {from_k:g}k). The user is away. Run your session-end routine now, without asking'
            f'{": " if routine_file else " "}{routine}, then end the turn. Do not start a new watcher.')


def _fresh_state(directory: str, session_id: str, moment: float) -> dict | None:
    """The state file of the session if it is alive at `moment` (not older than STALE_MIN minutes), else None."""
    state = context_reporter.read_state(directory, session_id)
    if state is not None and context_reporter.is_fresh(state.get('measured_at'),
                                                      datetime.fromtimestamp(moment, timezone.utc), STALE_MIN):
        return state
    return None


def _end_of_run(session_id: str, cfg: dict, directory: str, moment: float) -> str:
    """The closing message at the maximum run time. A restart message is a model turn that renews the prompt cache
    and starts the idle time again, so with a warm cache, enough context and a long idle time the watcher wakes
    the session now instead (heartbeat `fired`). Otherwise it writes `ended`, so the turn that delivers the
    restart message already offers the next watcher. A heartbeat that cannot be written changes nothing: the
    last `watching` beat blocks a second offer for a while anyway."""
    from_k = cfg['wake_watcher']['from_k']
    state = _fresh_state(directory, session_id, moment)
    measured = context_reporter.numbers(state) if state is not None else None
    facts = idle_at_limit(state, measured[1], from_k, moment) if measured is not None else None
    try:
        _wake.write_heartbeat(directory, session_id, 'ended' if facts is None else 'fired', moment)
    except OSError:
        pass
    if facts is not None:
        return _fired_text(session_id, facts, from_k, cfg['wake_watcher']['routine_file'])
    return (f'Wake-up watcher stopped: session {session_id} reached its maximum run time of {MAX_RUN_MIN} '
            'minutes. The reminder that comes with this message offers a new watcher; if it does, start it '
            'again.')


def watch(session_id: str, cfg: dict, directory: str, now=time.time, sleep=time.sleep) -> str:
    """Polls until there is a reason to end; returns the closing message. `now` and `sleep` are injectable."""
    from_k, lead_min = cfg['wake_watcher']['from_k'], cfg['wake_watcher']['lead_min']
    if from_k == 0:
        return (f'Wake-up watcher for session {session_id}: disabled (wake_watcher.from_k is 0 in config.json). '
                'Nothing to do.')
    beat = _wake.read_heartbeat(directory, session_id)
    if beat is not None and beat['state'] == 'watching' and _wake.blocks_hint(beat, now()):
        return (f'Wake-up watcher for session {session_id}: already running (another watcher for this session is '
                'alive). Nothing to do.')
    started = now()
    missed = 0
    while True:
        moment = now()
        if moment - started >= MAX_RUN_MIN * 60:
            return _end_of_run(session_id, cfg, directory, moment)
        state = _fresh_state(directory, session_id, moment)
        if state is not None:
            missed = 0
        else:
            missed += 1
            if missed >= STALE_POLLS:
                return (f'Wake-up watcher stopped: session {session_id} is no longer active (cleared or closed). '
                        'If you are a different session, there is nothing to do.')
        try:
            _wake.write_heartbeat(directory, session_id, 'watching', moment)
            measured = context_reporter.numbers(state) if state is not None else None
            facts = due(state, measured[1], from_k, lead_min, moment) if measured is not None else None
            if facts is not None:
                _wake.write_heartbeat(directory, session_id, 'fired', moment)
                return _fired_text(session_id, facts, from_k, cfg['wake_watcher']['routine_file'])
        except OSError as exc:
            return (f'Wake-up watcher stopped: session {session_id}: the heartbeat file cannot be written '
                    f'({exc.strerror or exc}). Nothing to do.')
        sleep(POLL_S)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or not _context_state.SESSION_ID.fullmatch(argv[0]):
        print('Wake-up watcher: expected one argument, the session_id (letters, digits, - and _, at most 128 '
              'characters). Nothing to do.')
        return 2
    import _config
    import _hookio
    print(watch(argv[0], _config.load(log_problems=False), os.path.join(_hookio.log_dir(), 'context')))
    return 0


if __name__ == '__main__':
    sys.exit(main())
