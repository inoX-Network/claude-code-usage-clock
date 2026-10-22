import type { On } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'

import { localTime } from './report'
import { REPORT } from './report-fixture'
import { INVALID_REPO, UPDATE_LINE, parseAnswer, validRepo } from './settings'
import { LOOK } from './tiles'

const COMMAND = '"C:/Python314/python.exe" -X utf8 "C:/Users/you/.claude/hooks/usage-clock/statusline.py"'
const CLI = 'C:/Users/you/.claude/hooks/usage-clock/settings_cli.py'
const PREFIX = ['C:/Python314/python.exe', '-X', 'utf8', CLI]
const SURFACES = ['terminal', 'desktop'] as const
const SPECIAL = `o-x/na'me; $(touch x) "q" \`y\``

type Surface = (typeof SURFACES)[number]
type Outcome = { exitCode: number; stdout: string; stderr: string }

const part = (id: string, label: string, installed: boolean, key: string | null, value: boolean | string | null,
  choices: string[], extra: Record<string, unknown> = {}) => ({
  id, label, installed, key, value, choices, summary: `What ${label} does.`, info: `  About ${label}.\n  More: https://x/docs#${id}`,
  tab: `In the tab ${label} works so.`, ...extra,
})

/** What `settings_cli.py show --json` answers: the usage clock and the spawn gate installed, the soft stop not. */
function shown(values: Record<string, unknown> = {}, sync: Record<string, unknown> = {}, info?: string) {
  return {
    modules: [
      part('statusline', 'Status line', true, null, null, []),
      part('desktop_line', 'Desktop line', true, 'modules.desktop_line', values.desktop_line ?? 'full', ['off', 'short', 'full']),
      part('usage_clock', 'Usage clock', true, 'modules.usage_clock', values.usage_clock ?? true, ['true', 'false']),
      part('context_reporter', 'Context reporter', true, 'modules.context_reporter', true, ['true', 'false']),
      part('spawn_gate', 'Spawn gate', true, 'modules.spawn_gate', values.spawn_gate ?? '', ['off', 'shadow', 'enforce'],
        { installed_mode: 'shadow', ...(info === undefined ? {} : { info }) }),
      part('soft_stop', 'Soft stop', false, 'modules.soft_stop', '', ['off', 'shadow', 'enforce'], { installed_mode: null }),
    ],
    sync: {
      installed: true, repo: 'anna/usage-sync', mode: values.mode ?? 'shadow', choices: ['off', 'shadow', 'on'], summary: 'What the sync does.',
      last_success: null, last_error: null, info: '  About the sync.', tab: 'Sync in the tab.', repo_info: '  About the repo.',
      repo_tab: 'Check and Save.', ...sync,
    },
    problems: [],
  }
}

/** Holds every show --json from the `from`-th on: `asked` is called when one comes, it answers once `until` settles. */
type Hold = { from: number; asked: () => void; until: Promise<void> }

function world(on: On, options: { hasCli?: boolean; check?: string; save?: string; sync?: Record<string, unknown>; info?: string; hold?: Hold } = {}) {
  const seen = { runs: [] as (readonly string[])[], values: {} as Record<string, unknown>, asked: [] as string[], shows: 0 }
  mock.clock(on, { now: Date.parse('2026-10-18T06:00:00Z') })
  mock.env(on, { USERPROFILE: 'C:\\Users\\you' })
  on('session.usage', () => ({ value: { startedAt: 0, context: { window: 1_000_000 }, rateLimits: [] } }))
  on('session.start', ($, e) => e)
  on('fs.read', () => ({ deny: 'ENOENT' }))
  // By the file name: the engine may hand the hook the path resolved, not as the plugin wrote it
  on('fs.exists', ($, e) => {
    seen.asked.push(e.path)
    return { value: /[\\/]settings_cli\.py$/.test(e.path) ? (options.hasCli ?? true) : true }
  })
  on('settings.read', () => ({ value: { statusLine: { type: 'command', command: COMMAND } } }))
  on('command.register', ($, e) => ({ value: { command: e.name } }))
  on('ui.focus', () => ({}))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('process.run', async ($, e) => {
    seen.runs.push(e.argv)
    let outcome: Outcome
    if (e.argv[3] !== CLI) {
      outcome = { exitCode: 0, stdout: JSON.stringify(REPORT), stderr: '' }
    } else if (e.argv[4] === 'show') {
      seen.shows += 1
      if (options.hold !== undefined && seen.shows >= options.hold.from) {
        options.hold.asked()
        await options.hold.until
      }
      outcome = { exitCode: 0, stdout: JSON.stringify(shown(seen.values, options.sync, options.info)), stderr: '' }
    } else if (e.argv[4] === 'set') {
      const key = String(e.argv[5]).split('.')[1] as string
      seen.values[key] = e.argv[6] === 'true' ? true : e.argv[6] === 'false' ? false : e.argv[6]
      outcome = { exitCode: 0, stdout: JSON.stringify({ key: e.argv[5], value: seen.values[key], changed: true, backup: null }), stderr: '' }
    } else if (e.argv[4] === 'check-repo') {
      const result = options.check ?? 'private_ok'
      outcome = { exitCode: result === 'private_ok' ? 0 : 1, stdout: JSON.stringify({ result, repo: e.argv.at(-1), text: `Result ${result}.` }), stderr: '' }
    } else {
      const result = options.save ?? 'saved'
      const text = result === 'saved' ? 'Sync: cloned.' : 'Move that folder away, do not delete it.'
      outcome = { exitCode: result === 'saved' ? 0 : 1, stdout: JSON.stringify({ result, repo: e.argv.at(-1), text }), stderr: '' }
    }
    return { value: { ...outcome, isStdoutTruncated: false, isStderrTruncated: false } }
  })
  return seen
}

