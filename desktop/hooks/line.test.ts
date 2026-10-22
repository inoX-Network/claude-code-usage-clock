import { expect, test } from 'claude-code/testing'

import { DEFAULTS } from './config'
import { GATE_MARKER_CASES } from './gate-markers-fixture'
import { CACHE_TTL_MS, gateMarkers, parts, resetText, rounded } from './line'

const NOW = Date.parse('2026-10-17T06:00:00Z')
const ZONE = 'Europe/Berlin'

const usage = {
  startedAt: 0,
  context: { tokens: 340_000, window: 1_000_000, percent: 34 },
  rateLimits: [
    { kind: 'five_hour', percentUsed: 17, resetsAt: '2026-10-17T10:50:00Z' },
    { kind: 'seven_day', percentUsed: 76, resetsAt: '2026-10-19T13:00:00Z' },
  ],
  cost: { usd: 3.5 },
}

test('the whole line, as the status line writes it', () => {
  expect(parts(usage, DEFAULTS, NOW - 6 * 60_000, NOW, ZONE)).toEqual([
    { text: '5h 17% ↻12:50', color: undefined, calm: 'success' },
    { text: 'week 76% ↻Mon 15:00', color: 'error', calm: 'success' },
    { text: '<- week gate: agents warn', color: 'error', attached: true },
    { text: 'ctx 340k/1000k 34%', color: 'warning', calm: 'suggestion' },
    { text: 'cache ~54m', color: undefined, calm: 'claude' },
    { text: '$3.50', color: undefined },
  ])
})

test('the thresholds come from the config', () => {
  const cfg = { ...DEFAULTS, yellowFrom: 70, redFrom: 85, startBlockWeek: 90, contextYellowFromK: 400 }
  expect(parts(usage, cfg, null, NOW, ZONE).map(p => [p.text, p.color])).toEqual([
    ['5h 17% ↻12:50', undefined],
    ['week 76% ↻Mon 15:00', 'warning'],
    ['ctx 340k/1000k 34%', undefined],
    ['$3.50', undefined],
  ])
  const quiet = parts(usage, { ...DEFAULTS, showReset: false, weekStopMarker: false }, null, NOW, ZONE)
  expect(quiet.map(p => p.text)).toEqual(['5h 17%', 'week 76%', 'ctx 340k/1000k 34%', '$3.50'])
})

test('colours start exactly at the thresholds', () => {
  const at = (five: number, tokens: number) => parts(
    { startedAt: 0, context: { tokens, window: 1_000_000 }, rateLimits: [{ kind: 'five_hour', percentUsed: five }] },
    DEFAULTS, null, NOW, ZONE).map(p => p.color)
  expect(at(49, 299_999)).toEqual([undefined, undefined])
  expect(at(50, 300_000)).toEqual(['warning', 'warning'])
  expect(at(74, 499_999)).toEqual(['warning', 'warning'])
  expect(at(75, 500_000)).toEqual(['error', 'error'])
})

test('the weekly reset and the gate marker only from their thresholds on', () => {
  const week = (percent: number) => parts(
    { startedAt: 0, context: { window: 1_000 }, rateLimits: [{ kind: 'seven_day', percentUsed: percent, resetsAt: '2026-10-19T13:00:00Z' }] },
    DEFAULTS, null, NOW, ZONE).map(p => p.text)
  expect(week(49)).toEqual(['week 49%', 'ctx –/1k'])
  expect(week(50)).toEqual(['week 50% ↻Mon 15:00', 'ctx –/1k'])
  expect(week(75)).toEqual(['week 75% ↻Mon 15:00', '<- week gate: agents warn', 'ctx –/1k'])
})

test('the gate marker says what the spawn gate does (week_action)', () => {
  const marker = (weekAction: 'warn' | 'ask' | 'deny') => parts(usage, { ...DEFAULTS, weekAction }, null, NOW, ZONE)
    .find(p => p.attached === true)
  expect(marker('warn')).toEqual({ text: '<- week gate: agents warn', color: 'error', attached: true })
  expect(marker('ask')).toEqual({ text: '<- week gate: agents ask', color: 'error', attached: true })
  expect(marker('deny')).toEqual({ text: '<- week gate: agents blocked', color: 'error', attached: true })
})

test('the context before the first answer, and without a usable window', () => {
  const ctx = (context: { tokens?: number; window: number; percent?: number }) =>
    parts({ startedAt: 0, context, rateLimits: [] }, DEFAULTS, null, NOW, ZONE)
  expect(ctx({ tokens: 0, window: 1_000_000 })).toEqual([{ text: 'ctx –/1000k', color: undefined, calm: 'suggestion' }])
  expect(ctx({ window: 200_000 })).toEqual([{ text: 'ctx –/200k', color: undefined, calm: 'suggestion' }])
  expect(ctx({ tokens: 1500, window: 200_000 })).toEqual([{ text: 'ctx 1k/200k', color: undefined, calm: 'suggestion' }])
  expect(ctx({ tokens: 5000, window: 999 })).toEqual([])
  expect(ctx({ tokens: 5000, window: NaN })).toEqual([])
})

