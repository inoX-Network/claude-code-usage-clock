import type { On } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'

import { REPORT, SYNCED } from './report-fixture'

const COMMAND = '"C:/Python314/python.exe" -X utf8 "C:/Users/you/.claude/hooks/usage-clock/statusline.py"'
const SCRIPT = 'C:/Users/you/.claude/hooks/usage-clock/usage_report.py'
const INTERPRETER = ['C:/Python314/python.exe', '-X', 'utf8', SCRIPT]
const SURFACES = ['terminal', 'desktop'] as const

type Surface = (typeof SURFACES)[number]
type Run = { argv: readonly string[]; timeoutMs?: number }
type Outcome = { exitCode: number; stdout: string; stderr: string }

/** What the report prints: the fixed example, titled with the flag it was asked for, so a test sees which period it is. */
function printed(argv: readonly string[]): Outcome {
  const flag = argv[argv.length - 2] ?? ''
  const report = { ...REPORT, overview: { ...REPORT.overview, title: `Report ${flag}` } }
  return { exitCode: 0, stdout: JSON.stringify(report), stderr: '' }
}

function world(on: On, options: {
  statusLine?: unknown
  isInstalled?: boolean
  run?: (argv: readonly string[], count: number) => Outcome | Promise<Outcome>
  denyRun?: string
  own?: string
  project?: Record<string, unknown>
} = {}) {
  const seen = { runs: [] as Run[], opened: [] as unknown[], registered: [] as unknown[], focused: [] as unknown[] }
  mock.clock(on, { now: Date.parse('2026-10-18T06:00:00Z') })
  mock.env(on, { USERPROFILE: 'C:\\Users\\you' })
  on('session.usage', () => ({ value: { startedAt: 0, context: { window: 1_000_000 }, rateLimits: [] } }))
  on('session.start', ($, e) => e)
  const own = options.own
  if (own !== undefined) on('session.id', () => ({ value: own }))
  on('fs.read', () => ({ deny: 'ENOENT' }))
  on('fs.exists', () => ({ value: options.isInstalled ?? true }))
  // The user's settings.json alone, or merged with what the project's settings set
  const user = options.statusLine === null ? {} : { statusLine: options.statusLine ?? { type: 'command', command: COMMAND } }
  on('settings.read', ($, e) => ({ value: e.source === 'user' ? user : { ...user, ...options.project } }))
  on('command.register', ($, e) => {
    seen.registered.push(e)
    return { value: { command: e.name } }
  })
  // The engine's move of the focus ring, beneath the plugin's ui.focus hook
  on('ui.focus', ($, e) => {
    seen.focused.push(e.element)
    return {}
  })
  on('ui.open', ($, e) => {
    seen.opened.push(e)
    return { value: { isPlaced: true } }
  })
  on('process.run', async ($, e) => {
    seen.runs.push({ argv: e.argv, timeoutMs: e.init?.timeoutMs })
    if (options.denyRun !== undefined) return { deny: options.denyRun }
    const outcome = await (options.run ?? printed)(e.argv, seen.runs.length)
    return { value: { ...outcome, isStdoutTruncated: false, isStderrTruncated: false } }
  })
  return seen
}

/** `/report <args>` typed at the prompt of a fullscreen terminal. */
function report($: Engine, args = '') {
  return $.command.run({ command: 'report', args, origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 120 } })
}

async function start($: Engine) {
  await $.session.start({ cwd: 'C:/proj', surface: 'terminal', isInteractive: true })
}

async function mountPane($: Engine, surface: Surface, bodyColumns = 100) {
  return $.ui.mount({
    plugin: 'usage-clock-display',
    surface,
    component: 'Pane',
    requestId: 'usage-report',
    props: { title: 'Usage report', isFocused: true, bodyColumns, placement: 'dock', scroll: { offset: 0, bodyRows: 30 }, view: {} },
  })
}

type Pane = Awaited<ReturnType<typeof mountPane>>

/** The props of the Text a tab Button holds as its label: the highlight lives there, not on the Button. */
async function labelOf(ui: Pane, key: string): Promise<Record<string, unknown>> {
  const label = (await ui.find({ key }))?.children.find(child => typeof child === 'object' && child !== null)
  return (label as { props?: Record<string, unknown> } | undefined)?.props ?? {}
}

