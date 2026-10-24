import { expect, test } from 'claude-code/testing'

import { statusInput, statuslineArgv } from './record'

const usage = {
  startedAt: 0,
  context: { tokens: 340_000, window: 1_000_000, percent: 34 },
  rateLimits: [
    { kind: 'five_hour', percentUsed: 17.5, resetsAt: '2026-10-17T10:50:00Z' },
    { kind: 'seven_day', percentUsed: 76, resetsAt: '2026-10-19T13:00:00.000Z' },
    { kind: 'spend_limit', percentUsed: 120 },
  ],
  cost: { usd: 3.5 },
}

test('the status line input: rate limits in Unix seconds, the context window, the session id', () => {
  expect(statusInput(usage, 'desk-1')).toEqual({
    session_id: 'desk-1',
    rate_limits: {
      five_hour: { used_percentage: 17.5, resets_at: Date.parse('2026-10-17T10:50:00Z') / 1000 },
      seven_day: { used_percentage: 76, resets_at: Date.parse('2026-10-19T13:00:00Z') / 1000 },
    },
    context_window: { context_window_size: 1_000_000, total_input_tokens: 340_000, used_percentage: 34 },
  })
})

test('a window without a valid reset time is left out: the merge would drop it anyway', () => {
  const broken = { ...usage, rateLimits: [
    { kind: 'five_hour', percentUsed: 20 },
    { kind: 'seven_day', percentUsed: 76, resetsAt: 'soon' },
  ] }
  expect(statusInput(broken, 'desk-1').rate_limits).toEqual({})
})

test('before the first answer the context has no tokens and no percentage', () => {
  const fresh = { startedAt: 0, context: { window: 200_000 }, rateLimits: [] }
  expect(statusInput(fresh, 'desk-1')).toEqual({
    session_id: 'desk-1',
    rate_limits: {},
    context_window: { context_window_size: 200_000, total_input_tokens: 0 },
  })
})

test('the command of install.py, Windows and POSIX spelling', () => {
  expect(statuslineArgv('"C:/Program Files/Python314/python.exe" -X utf8 "C:/Users/you/.claude/hooks/usage-clock/statusline.py"'))
    .toEqual(['C:/Program Files/Python314/python.exe', '-X', 'utf8', 'C:/Users/you/.claude/hooks/usage-clock/statusline.py'])
  expect(statuslineArgv('python3 /home/you/.claude/hooks/usage-clock/statusline.py'))
    .toEqual(['python3', '/home/you/.claude/hooks/usage-clock/statusline.py'])
  expect(statuslineArgv("python3 '/home/you/my hooks/statusline.py'")).toEqual(['python3', '/home/you/my hooks/statusline.py'])
  expect(statuslineArgv('/usr/bin/python3 /home/you/.claude/hooks/usage-clock/statusline.py'))
    .toEqual(['/usr/bin/python3', '/home/you/.claude/hooks/usage-clock/statusline.py'])
})

test('a path the open project could answer is not run: a relative script or interpreter', () => {
  for (const command of [
    'python3 ./statusline.py',
    'python3 statusline.py',
    'python3 hooks/statusline.py',
    'python3 C:statusline.py',
    './venv/bin/python /home/you/.claude/hooks/usage-clock/statusline.py',
    "bash -c 'id > /tmp/pwned' statusline.py",
    'node evil.js statusline.py',
  ]) {
    expect(statuslineArgv(command)).toBeNull()
  }
})

test('anything else is not run: another status line, shell syntax, a broken or missing command', () => {
  for (const command of [
    undefined,
    42,
    '',
    'npx ccusage statusline',
    'python3 /home/you/statusline.py.bak',
    'python3 /home/you/.claude/hooks/usage-clock/statusline.py | tee /tmp/line',
    'python3 /home/you/.claude/hooks/usage-clock/statusline.py; rm -rf ~',
    'python3 /home/you/.claude/hooks/usage-clock/statusline.py && echo',
    'python3 "/home/you/.claude/hooks/usage-clock/statusline.py',
    'statusline.py',
    'python3 a/statusline.py b/statusline.py',
  ]) {
    expect(statuslineArgv(command)).toBeNull()
  }
})