async function mount($: Engine, surface: Surface, bodyColumns = 100) {
  await $.session.start({ cwd: 'C:/proj', surface: 'terminal', isInteractive: true })
  await $.command.run({ command: 'report', args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 120 } })
  return $.ui.mount({
    plugin: 'usage-clock-display',
    surface,
    component: 'Pane',
    requestId: 'usage-report',
    props: { title: 'Usage report', isFocused: true, bodyColumns, placement: 'dock', scroll: { offset: 0, bodyRows: 30 }, view: {} },
  })
}

async function open($: Engine, surface: Surface, bodyColumns = 100) {
  const ui = await mount($, surface, bodyColumns)
  await ui.press({ key: 'view-settings' })
  return ui
}

const settingsRuns = (seen: { runs: (readonly string[])[] }) => seen.runs.filter(argv => argv[3] === CLI)

type Pane = Awaited<ReturnType<typeof open>>

/** The text of a row of the tab (a Box keyed row-<id>), or undefined if there is none. */
async function rowText(ui: Pane, id: string): Promise<string | undefined> {
  return (await ui.find({ key: `row-${id}` }))?.text
}

/** The props of the Text a Button holds as its label: the mark of the active choice lives there. */
async function labelOf(ui: Pane, key: string): Promise<Record<string, unknown>> {
  const label = (await ui.find({ key }))?.children.find(child => typeof child === 'object' && child !== null)
  return (label as { props?: Record<string, unknown> } | undefined)?.props ?? {}
}

/** The Text whose whole text is `text` (the engine finds a Text by its text, not by its key). */
async function exact(ui: Pane, text: string) {
  const escaped = text.replace(/[.*+?^$(){}|[\]\\]/g, '\\$&')
  return ui.find({ type: 'Text', text: new RegExp('^' + escaped + '$') })
}

async function hasText(ui: Pane, text: string): Promise<boolean> {
  return (await ui.find({ type: 'Text', text })) !== undefined
}

/** Check as the surface offers it: the terminal's own button, the desktop's button of the field, which submits it. */
async function check(ui: Pane, surface: Surface) {
  if (surface === 'terminal') {
    await ui.press({ key: 'check' })
  } else {
    await ui.input({ key: 'repo', text: String((await ui.find({ key: 'repo' }))?.props.value) })
  }
}

/** The keys of the Buttons anywhere below an element as drawn. */
function buttonsIn(node: unknown): string[] {
  if (typeof node !== 'object' || node === null) return []
  const element = node as { type?: unknown; props?: Record<string, unknown>; children?: unknown[] }
  return [...(element.type === 'Button' ? [String(element.props?.key)] : []), ...(element.children ?? []).flatMap(buttonsIn)]
}

type Drawn = { type: string; props: Record<string, unknown>; children: unknown[] }

/** The elements an element holds as drawn, strings left out. */
function elementsIn(node: { children: unknown[] } | undefined): Drawn[] {
  return (node?.children ?? []).filter((child): child is Drawn => typeof child === 'object' && child !== null)
}

/** The plates of the accent colour: what the desktop shows of a marked button, as its own label colours do not show. */
async function plates(ui: Pane) {
  return (await ui.findAll({ type: 'Box' })).filter(box => box.props.backgroundColor === LOOK.accent)
}

/** The Buttons that stand on such a plate. */
async function onPlate(ui: Pane): Promise<string[]> {
  return [...new Set((await plates(ui)).flatMap(buttonsIn))]
}

const choices = (keys: (string | undefined)[]) => keys.filter((key): key is string => key?.startsWith('set-') === true)