async function texts(ui: Pane): Promise<string> {
  return (await ui.findAll({ type: 'Text' })).map(found => found.text).join('\n')
}

test('the plugin registers /report with the periods as the argument hint', async ($, on) => {
  const seen = world(on)
  await start($)
  expect(seen.registered).toEqual([
    expect.objectContaining({ name: 'report', argumentHint: '[day|week|month|year]' }),
  ])
  expect((seen.registered[0] as { description: string }).description).toMatch(/usage report/i)
})

test('the command answers without text and without context, so nothing of it reaches the model', async ($, on) => {
  const seen = world(on)
  await start($)
  const result = await report($)
  expect(result.text).toBeUndefined()
  expect(result.context).toBeUndefined()
  expect(seen.opened).toEqual([{ id: 'usage-report', title: 'Usage report', focus: true, closeOnEscape: true }])
})

for (const [args, flag] of [
  ['', '--week'],
  ['month', '--month'],
  ['DAY', '--day'],
  ['year', '--year'],
  ['nonsense', '--week'],
] as const) {
  test(`/report ${args === '' ? '(no argument)' : args} reads ${flag} from the report next to the status line`, async ($, on) => {
    const seen = world(on)
    await start($)
    await report($, args)
    expect(seen.runs).toEqual([{ argv: [...INTERPRETER, flag, '--json'], timeoutMs: 30_000 }])
  })
}

