import type { On, SessionContextBreakdown } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'

import { contextRecord } from './context'

// A breakdown as /context has it, cut to what the record reads
const BREAKDOWN = {
  categories: [
    { name: 'System prompt', tokens: 3_100, color: 'promptBorder', isDeferred: false, kind: 'used' },
    { name: 'MCP tools', tokens: 12_400, color: 'permission', isDeferred: false, kind: 'used' },
    { name: 'Free space', tokens: 900_000, color: 'inactive', isDeferred: false, kind: 'free' },
  ],
  totalTokens: 63_000,
  maxTokens: 1_000_000,
  model: 'claude-opus-5-5',
  memoryFiles: [{ path: '/home/you/.claude/CLAUDE.md', type: 'User', tokens: 2_200 }],
  mcpTools: [
    { name: 'query-docs', serverName: 'context7', tokens: 800, isLoaded: true },
    { name: 'navigate', serverName: 'playwright', tokens: 450, isLoaded: false },
  ],
  agents: [{ agentType: 'arbeiter-bauen', source: 'userSettings', tokens: 120 }],
  skills: {
    totalSkills: 40, includedSkills: 38, tokens: 5_600,
    skillFrontmatter: [{ name: 'spec-graph', source: 'userSettings', tokens: 310 }],
  },
} as unknown as SessionContextBreakdown

const RECORD = {
  schema_version: 1,
  session_id: 'desk-1',
  at: '2026-10-18T06:00:00.000Z',
  cwd: 'C:/proj',
  model: 'claude-opus-5-5',
  detail: 'full',
  total_tokens: 63_000,
  max_tokens: 1_000_000,
  categories: [
    { name: 'System prompt', tokens: 3_100, kind: 'used' },
    { name: 'MCP tools', tokens: 12_400, kind: 'used' },
    { name: 'Free space', tokens: 900_000, kind: 'free' },
  ],
  memory_files: [{ path: '/home/you/.claude/CLAUDE.md', type: 'User', tokens: 2_200 }],
  mcp_tools: [
    { server: 'context7', name: 'query-docs', tokens: 800, loaded: true },
    { server: 'playwright', name: 'navigate', tokens: 450, loaded: false },
  ],
  skills: { tokens: 5_600, included: 38, total: 40, items: [{ name: 'spec-graph', tokens: 310 }] },
  agents: [{ type: 'arbeiter-bauen', source: 'userSettings', tokens: 120 }],
}

type Write = { path: string; text: string }

/** The written path in one spelling: the engine reports it in the host's, so `/home/you` comes back as `C:\home\you` on Windows. */
const posix = (path: string | undefined): string | undefined => path?.replace(/\\/g, '/').replace(/^[A-Za-z]:(?=\/)/, '')

function world(on: On, writes: Write[], asked: unknown[],
               { breakdown = BREAKDOWN as SessionContextBreakdown | undefined, config = undefined as string | undefined,
                 logEnv = undefined as string | undefined, failsWrite = false, home = '/home/you' as string | null } = {}) {
  mock.clock(on, { now: Date.parse('2026-10-18T06:00:00Z') })
  mock.env(on, { ...(home === null ? {} : { HOME: home }), ...(logEnv === undefined ? {} : { CCUC_LOG_DIR: logEnv }) })
  on('session.usage', ($, e) => {
    asked.push(e)
    const context = { tokens: 63_000, window: 1_000_000, percent: 6 }
    return { value: { startedAt: 0, rateLimits: [], context: e?.breakdown === undefined || breakdown === undefined ? context : { ...context, breakdown } } }
  })
  on('session.id', () => ({ value: 'desk-1' }))
  on('session.cwd', () => ({ value: 'C:/proj' }))
  on('session.start', ($, e) => e)
  on('turn.complete', ($, e) => ({ text: e.answer }))
  on('fs.read', () => (config === undefined ? { deny: 'ENOENT' } : { value: config }))
  on('fs.write', ($, e) => {
    if (failsWrite) return { deny: 'EACCES' }
    writes.push({ path: e.path, text: e.text })
    return { value: undefined }
  })
}

/** Two answers of the main loop: the record is written in the background after the first, and has settled by the end
 *  of the second. */
async function twoTurns($: Engine) {
  await turn($)
  await turn($)
}

function turn($: Engine, agentId?: string) {
  return $.turn.complete({
    answer: 'ok', durationMs: 10, isAborted: false, turnId: 't', reason: 'answer',
    ...(agentId === undefined ? {} : { agentId }),
  })
}