for (const surface of SURFACES) {
  test(`${surface}: Settings is a tab with the key h and reads show --json next to the status line`, async ($, on) => {
    const seen = world(on)
    const ui = await open($, surface)
    expect((await ui.find({ key: 'view-settings' }))?.props.hotkey).toBe(surface === 'desktop' ? undefined : 'h')
    expect(settingsRuns(seen)).toEqual([[...PREFIX, 'show', '--json']])
    expect(await rowText(ui, 'usage_clock')).toContain('What Usage clock does.')
    // Another view closes the tab again
    await ui.press({ key: 'view-agents' })
    expect(await ui.find({ key: 'row-usage_clock' })).toBeUndefined()
    await ui.unmount()
  })

  test(`${surface}: a button runs exactly its set and reads the settings again`, async ($, on) => {
    const seen = world(on)
    const ui = await open($, surface)
    await ui.press({ key: 'set-usage_clock-false' })
    expect(settingsRuns(seen).slice(1)).toEqual([[...PREFIX, 'set', 'modules.usage_clock', 'false'], [...PREFIX, 'show', '--json']])
    await ui.press({ key: 'set-spawn_gate-off' })
    expect(settingsRuns(seen).slice(3)).toEqual([[...PREFIX, 'set', 'modules.spawn_gate', 'off'], [...PREFIX, 'show', '--json']])
    await ui.press({ key: 'set-sync-on' })
    expect(settingsRuns(seen)[5]).toEqual([...PREFIX, 'set', 'sync.mode', 'on'])
    await ui.unmount()
  })

  test(`${surface}: the active choice is marked, an empty gate mode shows the mode of its entry as installed`, async ($, on) => {
    world(on)
    const ui = await open($, surface)
    expect((await ui.find({ key: 'set-usage_clock-true' }))?.text.trim()).toBe('on')
    expect((await ui.find({ key: 'set-usage_clock-false' }))?.text.trim()).toBe('off')
    expect((await ui.find({ key: 'set-spawn_gate-shadow' }))?.text.trim()).toBe('shadow (installed)')
    expect((await ui.find({ key: 'set-spawn_gate-enforce' }))?.text.trim()).toBe('enforce')
    expect((await labelOf(ui, 'set-spawn_gate-shadow')).backgroundColor).toBe('claude')
    expect((await labelOf(ui, 'set-spawn_gate-enforce')).backgroundColor).toBeUndefined()
    expect((await labelOf(ui, 'set-sync-shadow')).backgroundColor).toBe('claude')
    await ui.unmount()
  })

  test(`${surface}: parts that are not installed are grey and have no button`, async ($, on) => {
    world(on)
    const ui = await open($, surface)
    expect(await rowText(ui, 'soft_stop')).toContain('not installed: install.py')
    for (const key of ['set-soft_stop-off', 'set-soft_stop-shadow', 'set-soft_stop-enforce', 'info-soft_stop']) {
      expect(await ui.find({ key })).toBeUndefined()
    }
    // The status line has no switch: the reason stands in its place
    expect(await rowText(ui, 'statusline')).toContain('In the tab Status line works so.')
    expect(await ui.find({ key: 'set-statusline-true' })).toBeUndefined()
    await ui.unmount()
  })

  test(`${surface}: i unfolds the text of show --json under its row and folds it again`, async ($, on) => {
    world(on)
    const ui = await open($, surface)
    expect(await rowText(ui, 'spawn_gate')).not.toContain('About Spawn gate.')
    await ui.press({ key: 'info-spawn_gate' })
    expect(await rowText(ui, 'spawn_gate')).toContain('About Spawn gate.')
    expect(await rowText(ui, 'spawn_gate')).toContain('https://x/docs#spawn_gate')
    expect(await rowText(ui, 'spawn_gate')).toContain('In the tab Spawn gate works so.')
    await ui.press({ key: 'info-spawn_gate' })
    expect(await rowText(ui, 'spawn_gate')).not.toContain('About Spawn gate.')
    await ui.press({ key: 'info-repo' })
    expect(await rowText(ui, 'repo')).toContain('About the repo.')
    await ui.unmount()
  })

  test(`${surface}: an owner/name goes to check-repo unchanged as one word of the argv`, async ($, on) => {
    const seen = world(on, { check: 'public' })
    const ui = await open($, surface)
    expect((await ui.find({ key: 'repo' }))?.props.value).toBe('anna/usage-sync')
    await ui.input({ key: 'repo', text: 'an-na/usage.sync_2' })
    expect(settingsRuns(seen).at(-1)).toEqual([...PREFIX, 'check-repo', '--', 'an-na/usage.sync_2'])
    await ui.unmount()
  })

  test(`${surface}: a name that is no owner/name starts no process and says so`, async ($, on) => {
    const seen = world(on)
    const ui = await open($, surface)
    const before = settingsRuns(seen).length
    for (const name of [SPECIAL, '--help', 'a/../b', 'a/.', '-x/y', 'a\u001b]52;c;SGk=\u0007/b']) {
      await ui.input({ key: 'repo', text: name })
      expect(await ui.find({ key: 'save' })).toBeUndefined()
    }
    expect(settingsRuns(seen)).toHaveLength(before)
    expect(await hasText(ui, 'a?]52;c;SGk=?/b is no owner/name as on GitHub, for example anna/usage-sync.')).toBe(true)
    await ui.unmount()
  })

  test(`${surface}: Save appears only after private_ok and runs set-repo with that name`, async ($, on) => {
    const seen = world(on)
    const ui = await open($, surface)
    expect(await ui.find({ key: 'save' })).toBeUndefined()
    await check(ui, surface)
    expect(settingsRuns(seen).at(-1)).toEqual([...PREFIX, 'check-repo', '--', 'anna/usage-sync'])
    expect(await rowText(ui, 'repo')).toContain('Result private_ok.')
    expect(await ui.find({ key: 'save' })).toBeDefined()
    // Another name typed: its Check has not run, so there is no Save for it
    await ui.input({ key: 'repo', text: 'anna/other', kind: 'change' })
    expect(await ui.find({ key: 'save' })).toBeUndefined()
    await check(ui, surface)
    await ui.press({ key: 'save' })
    expect(settingsRuns(seen).slice(-2)).toEqual([[...PREFIX, 'set-repo', '--', 'anna/other'], [...PREFIX, 'show', '--json']])
    expect(await hasText(ui, 'Sync: cloned.')).toBe(true)
    await ui.unmount()
  })

  test(`${surface}: a save that finds a clone of another repository shows its text and changes nothing`, async ($, on) => {
    const seen = world(on, { save: 'other_clone' })
    const ui = await open($, surface)
    await check(ui, surface)
    await ui.press({ key: 'save' })
    expect(settingsRuns(seen).at(-2)).toEqual([...PREFIX, 'set-repo', '--', 'anna/usage-sync'])
    expect(await hasText(ui, 'Move that folder away, do not delete it.')).toBe(true)
    expect(await ui.find({ key: 'save' })).toBeUndefined()
    await ui.unmount()
  })

  test(`${surface}: a check that is not private_ok shows its result and no Save`, async ($, on) => {
    world(on, { check: 'not_found_or_no_access' })
    const ui = await open($, surface)
    await check(ui, surface)
    expect(await rowText(ui, 'repo')).toContain('Result not_found_or_no_access.')
    expect(await ui.find({ key: 'save' })).toBeUndefined()
    await ui.unmount()
  })

  test(`${surface}: without settings_cli.py the tab is the one update line`, async ($, on) => {
    const seen = world(on, { hasCli: false })
    const ui = await open($, surface)
    // The tab asked for the script next to the status line, was told there is none, and ran nothing
    expect(seen.asked.filter(path => /[\\/]settings_cli\.py$/.test(path)).length).toBeGreaterThan(0)
    expect(await ui.find({ type: 'Text', text: /needs a newer installation/ })).toBeDefined()
    expect(await hasText(ui, UPDATE_LINE)).toBe(true)
    expect(await ui.find({ key: 'row-usage_clock' })).toBeUndefined()
    expect(await ui.find({ key: 'repo' })).toBeUndefined()
    expect(settingsRuns(seen)).toEqual([])
    await ui.unmount()
  })

  test(`${surface}: while show --json reads again after a choice, the tab stays with all its buttons`, async ($, on) => {
    let asked: () => void = () => {}
    let release: () => void = () => {}
    const reading = new Promise<void>(resolve => { asked = resolve })
    const until = new Promise<void>(resolve => { release = resolve })
    world(on, { hold: { from: 2, asked: () => asked(), until } })
    const ui = await open($, surface)
    const keys = async () => (await ui.findAll({ type: 'Button' })).map(button => button.key)
    const before = await keys()
    expect(before).toContain('set-usage_clock-false')
    const pressing = ui.press({ key: 'set-usage_clock-false' })
    await reading
    expect(await ui.find({ type: 'Text', text: /loading/ })).toBeUndefined()
    expect(await keys()).toEqual(before)
    expect(await ui.find({ key: 'repo' })).toBeDefined()
    release()
    await pressing
    // The same keys afterwards: the focus stays on the button that was pressed
    expect(await keys()).toEqual(before)
    await ui.unmount()
  })

  test(`${surface}: while show --json reads again after Save, the tab stays; only Save itself is gone`, async ($, on) => {
    let asked: () => void = () => {}
    let release: () => void = () => {}
    const reading = new Promise<void>(resolve => { asked = resolve })
    const until = new Promise<void>(resolve => { release = resolve })
    world(on, { hold: { from: 2, asked: () => asked(), until } })
    const ui = await open($, surface)
    await check(ui, surface)
    const keys = async () => (await ui.findAll({ type: 'Button' })).map(button => button.key)
    const before = await keys()
    expect(before).toContain('save')
    const pressing = ui.press({ key: 'save' })
    await reading
    expect(await ui.find({ type: 'Text', text: /loading/ })).toBeUndefined()
    expect(await keys()).toEqual(before.filter(key => key !== 'save'))
    release()
    await pressing
    expect(await hasText(ui, 'Sync: cloned.')).toBe(true)
    await ui.unmount()
  })

  test(`${surface}: only the first read of the tab says loading`, async ($, on) => {
    let asked: () => void = () => {}
    let release: () => void = () => {}
    const reading = new Promise<void>(resolve => { asked = resolve })
    const until = new Promise<void>(resolve => { release = resolve })
    world(on, { hold: { from: 1, asked: () => asked(), until } })
    await $.session.start({ cwd: 'C:/proj', surface: 'terminal', isInteractive: true })
    await $.command.run({ command: 'report', args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 120 } })
    const ui = await $.ui.mount({
      plugin: 'usage-clock-display', surface, component: 'Pane', requestId: 'usage-report',
      props: { title: 'Usage report', isFocused: true, bodyColumns: 100, placement: 'dock', scroll: { offset: 0, bodyRows: 30 }, view: {} },
    })
    const opening = ui.press({ key: 'view-settings' })
    await reading
    expect(await exact(ui, 'loading…')).toBeDefined()
    release()
    await opening
    expect(await exact(ui, 'loading…')).toBeUndefined()
    expect(await ui.find({ key: 'row-usage_clock' })).toBeDefined()
    await ui.unmount()
  })

  test(`${surface}: the last success of the sync is a local time as the report writes it, not ISO UTC`, async ($, on) => {
    world(on, { sync: { last_success: '2026-10-21T12:33:45Z', last_error: 'network' } })
    const ui = await open($, surface)
    expect(await exact(ui, `last success ${localTime('2026-10-21T12:33:45Z')} · last error network`)).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /2026-10-21T/ })).toBeUndefined()
    await ui.unmount()
  })

  test(`${surface}: the sentence of the sync is the summary of show --json, nothing added`, async ($, on) => {
    world(on)
    const ui = await open($, surface)
    expect(await exact(ui, 'What the sync does.')).toBeDefined()
    expect((await ui.find({ key: 'body-sync' }))?.text).toContain('What the sync does.')
    await ui.unmount()
  })
}