test('the cache clock: an estimate, yellow below the config value, cold when past', () => {
  const cache = (minutesAgo: number, cfg = DEFAULTS) =>
    parts({ startedAt: 0, context: { window: 0 }, rateLimits: [] }, cfg, NOW - minutesAgo * 60_000, NOW, ZONE)
  expect(cache(0)).toEqual([{ text: 'cache ~60m', color: undefined, calm: 'claude' }])
  expect(cache(55)).toEqual([{ text: 'cache ~5m', color: undefined, calm: 'claude' }])
  expect(cache(56)).toEqual([{ text: 'cache ~4m', color: 'warning', calm: 'claude' }])
  expect(cache(51, { ...DEFAULTS, cacheYellowBelowMin: 10 })).toEqual([{ text: 'cache ~9m', color: 'warning', calm: 'claude' }])
  expect(cache(CACHE_TTL_MS / 60_000)).toEqual([{ text: 'cache cold', color: 'error' }])
  expect(cache(90)).toEqual([{ text: 'cache cold', color: 'error' }])
})

test('rounding is half to even, like the status line', () => {
  expect([74.4, 74.5, 74.6, 75.5, 0.5, 1.5, 100].map(rounded)).toEqual([74, 74, 75, 76, 0, 2, 100])
})

test('a reset that is past, missing or broken shows nothing', () => {
  expect(resetText('2026-10-17T05:59:00Z', NOW, false, ZONE)).toBe('')
  expect(resetText(undefined, NOW, false, ZONE)).toBe('')
  expect(resetText('soon', NOW, true, ZONE)).toBe('')
  expect(resetText('2026-10-17T22:05:00Z', NOW, false, ZONE)).toBe(' ↻00:05')
  expect(resetText('2026-10-18T22:05:00Z', NOW, true, 'UTC')).toBe(' ↻Sun 22:05')
})

test('the weekly reset names its weekday only when it is not later today', () => {
  // NOW is Sat 17 Oct 08:00 in Berlin
  expect(resetText('2026-10-17T13:00:00Z', NOW, true, ZONE)).toBe(' ↻15:00')
  expect(resetText('2026-10-17T22:30:00Z', NOW, true, ZONE)).toBe(' ↻Sun 00:30')
  expect(resetText('2026-10-19T13:00:00Z', NOW, true, ZONE)).toBe(' ↻Mon 15:00')
})

test('other windows than five hours and a week are left out', () => {
  expect(parts({ startedAt: 0, context: { window: 0 }, rateLimits: [{ kind: 'spend_limit', percentUsed: 99 }] },
               DEFAULTS, null, NOW, ZONE)).toEqual([])
})

// --- A gate that modules of config.json takes the teeth from, as hooks/statusline.py marks it ---------------------

const FOLDER = 'C:/Users/you/.claude/hooks/usage-clock'
const gateSettings = (...entries: [string, string][]) => ({
  statusLine: { type: 'command', command: `"C:/Python314/python.exe" -X utf8 "${FOLDER}/statusline.py"` },
  hooks: {
    PreToolUse: [{
      matcher: '*',
      hooks: entries.map(([script, options]) => ({ type: 'command', command: `"C:/Python314/python.exe" -X utf8 "${FOLDER}/${script}" ${options} || true` })),
    }],
  },
})
const gates = (modules: object) => JSON.stringify({ modules })

// The cases tests/test_statusline.py runs against hooks/statusline.py as well: both sides give the same markers
for (const [i, { modules, entries, markers }] of GATE_MARKER_CASES.entries()) {
  test(`case ${i + 1} of the shared fixture gives its markers word for word: ${JSON.stringify(modules)}`, () => {
    expect(gateMarkers(gates(modules), gateSettings(...(entries as [string, string][])))).toEqual(markers)
  })
}

test('a gate installed elsewhere, or a missing or broken file, gives no marker', () => {
  const elsewhere = { ...gateSettings(), hooks: { PreToolUse: [{ hooks: [{ command: 'python3 /other/spawn_gate.py --mode=enforce || true' }] }] } }
  expect(gateMarkers(gates({ spawn_gate: 'off' }), elsewhere)).toEqual([])
  expect(gateMarkers(undefined, gateSettings(['spawn_gate.py', '--mode=enforce']))).toEqual([])
  expect(gateMarkers('{broken', gateSettings(['spawn_gate.py', '--mode=enforce']))).toEqual([])
  expect(gateMarkers(gates({ spawn_gate: 'off' }), { hooks: { PreToolUse: 'x' } })).toEqual([])
  expect(gateMarkers(gates({ spawn_gate: 'off' }), null)).toEqual([])
})

test('the path of the entry counts in any spelling of Windows, and the last --mode', () => {
  const settings = { ...gateSettings(), hooks: { PreToolUse: [{ hooks: [{ command: `python 'c:\\users\\you\\.claude\\hooks\\usage-clock\\spawn_gate.py' --mode=enforce --mode=shadow` }] }] } }
  expect(gateMarkers(gates({ spawn_gate: 'off' }), settings)).toEqual(['spawn gate off'])
  expect(gateMarkers(gates({ spawn_gate: 'shadow' }), settings)).toEqual([])
})

test('the markers follow the week in red, attached to it', () => {
  const texts = parts(usage, DEFAULTS, null, NOW, ZONE, ['spawn gate off']).map(part => [part.text, part.color, part.attached])
  expect(texts.slice(1, 4)).toEqual([['week 76% ↻Mon 15:00', 'error', undefined], ['<- week gate: agents warn', 'error', true],
    ['spawn gate off', 'error', true]])
})