for (const surface of SURFACES) {
  test(`${surface}: the tabs are buttons with the hotkeys 1-4, a-h and r`, async ($, on) => {
    world(on)
    await start($)
    await report($)
    const ui = await mountPane($, surface)
    const buttons = await ui.findAll({ type: 'Button' })
    // The desktop writes the key of the open view and period into its label instead (see the label test)
    const open = surface === 'desktop' ? undefined : true
    expect(Object.fromEntries(buttons.map(button => [button.key, button.props.hotkey]))).toEqual({
      'period-day': '1', 'period-week': open && '2', 'period-month': '3', 'period-year': '4',
      'view-overview': open && 'a', 'view-agents': 'b', 'view-models': 'c', 'view-cache': 'd', 'view-tokens': 'e', 'view-projects': 'f',
      'view-sessions': 'g', 'view-settings': 'h',
      refresh: 'r',
    })
    await ui.unmount()
  })

  test(`${surface}: the labels are the names alone, the surface shows the key`, async ($, on) => {
    world(on)
    await start($)
    await report($)
    const ui = await mountPane($, surface)
    // Every surface shows a plain Button's hotkey itself: the terminal as `d: `, the desktop beside the label. On the
    // desktop the open view and period carry their key in the label, in the colour of the text, and no hotkey
    const open = surface === 'desktop' ? ['2  Week', 'A  Overview'] : ['Week', 'Overview']
    expect((await ui.find({ key: 'period-week' }))?.text.trim()).toBe(open[0])
    expect((await ui.find({ key: 'view-overview' }))?.text.trim()).toBe(open[1])
    expect((await ui.find({ key: 'view-cache' }))?.text.trim()).toBe('Cache/Thinking')
    expect((await ui.find({ key: 'refresh' }))?.text.trim()).toBe('Refresh')
    // Another view chosen: the key moves back to the app's key box, the new one takes it into its label
    await ui.press({ key: 'view-cache' })
    expect((await ui.find({ key: 'view-overview' }))?.props.hotkey).toBe('a')
    expect((await ui.find({ key: 'view-overview' }))?.text.trim()).toBe('Overview')
    expect((await ui.find({ key: 'view-cache' }))?.text.trim()).toBe(surface === 'desktop' ? 'D  Cache/Thinking' : 'Cache/Thinking')
    await ui.unmount()
  })

  test(`${surface}: the open view is a highlighted tab, the period in the accent colour, the others plain or dim`, async ($, on) => {
    const seen = world(on)
    await start($)
    await report($)
    const ui = await mountPane($, surface)
    const look = async (key: string) => {
      const props = await labelOf(ui, key)
      return { background: props.backgroundColor, color: props.color, isDim: props.dimColor === true }
    }
    const tab = { background: 'claude', color: 'inverseText', isDim: false }
    const plain = { background: undefined, color: undefined, isDim: false }
    const period = { background: undefined, color: 'claude', isDim: false }
    const dim = { background: undefined, color: undefined, isDim: true }
    expect(await look('view-overview')).toEqual(tab)
    expect(await look('view-agents')).toEqual(plain)
    expect(await look('period-week')).toEqual(period)
    expect(await look('period-day')).toEqual(dim)
    await ui.press({ key: 'view-agents' })
    await ui.press({ key: 'period-year' })
    expect(await look('view-agents')).toEqual(tab)
    expect(await look('view-overview')).toEqual(plain)
    expect(await look('period-year')).toEqual(period)
    expect(await look('period-week')).toEqual(dim)
    // The engine draws the focus ring as a white key; the plugin leaves it where the person put it, or nowhere
    expect((await ui.find({ key: 'view-agents' }))?.props.autoFocus).toBeUndefined()
    expect((await ui.find({ key: 'view-overview' }))?.props.autoFocus).toBeUndefined()
    expect(seen.focused).toEqual([])
    await ui.unmount()
  })

  test(`${surface}: Sessions lists the sessions; one opens its detail with the recorded context, q goes back`, async ($, on) => {
    const seen = world(on)
    await start($)
    await report($)
    const ui = await mountPane($, surface)
    await ui.press({ key: 'view-sessions' })
    if (surface === 'terminal') {
      expect(await texts(ui)).toMatch(/session\s+started\s+USD\s+~week pts\s+requests/)
      expect((await ui.find({ key: 'session-aaaaaaaa' }))?.text).toMatch(/webshop aaaaaaaa\s+\S+ 13 Oct \d\d:\d\d\s+60\s+20\.0\s+300/)
    } else {
      // A card per session: the button with name and cost, the start and the counts under it
      expect(await texts(ui)).toMatch(/SESSIONS · HIGHEST COST FIRST/)
      expect((await ui.find({ key: 'session-aaaaaaaa' }))?.text).toMatch(/webshop aaaaaaaa\s+60 USD/)
      expect((await ui.find({ key: 'card-aaaaaaaa' }))?.text).toMatch(/13 Oct \d\d:\d\d · 300 requests · cache 90 % · week: ~20\.0 pts/)
    }
    expect(await ui.find({ key: 'session-bbbbbbbb' })).toBeDefined()

    await ui.press({ key: 'session-aaaaaaaa' })
    const detail = await texts(ui)
    for (const pattern of [
      /webshop aaaaaaaa · .* · 60 USD · week: ~20\.0 pts · 5h: ~8\.0 pts/,
      /Context after the first answer: 63,000 tokens of 1,000,000 \(6 %\)/,
      /MCP tools\s+12,400/,
      /context7 \(2 tools\)\s+1,500/,
      /playwright navigate\s+450/,
      /~\/\.claude\/CLAUDE\.md\s+2,200/,
      /requests of the session before or after it are not counted/,
      /Skills: 5,600 tokens, 38 of 40 listed/,
      /arbeiter-bauen\s+120/,
    ]) expect(detail).toMatch(pattern)
    expect(detail).not.toMatch(/Free space/)
    expect(await ui.find({ key: 'back' })).toBeDefined()

    await ui.press({ key: 'back' })
    expect(await ui.find({ key: 'session-bbbbbbbb' })).toBeDefined()
    await ui.press({ key: 'session-bbbbbbbb' })
    expect(await texts(ui)).toMatch(/No context recorded for this session/)

    await ui.press({ key: 'period-month' })
    expect(await ui.find({ key: 'back' })).toBeUndefined()
    expect(seen.runs).toHaveLength(2)
    await ui.unmount()
  })

  test(`${surface}: Sessions marks the session the pane was opened from, in the list and in its detail`, async ($, on) => {
    // The session id is a whole UUID, the report knows only its first 8 characters
    world(on, { own: 'bbbbbbbb-1c2d-4e5f-8a9b-0c1d2e3f4a5b' })
    await start($)
    await report($)
    const ui = await mountPane($, surface)
    await ui.press({ key: 'view-sessions' })
    if (surface === 'terminal') {
      expect(await texts(ui)).toMatch(/  session\s+started/)
      expect((await ui.find({ key: 'session-bbbbbbbb' }))?.text).toMatch(/^▶ blog bbbbbbbb/)
      expect((await ui.find({ key: 'session-aaaaaaaa' }))?.text).toMatch(/^  webshop aaaaaaaa/)
      // The mark is a piece of its own in the accent colour; the row around it has none
      const marks = (await ui.findAll({ type: 'Text' })).filter(found => found.text.trim() === '▶')
      expect(marks.map(found => found.props.color)).toEqual(['claude'])
    } else {
      // The own session stands first, under a label of its own, its card toned and framed in the accent colour
      expect(await texts(ui)).toMatch(/THIS SESSION\nblog bbbbbbbb[\s\S]*LIVE[\s\S]*ALL SESSIONS[\s\S]*webshop aaaaaaaa/)
      const own = await ui.find({ key: 'card-bbbbbbbb' })
      expect([own?.props.backgroundColor, own?.props.borderColor]).toEqual(['#2b1f14', '#c47a3a'])
      const other = await ui.find({ key: 'card-aaaaaaaa' })
      expect([other?.props.backgroundColor, other?.props.borderColor]).toEqual(['#1c1914', '#2a241d'])
      expect(await texts(ui)).not.toContain('▶')
    }
    await ui.press({ key: 'session-bbbbbbbb' })
    expect(await texts(ui)).toMatch(/blog bbbbbbbb · this session · /)
    await ui.unmount()
  })

  test(`${surface}: with the sync Sessions names the machine of each session and marks only one of this machine`, async ($, on) => {
    // The laptop session has the same first characters as the session the pane was opened from: it is not this one
    world(on, { own: 'aaaaaaaa-1c2d-4e5f-8a9b-0c1d2e3f4a5b', run: () => ({ exitCode: 0, stdout: JSON.stringify(SYNCED), stderr: '' }) })
    await start($)
    await report($)
    const ui = await mountPane($, surface, 120)
    await ui.press({ key: 'view-sessions' })
    if (surface === 'terminal') {
      expect(await texts(ui)).toMatch(/session\s+started\s+USD\s+~week pts\s+requests\s+machine/)
      expect((await ui.find({ key: 'session-aaaaaaaa' }))?.text).toMatch(/^webshop aaaaaaaa\s.*\s300\s+laptop$/)
      expect((await ui.find({ key: 'session-bbbbbbbb' }))?.text).toMatch(/^blog bbbbbbbb\s.*\s40\s+desk$/)
      expect(await texts(ui)).not.toContain('▶')
    } else {
      expect((await ui.find({ key: 'card-aaaaaaaa' }))?.text).toMatch(/on laptop · \S+ 13 Oct \d\d:\d\d · 300 requests/)
      expect((await ui.find({ key: 'card-bbbbbbbb' }))?.text).toMatch(/on desk · /)
      expect(await texts(ui)).not.toMatch(/THIS SESSION|LIVE/)
    }
    await ui.press({ key: 'session-aaaaaaaa' })
    expect(await texts(ui)).toMatch(/webshop aaaaaaaa · on laptop · /)
    await ui.unmount()
  })

  test(`${surface}: Tab landing on a view opens it; landing on a period reads nothing`, async ($, on) => {
    const seen = world(on)
    await start($)
    await report($)
    const ui = await mountPane($, surface)
    const tab = (element: string) =>
      $.ui.focus({ component: 'Pane', requestId: 'usage-report', element, origin: { kind: 'person' } })
    const agents = surface === 'terminal' ? /agent\s+USD\s+requests/ : /BY AGENT · USD · REQUESTS/
    await tab('view-agents')
    expect(await texts(ui)).toMatch(agents)
    await tab('period-year')
    expect(await texts(ui)).toMatch(agents)
    expect(seen.runs).toHaveLength(1)
    await ui.unmount()
  })

  test(`${surface}: a period is read once; switching back shows what was read, refresh reads again`, async ($, on) => {
    const seen = world(on)
    await start($)
    await report($)
    const ui = await mountPane($, surface)
    expect(await texts(ui)).toContain('Report --week')
    expect(seen.runs).toHaveLength(1)

    await ui.press({ key: 'period-month' })
    expect(seen.runs).toHaveLength(2)
    expect(seen.runs[1]?.argv.slice(-2)).toEqual(['--month', '--json'])
    expect(await texts(ui)).toContain('Report --month')

    await ui.press({ key: 'period-week' })
    expect(seen.runs).toHaveLength(2)
    expect(await texts(ui)).toContain('Report --week')

    await ui.press({ key: 'view-models' })
    await ui.press({ key: 'period-month' })
    expect(seen.runs).toHaveLength(2)

    await ui.press({ key: 'refresh' })
    expect(seen.runs).toHaveLength(3)
    expect(seen.runs[2]?.argv.slice(-2)).toEqual(['--month', '--json'])
    await ui.unmount()
  })

  test(`${surface}: each view draws its fields from the report`, async ($, on) => {
    const seen = world(on)
    await start($)
    await report($)
    const ui = await mountPane($, surface)
    const views: [string, RegExp[]][] = surface === 'desktop' ? [
      ['view-overview', [/WEEKLY LIMIT\n\+39\.0 pts in this period/, /webshop\n60 · 20\.0/, /main 75 · agents 25/, /5H  one 5h window reached 82 %/]],
      ['view-agents', [/MAIN\n75 USD[\s\S]*300 requests/, /SUBAGENTS\n25 USD[\s\S]*2 types/, /stripe-meister\n20 · 40 · 6\.6/, /reviewer\n5\.0 · 160 · <1/]],
      ['view-models', [/OPUS 5\.5\n92 USD[\s\S]*97 requests\nthinking 30 %\ncache hits 90 %/, /MYSTERY 1\n- USD/, /without a price has no USD/, /SHARE OF THE COST/]],
      ['view-cache', [/CACHE HIT RATE\n89 %/, /COLD STARTS\n3/, /THINKING TOKENS\n123k[\s\S]*123,000/, /THINKING SHARE\n25 % of output/]],
      ['view-tokens', [/CACHE READ\n78\.9M tokens[\s\S]*78,900,000/, /OUTPUT\n123k tokens/, /TOKENS BY KIND\ncache read\n78\.9M/]],
      ['view-projects', [/PROJECTS · USD · SHARE/, /blog\n9\.0 · 9 % · 3\.0 · 2/, /scratch\n0\.40 · <1 % · <1 · 2/]],
    ] : [
      ['view-overview', [/39 % of the weekly limit used \(\+39 points\)/, /webshop\s+60\s+60 %\s+20\.0\s+1/, /Main 75 USD · subagents 25 USD/]],
      ['view-agents', [/agent\s+USD\s+requests\s+~week pts\s+~5h pts/, /stripe-meister\s+20\s+40\s+6\.6\s+2\.0/, /main\s+75\s+300\s+30\.5\s+12\.5/]],
      ['view-models', [/Opus 5\.5\s+92\s+97\s+30 %\s+90 %/, /Mystery 1\s+-\s+3/]],
      ['view-cache', [/Cache hit rate\s+89 %/, /Cold starts\s+3/, /Thinking tokens\s+123,000/, /Thinking share\s+25 % of output/]],
      ['view-tokens', [/input\s+1,200/, /cache write 5m\s+3,400/, /cache write 1h\s+5,600,000/, /cache read\s+78,900,000/, /output\s+123,456/]],
      ['view-projects', [/project\s+USD\s+share\s+~week pts\s+sessions/, /blog\s+9\.0\s+9 %\s+3\.0\s+2/, /scratch\s+0\.40\s+<1 %\s+<1\s+2/]],
    ]
    for (const [key, patterns] of views) {
      await ui.press({ key })
      const shown = await texts(ui)
      for (const pattern of patterns) expect(shown).toMatch(pattern)
    }
    expect(seen.runs).toHaveLength(1)
    await ui.unmount()
  })

  test(`${surface}: while the report is read the pane says so`, async ($, on) => {
    let release: () => void = () => {}
    const gate = new Promise<void>(resolve => { release = resolve })
    world(on, { run: async argv => { await gate; return printed(argv) } })
    await start($)
    const running = report($)
    const ui = await mountPane($, surface)
    expect(await texts(ui)).toContain('loading…')
    release()
    await running
    expect(await texts(ui)).toContain('Report --week')
    expect(await texts(ui)).not.toContain('loading…')
    await ui.unmount()
  })

  for (const [name, outcome, shows] of [
    ['an exit code other than 0 gives the code and the first line of stderr',
      { exitCode: 2, stdout: '', stderr: 'usage_report.py: error: unknown flag\nsecond line' }, /exit code 2.*unknown flag/],
    ['output that is no JSON gives a line', { exitCode: 0, stdout: 'Traceback (most recent call last):', stderr: '' }, /not valid JSON/],
    ['JSON that is no report gives a line', { exitCode: 0, stdout: '{"a": 1}', stderr: '' }, /not a usage report/],
  ] as const) {
    test(`${surface}: ${name}`, async ($, on) => {
      world(on, { run: () => outcome })
      await start($)
      await report($)
      const ui = await mountPane($, surface)
      const line = await ui.find({ type: 'Text', text: shows })
      expect(line).toBeDefined()
      expect(line?.props.color).toBe('error')
      expect(await texts(ui)).not.toContain('loading…')
      await ui.unmount()
    })
  }

  test(`${surface}: after an error, refresh reads again`, async ($, on) => {
    world(on, {
      run: (argv, count) => (count === 1 ? { exitCode: 1, stdout: '', stderr: 'disk full' } : printed(argv)),
    })
    await start($)
    await report($)
    const ui = await mountPane($, surface)
    expect(await texts(ui)).toMatch(/disk full/)
    expect(await texts(ui)).toMatch(/r to try again/)
    await ui.press({ key: 'refresh' })
    expect(await texts(ui)).toContain('Report --week')
    expect(await texts(ui)).not.toMatch(/disk full/)
    await ui.unmount()
  })

  test(`${surface}: a command that cannot start gives a line`, async ($, on) => {
    world(on, { denyRun: 'python.exe not found' })
    await start($)
    await report($)
    const ui = await mountPane($, surface)
    expect(await ui.find({ type: 'Text', text: /Could not start the report/ })).toBeDefined()
    await ui.unmount()
  })

  for (const [name, statusLine] of [
    ['no status line', null],
    ['another status line', { type: 'command', command: 'npx ccusage statusline' }],
    ['shell syntax in the status line', { type: 'command', command: `${COMMAND} | tee out.txt` }],
    ['two status line scripts', { type: 'command', command: `${COMMAND} /other/statusline.py` }],
  ] as const) {
    test(`${surface}: ${name}: no process is started, the pane points to install.py`, async ($, on) => {
      const seen = world(on, { statusLine })
      await start($)
      await report($)
      const ui = await mountPane($, surface)
      expect(seen.runs).toEqual([])
      expect(await ui.find({ type: 'Text', text: /status line.*install\.py/ })).toBeDefined()
      await ui.unmount()
    })
  }

  test(`${surface}: a status line only in the settings of the project: no process, the pane points to install.py`, async ($, on) => {
    const seen = world(on, { statusLine: null, project: { statusLine: { type: 'command', command: COMMAND } } })
    await start($)
    await report($)
    const ui = await mountPane($, surface)
    expect(seen.runs).toEqual([])
    expect(await ui.find({ type: 'Text', text: /status line.*install\.py/ })).toBeDefined()
    await ui.unmount()
  })

  test(`${surface}: a report script that is not installed: no process, the pane points to install.py`, async ($, on) => {
    const seen = world(on, { isInstalled: false })
    await start($)
    await report($)
    const ui = await mountPane($, surface)
    expect(seen.runs).toEqual([])
    const line = await ui.find({ type: 'Text', text: /not installed.*install\.py/ })
    expect(line?.text).toContain(SCRIPT)
    await ui.unmount()
  })
}