test('terminal: a row is the name on its own line and one block indented by a Box', async ($, on) => {
  world(on)
  const ui = await open($, 'terminal')
  for (const [id, label] of [['statusline', 'Status line'], ['usage_clock', 'Usage clock'], ['spawn_gate', 'Spawn gate'],
    ['soft_stop', 'Soft stop'], ['sync', 'Sync'], ['repo', 'Sync repository']] as const) {
    const name = await exact(ui, label)
    expect([id, name?.text, name?.props.weight]).toEqual([id, label, 'bold'])
    const block = await ui.find({ key: `body-${id}` })
    expect([id, block?.props.paddingLeft]).toEqual([id, 2])
  }
  // The sentence without spaces in front: the Box indents it, also where it wraps
  expect(await exact(ui, 'What Usage clock does.')).toBeDefined()
  expect((await ui.find({ key: 'body-usage_clock' }))?.text).toContain('What Usage clock does.')
  await ui.unmount()
})

test('terminal: every row of buttons is in the block and starts in the same column', async ($, on) => {
  world(on)
  const ui = await open($, 'terminal')
  for (const id of ['statusline', 'usage_clock', 'spawn_gate', 'sync', 'repo']) {
    expect([id, (await ui.find({ key: `body-${id}` }))?.text]).toEqual([id, expect.stringContaining((await ui.find({ key: `controls-${id}` }))?.text ?? '-')])
  }
  // The first button of a row has no space in front unless it stands on the plate of the marked choice
  for (const key of ['set-usage_clock-true', 'set-spawn_gate-off', 'set-sync-off', 'info-statusline']) {
    const text = (await ui.find({ key }))?.text ?? ''
    const plate = (await labelOf(ui, key)).backgroundColor
    expect([key, text.startsWith(' ') ? plate : 'no space']).toEqual([key, text.startsWith(' ') ? 'claude' : 'no space'])
  }
  expect((await ui.find({ key: 'set-spawn_gate-off' }))?.text).toBe('off')
  expect((await ui.find({ key: 'set-sync-off' }))?.text).toBe('off')
  await ui.unmount()
})

