import { expect, mock, test } from 'claude-code/testing'

const COMMAND = '"C:/Python314/python.exe" -X utf8 "C:/Users/you/.claude/hooks/usage-clock/statusline.py"'
const USAGE = {
  startedAt: 0,
  context: { tokens: 340_000, window: 1_000_000, percent: 34 },
  rateLimits: [{ kind: 'five_hour', percentUsed: 17, resetsAt: '2026-10-17T10:50:00Z' }],
}

type Run = { argv: readonly string[]; stdin?: string; timeoutMs?: number }

function world(on: Parameters<Parameters<typeof test>[1]>[1], runs: Run[],
               { surfaces = ['desktop'], statusLine = { type: 'command', command: COMMAND } as unknown,
                 fails = false, project = {} as Record<string, unknown> } = {}) {
  mock.clock(on, { now: Date.parse('2026-10-17T06:00:00Z') })
  mock.env(on, { USERPROFILE: 'C:\\Users\\you' })
  on('session.usage', () => ({ value: USAGE }))
  on('session.id', () => ({ value: 'desk-1' }))
  on('session.surfaces', () => ({ value: surfaces }))
  // The user's settings.json alone, or merged with what the project's settings set (`project`)
  const user = statusLine === null ? {} : { statusLine }
  on('settings.read', ($, e) => ({ value: e.source === 'user' ? user : { ...user, ...project } }))
  on('session.start', ($, e) => e)
  on('session.measure', ($, e) => ({ changed: e.changed }))
  on('fs.read', () => ({ deny: 'ENOENT' }))
  on('process.run', ($, e) => {
    runs.push({ argv: e.argv, stdin: e.init?.stdin, timeoutMs: e.init?.timeoutMs })
    return fails ? { deny: 'cannot start' } : { value: { exitCode: 0, stdout: '', stderr: '' } }
  })
}

test('each measurement in the app goes through the status line of install.py', async ($, on) => {
  const runs: Run[] = []
  world(on, runs)
  await $.session.start({ cwd: 'C:/proj' })
  await $.session.measure({ ...USAGE, changed: ['context', 'rateLimits'] })
  expect(runs).toHaveLength(1)
  expect(runs[0]?.argv).toEqual(['C:/Python314/python.exe', '-X', 'utf8', 'C:/Users/you/.claude/hooks/usage-clock/statusline.py'])
  expect(runs[0]?.timeoutMs).toBe(10_000)
  expect(JSON.parse(runs[0]?.stdin ?? '')).toEqual({
    session_id: 'desk-1',
    rate_limits: { five_hour: { used_percentage: 17, resets_at: Date.parse('2026-10-17T10:50:00Z') / 1000 } },
    context_window: { context_window_size: 1_000_000, total_input_tokens: 340_000, used_percentage: 34 },
  })
})

test('the measurement carries the numbers, not a second read of the usage', async ($, on) => {
  const runs: Run[] = []
  world(on, runs)
  await $.session.start({ cwd: 'C:/proj' })
  const later = { ...USAGE, rateLimits: [{ kind: 'five_hour', percentUsed: 18, resetsAt: '2026-10-17T10:50:00Z' }] }
  await $.session.measure({ ...later, changed: ['rateLimits'] })
  expect(JSON.parse(runs[0]?.stdin ?? '').rate_limits.five_hour.used_percentage).toBe(18)
})

test('with a terminal attached nothing is run: the status line writes there, with the cache clock', async ($, on) => {
  const runs: Run[] = []
  world(on, runs, { surfaces: ['terminal', 'mobile'] })
  await $.session.start({ cwd: 'C:/proj' })
  await $.session.measure({ ...USAGE, changed: ['context', 'rateLimits'] })
  expect(runs).toEqual([])
})

for (const [name, statusLine] of [
  ['no status line', null],
  ['another status line', { type: 'command', command: 'npx ccusage statusline' }],
  ['a broken setting', 'broken'],
] as const) {
  test(`${name}: nothing is run`, async ($, on) => {
    const runs: Run[] = []
    world(on, runs, { statusLine })
    await $.session.start({ cwd: 'C:/proj' })
    await $.session.measure({ ...USAGE, changed: ['context', 'rateLimits'] })
    expect(runs).toEqual([])
  })
}

test('a status line that cannot start leaves the measurement alone', async ($, on) => {
  const runs: Run[] = []
  world(on, runs, { fails: true })
  await $.session.start({ cwd: 'C:/proj' })
  const result = await $.session.measure({ ...USAGE, changed: ['context', 'rateLimits'] })
  expect(runs).toHaveLength(1)
  expect(result).toEqual({ changed: ['context', 'rateLimits'] })
})

test('a status line only in the settings of the project is not run: the plugin runs the user\'s own', async ($, on) => {
  const runs: Run[] = []
  world(on, runs, { statusLine: null, project: { statusLine: { type: 'command', command: COMMAND } } })
  await $.session.start({ cwd: 'C:/proj' })
  await $.session.measure({ ...USAGE, changed: ['context', 'rateLimits'] })
  expect(runs).toEqual([])
})

test('a project\'s status line does not take the place of the user\'s', async ($, on) => {
  const runs: Run[] = []
  world(on, runs, { project: { statusLine: { type: 'command', command: '/usr/bin/python3 /proj/statusline.py' } } })
  await $.session.start({ cwd: 'C:/proj' })
  await $.session.measure({ ...USAGE, changed: ['context', 'rateLimits'] })
  expect(runs.map(run => run.argv))
    .toEqual([['C:/Python314/python.exe', '-X', 'utf8', 'C:/Users/you/.claude/hooks/usage-clock/statusline.py']])
})

test('with disableAllHooks nothing is run, as Claude Code runs no status line then', async ($, on) => {
  const runs: Run[] = []
  world(on, runs, { project: { disableAllHooks: true } })
  await $.session.start({ cwd: 'C:/proj' })
  await $.session.measure({ ...USAGE, changed: ['context', 'rateLimits'] })
  expect(runs).toEqual([])
})