for (const [bodyColumns, width] of [[30, '100%'], [60, '49%'], [120, '24%']] as const) {
  test(`desktop: a pane ${bodyColumns} cells wide puts the key figures ${width} wide, the gauge an Svg`, async ($, on) => {
    world(on)
    await start($)
    await report($)
    const ui = await mountPane($, 'desktop', bodyColumns)
    for (const id of ['week', 'five', 'cost', 'cache']) expect((await ui.find({ key: `kpi-${id}` }))?.props.width).toBe(width)
    const drawings = await ui.findAll({ type: 'Svg' })
    expect(drawings.map(found => found.props.alt)).toEqual(['39 % of the weekly limit used', 'Peaks of the 5h windows: 82, 20 %, scale to 90 %'])
    await ui.unmount()
  })
}

test('desktop: the open view and period sit on a plate of the accent colour, no primary (white) button; the plate moves along', async ($, on) => {
  world(on)
  await start($)
  await report($)
  const ui = await mountPane($, 'desktop')
  const look = async (key: string) => {
    const button = await ui.find({ key })
    const frame = await ui.find({ key: `tab-${key}` })
    return { variant: button?.props.variant, plain: button?.props.plain, plate: frame?.props.backgroundColor }
  }
  const active = { variant: undefined, plain: true, plate: '#c47a3a' }
  const other = { variant: undefined, plain: true, plate: undefined }
  expect(await look('view-overview')).toEqual(active)
  expect(await look('period-week')).toEqual(active)
  expect(await look('view-tokens')).toEqual(other)
  expect(await look('period-year')).toEqual(other)
  await ui.press({ key: 'view-tokens' })
  await ui.press({ key: 'period-year' })
  expect(await look('view-tokens')).toEqual(active)
  expect(await look('period-year')).toEqual(active)
  expect(await look('view-overview')).toEqual(other)
  expect(await look('period-week')).toEqual(other)
  await ui.unmount()
})