test('terminal: the info is a block indented deeper, a line per line, the link on a line of its own', async ($, on) => {
  world(on)
  const ui = await open($, 'terminal')
  await ui.press({ key: 'info-spawn_gate' })
  const info = await ui.find({ key: 'info-text-spawn_gate' })
  expect(info?.props.paddingLeft).toBe(2)
  expect((await ui.find({ key: 'body-spawn_gate' }))?.text).toContain('About Spawn gate.')
  expect(await exact(ui, 'About Spawn gate.')).toBeDefined()
  expect(await exact(ui, 'More:')).toBeDefined()
  expect(await exact(ui, 'https://x/docs#spawn_gate')).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /More: https/ })).toBeUndefined()
  expect(await exact(ui, 'In the tab Spawn gate works so.')).toBeDefined()
  expect(info?.text).toContain('https://x/docs#spawn_gate')
  await ui.unmount()
})

test('terminal: the status line has its reason under its sentence and a row with i alone', async ($, on) => {
  world(on)
  const ui = await open($, 'terminal')
  const body = (await ui.find({ key: 'body-statusline' }))?.text ?? ''
  expect(body.indexOf('What Status line does.')).toBeLessThan(body.indexOf('In the tab Status line works so.'))
  expect((await ui.find({ key: 'controls-statusline' }))?.text.trim()).toBe('i')
  await ui.unmount()
})

