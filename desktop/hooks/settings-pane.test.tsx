import type { On } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'

import { localTime } from './report'
import { REPORT } from './report-fixture'
import { UPDATE_LINE } from './settings'

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
function shown(values: Record<string, unknown> = {}, sync: Record<string, unknown> = {}) {
  return {
    modules: [
      part('statusline', 'Status line', true, null, null, []),
      part('usage_clock', 'Usage clock', true, 'modules.usage_clock', values.usage_clock ?? true, ['true', 'false']),
      part('context_reporter', 'Context reporter', true, 'modules.context_reporter', true, ['true', 'false']),
      part('spawn_gate', 'Spawn gate', true, 'modules.spawn_gate', values.spawn_gate ?? '', ['off', 'shadow', 'enforce'],
        { installed_mode: 'shadow' }),
      part('soft_stop', 'Soft stop', false, 'modules.soft_stop', '', ['off', 'shadow', 'enforce'], { installed_mode: null }),
    ],
    sync: {
      installed: true, repo: 'anna/usage-sync', mode: 'shadow', choices: ['off', 'shadow', 'on'], summary: 'What the sync does.',
      last_success: null, last_error: null, info: '  About the sync.', tab: 'Sync in the tab.', repo_info: '  About the repo.',
      repo_tab: 'Check and Save.', ...sync,
    },
    problems: [],
  }
}

/** Holds every show --json from the `from`-th on: `asked` is called when one comes, it answers once `until` settles. */
type Hold = { from: number; asked: () => void; until: Promise<void> }

function world(on: On, options: { hasCli?: boolean; check?: string; save?: string; sync?: Record<string, unknown>; hold?: Hold } = {}) {
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
      outcome = { exitCode: 0, stdout: JSON.stringify(shown(seen.values, options.sync)), stderr: '' }
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

async function open($: Engine, surface: Surface) {
  await $.session.start({ cwd: 'C:/proj', surface: 'terminal', isInteractive: true })
  await $.command.run({ command: 'report', args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 120 } })
  const ui = await $.ui.mount({
    plugin: 'usage-clock-display',
    surface,
    component: 'Pane',
    requestId: 'usage-report',
    props: { title: 'Usage report', isFocused: true, bodyColumns: 100, placement: 'dock', scroll: { offset: 0, bodyRows: 30 }, view: {} },
  })
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

async function hasText(ui: Pane, text: string): Promise<boolean> {
  return (await ui.find({ type: 'Text', text })) !== undefined
}

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

  test(`${surface}: the repository goes to check-repo unchanged as one word of the argv`, async ($, on) => {
    const seen = world(on, { check: 'public' })
    const ui = await open($, surface)
    expect((await ui.find({ key: 'repo' }))?.props.value).toBe('anna/usage-sync')
    await ui.input({ key: 'repo', text: SPECIAL })
    expect(settingsRuns(seen).at(-1)).toEqual([...PREFIX, 'check-repo', '--', SPECIAL])
    await ui.input({ key: 'repo', text: '--help' })
    expect(settingsRuns(seen).at(-1)).toEqual([...PREFIX, 'check-repo', '--', '--help'])
    await ui.unmount()
  })

  test(`${surface}: Save appears only after private_ok and runs set-repo with that name`, async ($, on) => {
    const seen = world(on)
    const ui = await open($, surface)
    expect(await ui.find({ key: 'save' })).toBeUndefined()
    await ui.press({ key: 'check' })
    expect(settingsRuns(seen).at(-1)).toEqual([...PREFIX, 'check-repo', '--', 'anna/usage-sync'])
    expect(await rowText(ui, 'repo')).toContain('Result private_ok.')
    expect(await ui.find({ key: 'save' })).toBeDefined()
    // Another name typed: its Check has not run, so there is no Save for it
    await ui.input({ key: 'repo', text: 'anna/other', kind: 'change' })
    expect(await ui.find({ key: 'save' })).toBeUndefined()
    await ui.press({ key: 'check' })
    await ui.press({ key: 'save' })
    expect(settingsRuns(seen).slice(-2)).toEqual([[...PREFIX, 'set-repo', '--', 'anna/other'], [...PREFIX, 'show', '--json']])
    expect(await hasText(ui, 'Sync: cloned.')).toBe(true)
    await ui.unmount()
  })

  test(`${surface}: a save that finds a clone of another repository shows its text and changes nothing`, async ($, on) => {
    const seen = world(on, { save: 'other_clone' })
    const ui = await open($, surface)
    await ui.press({ key: 'check' })
    await ui.press({ key: 'save' })
    expect(settingsRuns(seen).at(-2)).toEqual([...PREFIX, 'set-repo', '--', 'anna/usage-sync'])
    expect(await hasText(ui, 'Move that folder away, do not delete it.')).toBe(true)
    expect(await ui.find({ key: 'save' })).toBeUndefined()
    await ui.unmount()
  })

  test(`${surface}: a check that is not private_ok shows its result and no Save`, async ($, on) => {
    world(on, { check: 'not_found_or_no_access' })
    const ui = await open($, surface)
    await ui.press({ key: 'check' })
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
    await ui.press({ key: 'check' })
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
    expect(await ui.find({ key: 'settings-loading' })).toBeDefined()
    release()
    await opening
    expect(await ui.find({ key: 'settings-loading' })).toBeUndefined()
    expect(await ui.find({ key: 'row-usage_clock' })).toBeDefined()
    await ui.unmount()
  })

  test(`${surface}: the last success of the sync is a local time as the report writes it, not ISO UTC`, async ($, on) => {
    world(on, { sync: { last_success: '2026-10-21T12:33:45Z', last_error: 'network' } })
    const ui = await open($, surface)
    expect((await ui.find({ key: 'sync-state' }))?.text).toBe(`last success ${localTime('2026-10-21T12:33:45Z')} · last error network`)
    expect(await ui.find({ type: 'Text', text: /2026-10-21T/ })).toBeUndefined()
    await ui.unmount()
  })

  test(`${surface}: the sentence of the sync is the summary of show --json, nothing added`, async ($, on) => {
    world(on)
    const ui = await open($, surface)
    expect((await ui.find({ key: 'summary-sync' }))?.text).toBe('What the sync does.')
    await ui.unmount()
  })
}

test('terminal: a row is the name on its own line and one block indented by a Box', async ($, on) => {
  world(on)
  const ui = await open($, 'terminal')
  for (const [id, label] of [['statusline', 'Status line'], ['usage_clock', 'Usage clock'], ['spawn_gate', 'Spawn gate'],
    ['soft_stop', 'Soft stop'], ['sync', 'Sync'], ['repo', 'Sync repository']] as const) {
    const name = await ui.find({ key: `part-${id}` })
    expect([id, name?.text, name?.props.weight]).toEqual([id, label, 'bold'])
    const block = await ui.find({ key: `body-${id}` })
    expect([id, block?.props.paddingLeft]).toEqual([id, 2])
  }
  // The sentence without spaces in front: the Box indents it, also where it wraps
  expect((await ui.find({ key: 'summary-usage_clock' }))?.text).toBe('What Usage clock does.')
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
  expect((await ui.find({ key: 'info-spawn_gate-0' }))?.text).toBe('About Spawn gate.')
  expect((await ui.find({ key: 'info-spawn_gate-1' }))?.text).toBe('More:')
  expect((await ui.find({ key: 'info-spawn_gate-2' }))?.text).toBe('https://x/docs#spawn_gate')
  expect((await ui.find({ key: 'info-spawn_gate-3' }))?.text).toBe('In the tab Spawn gate works so.')
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