test('desktop: a bar of 0 is an empty track, no stub', async ($, on) => {
  const zero = { ...REPORT, total: { ...REPORT.total, tokens: { ...REPORT.total.tokens, cache_write_5m: 0 } } }
  world(on, { run: () => ({ exitCode: 0, stdout: JSON.stringify(zero), stderr: '' }) })
  await start($)
  await report($)
  const ui = await mountPane($, 'desktop')
  await ui.press({ key: 'view-tokens' })
  // Sorted by count, the 0 comes last: its track holds nothing, the one before it a filled part
  expect((await ui.find({ key: 'kinds-bar-4' }))?.children.filter(child => child !== null && child !== undefined)).toHaveLength(0)
  expect((await ui.find({ key: 'kinds-bar-3' }))?.children.filter(child => child !== null && child !== undefined)).toHaveLength(1)
  await ui.unmount()
})

test('terminal: the tabs stay plain buttons without a frame', async ($, on) => {
  world(on)
  await start($)
  await report($)
  const ui = await mountPane($, 'terminal')
  expect((await ui.find({ key: 'view-overview' }))?.props.plain).toBe(true)
  expect((await ui.find({ key: 'view-overview' }))?.props.variant).toBeUndefined()
  expect(await ui.find({ key: 'tab-view-overview' })).toBeUndefined()
  await ui.unmount()
})

test('terminal: no tiles, the overview stays text', async ($, on) => {
  world(on)
  await start($)
  await report($)
  const ui = await mountPane($, 'terminal')
  expect(await ui.find({ key: 'kpi-week' })).toBeUndefined()
  expect(await texts(ui)).toMatch(/39 % of the weekly limit used/)
  await ui.unmount()
})

test('/report again starts fresh: it reads again and shows the overview', async ($, on) => {
  const seen = world(on)
  await start($)
  await report($)
  const ui = await mountPane($, 'terminal')
  await ui.press({ key: 'view-agents' })
  await report($, 'week')
  expect(seen.runs).toHaveLength(2)
  expect((await labelOf(ui, 'view-overview')).backgroundColor).toBe('claude')
  expect((await labelOf(ui, 'view-agents')).backgroundColor).toBeUndefined()
  await ui.unmount()
})