test('terminal: the repository is a framed field, Check and Save stand on a plate', async ($, on) => {
  world(on)
  const ui = await open($, 'terminal')
  expect((await ui.find({ key: 'repo-field' }))?.props.borderStyle).toBe('round')
  expect((await ui.find({ key: 'controls-repo' }))?.props.columnGap).toBe(2)
  expect((await ui.find({ key: 'check' }))?.text).toBe(' Check ')
  expect((await labelOf(ui, 'check')).backgroundColor).toBe('claude')
  await ui.press({ key: 'check' })
  expect((await ui.find({ key: 'save' }))?.text).toBe(' Save ')
  expect((await labelOf(ui, 'save')).backgroundColor).toBe('claude')
  await ui.unmount()
})

test('terminal: the tab as text, top to bottom', async ($, on) => {
  world(on)
  const ui = await open($, 'terminal')
  const shownText = (await ui.findAll({ type: 'Text' })).map(found => found.text).join('\n')
  let at = 0
  for (const line of ['Status line', 'What Status line does.', 'In the tab Status line works so.',
    'Usage clock', 'What Usage clock does.', ' on ', 'off',
    'Spawn gate', 'What Spawn gate does.', 'off', ' shadow (installed) ', 'enforce',
    'Soft stop', 'What Soft stop does.', 'not installed: install.py',
    'Sync', 'What the sync does.', 'Sync repository', ' Check ']) {
    const found = shownText.indexOf(line, at)
    expect([line, found >= 0]).toEqual([line, true])
    at = found + line.length
  }
  await ui.unmount()
})

// The info of the spawn gate as _setup.INFO holds it: lines broken for a terminal, mid-sentence
const GATE_INFO = '  Checks every start of an agent, workflow or skill: no new block from 75 % of the 5-hour usage, a warning or\n'
  + '  a stop near the weekly limit, and model and effort in the agent definitions (thresholds in config.json).\n'
  + '  Writes: a PreToolUse hook (spawn_gate.py, matcher Agent|Workflow|Skill) in\n'
  + '  settings.json.\n'
  + '  More: https://x/blob/main/docs/gates.md#the-two-gates'

for (const surface of SURFACES) {
  test(`${surface}: the info is a paragraph; a line starts only before Writes: and More:, the tab sentence apart`, async ($, on) => {
    world(on, { info: GATE_INFO })
    const ui = await open($, surface)
    await ui.press({ key: 'info-spawn_gate' })
    expect(await exact(ui, 'Checks every start of an agent, workflow or skill: no new block from 75 % of the 5-hour usage, a warning or '
      + 'a stop near the weekly limit, and model and effort in the agent definitions (thresholds in config.json).')).toBeDefined()
    expect(await exact(ui, 'Writes: a PreToolUse hook (spawn_gate.py, matcher Agent|Workflow|Skill) in settings.json.')).toBeDefined()
    expect(await exact(ui, 'In the tab Spawn gate works so.')).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /a warning or$/ })).toBeUndefined()
    await ui.unmount()
  })
}

test('terminal: the link of the info stays a line of its own after More:', async ($, on) => {
  world(on, { info: GATE_INFO })
  const ui = await open($, 'terminal')
  await ui.press({ key: 'info-spawn_gate' })
  expect(await exact(ui, 'More:')).toBeDefined()
  expect(await exact(ui, 'https://x/blob/main/docs/gates.md#the-two-gates')).toBeDefined()
  expect(await ui.find({ type: 'Link' })).toBeUndefined()
  await ui.unmount()
})

test('desktop: the active choice of every group stands on the plate of the open tab, no other choice', async ($, on) => {
  world(on)
  const ui = await open($, 'desktop')
  const buttons = async () => (await ui.findAll({ type: 'Button' })).map(button => button.key)
  expect(choices(await onPlate(ui))).toEqual(['set-desktop_line-full', 'set-usage_clock-true', 'set-context_reporter-true', 'set-spawn_gate-shadow', 'set-sync-shadow'])
  expect(choices(await buttons())).toHaveLength(13)
  // The plate of a choice is the plate of the open tab
  const looks = (await plates(ui)).map(({ props }) => ({ ...props, key: undefined, children: undefined }))
  expect(looks.length).toBeGreaterThan(4)
  for (const look of looks) expect(look).toEqual(looks[0])
  await ui.press({ key: 'set-spawn_gate-enforce' })
  await ui.press({ key: 'set-usage_clock-false' })
  await ui.press({ key: 'set-sync-on' })
  expect(choices(await onPlate(ui))).toEqual(['set-desktop_line-full', 'set-usage_clock-false', 'set-context_reporter-true', 'set-spawn_gate-enforce', 'set-sync-on'])
  await ui.unmount()
})