test('the record holds the names and numbers of the breakdown, in the fields of docs/report-format.md', () => {
  expect(contextRecord(BREAKDOWN, 'desk-1', 'C:/proj', '2026-10-18T06:00:00.000Z')).toEqual(RECORD)
  const noSkills = { ...BREAKDOWN, skills: undefined } as unknown as SessionContextBreakdown
  expect(contextRecord(noSkills, 'desk-1', 'C:/proj', 'x').skills).toBeNull()
})

test('after the first answer of the main loop the session writes its file once, with a full count', async ($, on) => {
  const writes: Write[] = []
  const asked: unknown[] = []
  world(on, writes, asked)
  await $.session.start({ cwd: 'C:/proj', surface: 'terminal', isInteractive: true })
  await turn($, 'sub-1')
  expect(writes).toEqual([])
  await turn($)
  await turn($)
  expect(writes).toHaveLength(1)
  expect(posix(writes[0]?.path)).toBe('/home/you/.cache/claude-usage-clock/context-breakdown/desk-1.json')
  expect(JSON.parse(writes[0]?.text ?? '')).toEqual(RECORD)
  expect(asked).toContainEqual(expect.objectContaining({ breakdown: 'full' }))
})

test('the folder is that of the hooks: CCUC_LOG_DIR, else log_dir of config.json', async ($, on) => {
  const writes: Write[] = []
  world(on, writes, [], { config: JSON.stringify({ log_dir: '~/logs/usage' }) })
  await $.session.start({ cwd: 'C:/proj', surface: 'terminal', isInteractive: true })
  await twoTurns($)
  expect(posix(writes[0]?.path)).toBe('/home/you/logs/usage/context-breakdown/desk-1.json')
})

test('CCUC_LOG_DIR wins over config.json', async ($, on) => {
  const writes: Write[] = []
  world(on, writes, [], { config: JSON.stringify({ log_dir: '~/logs/usage' }), logEnv: '/var/tmp/ccuc/' })
  await $.session.start({ cwd: 'C:/proj', surface: 'terminal', isInteractive: true })
  await twoTurns($)
  expect(posix(writes[0]?.path)).toBe('/var/tmp/ccuc/context-breakdown/desk-1.json')
})

test('no breakdown, or a file that cannot be written: nothing written, the turn goes on', async ($, on) => {
  const writes: Write[] = []
  world(on, writes, [], { breakdown: undefined })
  await $.session.start({ cwd: 'C:/proj', surface: 'terminal', isInteractive: true })
  const result = await turn($)
  expect(result).toBeDefined()
  expect(writes).toEqual([])
})

test('a refused write leaves the turn alone', async ($, on) => {
  const writes: Write[] = []
  world(on, writes, [], { failsWrite: true })
  await $.session.start({ cwd: 'C:/proj', surface: 'terminal', isInteractive: true })
  expect(await turn($)).toBeDefined()
  expect(writes).toEqual([])
})

// /clear starts a session without session.start, its atoms at their initials: the first answer reads the folder itself
test('after /clear (no session.start) the first answer writes to the settled folder, not to a ~ below the cwd', async ($, on) => {
  const writes: Write[] = []
  world(on, writes, [])
  await twoTurns($)
  expect(writes).toHaveLength(1)
  expect(posix(writes[0]?.path)).toBe('/home/you/.cache/claude-usage-clock/context-breakdown/desk-1.json')
})

test('after /clear the folder is still that of the hooks: log_dir of config.json, CCUC_LOG_DIR before it', async ($, on) => {
  const writes: Write[] = []
  world(on, writes, [], { config: JSON.stringify({ log_dir: '~/logs/usage' }) })
  await twoTurns($)
  expect(posix(writes[0]?.path)).toBe('/home/you/logs/usage/context-breakdown/desk-1.json')
})

test('after /clear CCUC_LOG_DIR wins over config.json', async ($, on) => {
  const writes: Write[] = []
  world(on, writes, [], { config: JSON.stringify({ log_dir: '~/logs/usage' }), logEnv: '/var/tmp/ccuc' })
  await twoTurns($)
  expect(posix(writes[0]?.path)).toBe('/var/tmp/ccuc/context-breakdown/desk-1.json')
})

test('no home known: nothing written, the turn goes on', async ($, on) => {
  const writes: Write[] = []
  world(on, writes, [], { home: null })
  await $.session.start({ cwd: 'C:/proj', surface: 'terminal', isInteractive: true })
  expect(await turn($)).toBeDefined()
  await turn($)
  expect(writes).toEqual([])
})

test('no home known after /clear either: nothing written', async ($, on) => {
  const writes: Write[] = []
  world(on, writes, [], { home: null })
  await twoTurns($)
  expect(writes).toEqual([])
})