test('desktop: the choices of a part stand in a framed group, i outside it on the right', async ($, on) => {
  world(on)
  const ui = await open($, 'desktop')
  for (const [id, keys] of [['usage_clock', ['true', 'false']], ['context_reporter', ['true', 'false']],
    ['spawn_gate', ['off', 'shadow', 'enforce']], ['sync', ['off', 'shadow', 'on']]] as const) {
    const group = await ui.find({ key: `group-${id}` })
    expect([id, group?.props.borderStyle, group?.props.borderColor]).toEqual([id, 'round', LOOK.border])
    expect([id, buttonsIn(group)]).toEqual([id, keys.map(choice => `set-${id}-${choice}`)])
    expect([id, buttonsIn(await ui.find({ key: `controls-${id}` })).at(-1)]).toEqual([id, `info-${id}`])
  }
  expect(await ui.find({ key: 'group-statusline' })).toBeUndefined()
  await ui.unmount()
})

for (const surface of SURFACES) {
  test(`${surface}: the desktop line has off, short and full; its buttons set modules.desktop_line`, async ($, on) => {
    const seen = world(on)
    const ui = await open($, surface)
    for (const choice of ['off', 'short', 'full']) {
      expect((await ui.find({ key: `set-desktop_line-${choice}` }))?.text.trim()).toBe(choice)
    }
    expect((await labelOf(ui, 'set-desktop_line-full')).backgroundColor).toBe('claude')
    await ui.press({ key: 'set-desktop_line-short' })
    expect(settingsRuns(seen).slice(1)).toEqual([[...PREFIX, 'set', 'modules.desktop_line', 'short'], [...PREFIX, 'show', '--json']])
    expect((await labelOf(ui, 'set-desktop_line-short')).backgroundColor).toBe('claude')
    await ui.unmount()
  })
}

test('desktop: the head of a card is the name and its state as a small label in capitals', async ($, on) => {
  world(on)
  const ui = await open($, 'desktop')
  // The engine finds a Text by its text only: the name and the label are the Texts of the head
  const head = async (id: string) => elementsIn(await ui.find({ key: `head-${id}` }))
  const state = async (id: string) => {
    const label = (await head(id))[1]
    return label === undefined ? undefined : [label.children.join(''), label.props.color, label.props.size]
  }
  for (const [id, name] of [['statusline', 'Status line'], ['usage_clock', 'Usage clock'], ['spawn_gate', 'Spawn gate'], ['sync', 'Sync']]) {
    const [text] = await head(id)
    expect([id, text?.type, text?.children.join(''), text?.props.weight, text?.props.color]).toEqual([id, 'Text', name, 'semibold', LOOK.text])
  }
  expect(await state('statusline')).toBeUndefined()
  expect(await state('usage_clock')).toEqual(['ON', LOOK.accent, 'caption'])
  expect(await state('desktop_line')).toEqual(['FULL', LOOK.accent, 'caption'])
  expect(await state('context_reporter')).toEqual(['ON', LOOK.accent, 'caption'])
  expect(await state('spawn_gate')).toEqual(['SHADOW', LOOK.warning, 'caption'])
  expect(await state('soft_stop')).toEqual(['NOT INSTALLED', LOOK.muted, 'caption'])
  expect(await state('sync')).toEqual(['SHADOW', LOOK.warning, 'caption'])
  // The name and the label share a row
  expect((await ui.find({ key: 'head-spawn_gate' }))?.props.flexDirection).toBe('row')
  expect((await ui.find({ key: 'head-spawn_gate' }))?.text).toContain('SHADOW')
  await ui.press({ key: 'set-usage_clock-false' })
  await ui.press({ key: 'set-spawn_gate-enforce' })
  await ui.press({ key: 'set-sync-on' })
  expect(await state('usage_clock')).toEqual(['OFF', LOOK.muted, 'caption'])
  expect(await state('spawn_gate')).toEqual(['ENFORCE', LOOK.accent, 'caption'])
  expect(await state('sync')).toEqual(['ON', LOOK.accent, 'caption'])
  await ui.press({ key: 'set-spawn_gate-off' })
  await ui.press({ key: 'set-sync-off' })
  await ui.press({ key: 'set-desktop_line-short' })
  expect(await state('desktop_line')).toEqual(['SHORT', LOOK.accent, 'caption'])
  await ui.press({ key: 'set-desktop_line-off' })
  expect(await state('desktop_line')).toEqual(['OFF', LOOK.muted, 'caption'])
  expect(await state('spawn_gate')).toEqual(['OFF', LOOK.muted, 'caption'])
  expect(await state('sync')).toEqual(['OFF', LOOK.muted, 'caption'])
  await ui.unmount()
})

test('desktop: the sentence and the texts beside it are muted, as the footnotes of the tiles', async ($, on) => {
  world(on, { sync: { last_success: '2026-10-21T12:33:45Z', last_error: null } })
  const ui = await open($, 'desktop')
  for (const text of ['What Usage clock does.', 'What Soft stop does.', 'What the sync does.', 'In the tab Status line works so.',
    'not installed: install.py', `last success ${localTime('2026-10-21T12:33:45Z')}`]) {
    const found = await exact(ui, text)
    expect([text, found?.props.color, found?.props.size]).toEqual([text, LOOK.muted, 'footnote'])
  }
  await ui.unmount()
})

test('desktop: the cards of the parts stand side by side, at most two; Sync and its repository span the width', async ($, on) => {
  world(on)
  for (const [columns, width] of [[100, '49%'], [140, '49%'], [40, '100%']] as const) {
    const ui = await open($, 'desktop', columns)
    for (const id of ['statusline', 'usage_clock', 'context_reporter', 'spawn_gate', 'soft_stop']) {
      expect([columns, id, (await ui.find({ key: `row-${id}` }))?.props.width]).toEqual([columns, id, width])
    }
    expect((await ui.find({ key: 'row-sync' }))?.props.width).toBe('100%')
    expect((await ui.find({ key: 'row-repo' }))?.props.width).toBe('100%')
    await ui.unmount()
  }
})

test('desktop: a card of the tab is a card of the other views', async ($, on) => {
  world(on)
  const ui = await mount($, 'desktop')
  const look = (props: Record<string, unknown> | undefined) => ({ ...props, key: undefined, width: undefined, children: undefined })
  const tile = (await ui.find({ key: 'kpi-week' }))?.props
  expect(tile?.backgroundColor).toBe(LOOK.card)
  await ui.press({ key: 'view-settings' })
  for (const id of ['usage_clock', 'soft_stop', 'sync', 'repo']) {
    expect([id, look((await ui.find({ key: `row-${id}` }))?.props)]).toEqual([id, look(tile)])
  }
  await ui.unmount()
})

test('desktop: one Check, the button of the field; Save stands on the plate', async ($, on) => {
  const seen = world(on)
  const ui = await open($, 'desktop')
  expect((await ui.find({ key: 'repo' }))?.props.submitLabel).toBe('Check')
  expect(await ui.find({ key: 'check' })).toBeUndefined()
  expect(await ui.findAll({ type: 'Button', text: /check/i })).toEqual([])
  await ui.input({ key: 'repo', text: 'anna/usage-sync' })
  expect(settingsRuns(seen).at(-1)).toEqual([...PREFIX, 'check-repo', '--', 'anna/usage-sync'])
  expect(await onPlate(ui)).toContain('save')
  await ui.unmount()
})

test('terminal: the field keeps its word and the Check beside it', async ($, on) => {
  world(on)
  const ui = await open($, 'terminal')
  expect((await ui.find({ key: 'repo' }))?.props.submitLabel).toBe('check')
  expect(await ui.find({ key: 'check' })).toBeDefined()
  await ui.unmount()
})

test('desktop: the info is muted small type inside the card, not mono; the docs are a link', async ($, on) => {
  world(on, { info: GATE_INFO })
  const ui = await open($, 'desktop')
  await ui.press({ key: 'info-spawn_gate' })
  const info = await ui.find({ key: 'info-text-spawn_gate' })
  const texts = elementsIn(info)
  expect(texts.length).toBe(4)
  for (const text of texts) expect([text.type, text.props.mono, text.props.color, text.props.size]).toEqual(['Text', undefined, LOOK.muted, 'footnote'])
  expect(await rowText(ui, 'spawn_gate')).toContain('a warning or a stop near')
  const link = await ui.find({ type: 'Link' })
  expect([link?.props.href, link?.children]).toEqual(['https://x/blob/main/docs/gates.md#the-two-gates', ['Docs: gates.md#the-two-gates']])
  expect(await exact(ui, 'https://x/blob/main/docs/gates.md#the-two-gates')).toBeUndefined()
  expect(await exact(ui, 'More:')).toBeUndefined()
  await ui.unmount()
})

test('an answer of the script reaches the tab without control characters, its lines kept', () => {
  const answer = parseAnswer(JSON.stringify({ problems: ['modules.x\u001b]52;c;SGk=\u0007\u009b\u0000\t: unknown key'], info: 'a\nb', n: 1 }))
  expect(answer).toEqual({ problems: ['modules.x?]52;c;SGk=????: unknown key'], info: 'a\nb', n: 1 })
})

test('only an owner/name as sync.repo takes it is a repository', () => {
  for (const name of ['anna/usage-sync', 'a/b', 'A-1/x.y_z', `${'a'.repeat(39)}/${'b'.repeat(100)}`]) expect(validRepo(name)).toBe(true)
  for (const name of ['', 'a', 'a/b/c', '-a/b', 'a/..', 'a/.', 'a/b..c', `${'a'.repeat(40)}/b`, `a/${'b'.repeat(101)}`, 'a /b', 'a/b\n']) {
    expect(validRepo(name)).toBe(false)
  }
  expect(INVALID_REPO).toContain('{repo}')
})
