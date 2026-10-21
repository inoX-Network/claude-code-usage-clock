import type { On } from 'claude-code'
import { expect, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'

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
function shown(values: Record<string, unknown> = {}) {
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
      repo_tab: 'Check and Save.',
    },
    problems: [],
  }
}

function world(on: On, options: { hasCli?: boolean; check?: string } = {}) {
  const seen = { runs: [] as (readonly string[])[], values: {} as Record<string, unknown> }
  on('session.usage', () => ({ value: { startedAt: 0, context: { window: 1_000_000 }, rateLimits: [] } }))
  on('session.start', ($, e) => e)
  on('fs.read', () => ({ deny: 'ENOENT' }))
  on('fs.exists', ($, e) => ({ value: e.path === CLI ? (options.hasCli ?? true) : true }))
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
      outcome = { exitCode: 0, stdout: JSON.stringify(shown(seen.values)), stderr: '' }
    } else if (e.argv[4] === 'set') {
      const key = String(e.argv[5]).split('.')[1] as string
      seen.values[key] = e.argv[6] === 'true' ? true : e.argv[6] === 'false' ? false : e.argv[6]
      outcome = { exitCode: 0, stdout: JSON.stringify({ key: e.argv[5], value: seen.values[key], changed: true, backup: null }), stderr: '' }
    } else if (e.argv[4] === 'check-repo') {
      const result = options.check ?? 'private_ok'
      outcome = { exitCode: result === 'private_ok' ? 0 : 1, stdout: JSON.stringify({ result, repo: e.argv[5], text: `Result ${result}.` }), stderr: '' }
    } else {
      outcome = { exitCode: 0, stdout: JSON.stringify({ result: 'saved', repo: e.argv[5], text: 'Sync: cloned.' }), stderr: '' }
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

for (const surface of SURFACES) {
  test(`${surface}: Settings is a tab with the key h and reads show --json next to the status line`, async ($, on) => {
    const seen = world(on)
    const ui = await open($, surface)
    expect((await ui.find({ key: 'view-settings' }))?.props.hotkey).toBe(surface === 'desktop' ? undefined : 'h')
    expect(settingsRuns(seen)).toEqual([[...PREFIX, 'show', '--json']])
    expect((await ui.find({ key: 'part-usage_clock' }))?.text).toContain('What Usage clock does.')
    // Another view closes the tab again
    await ui.press({ key: 'view-agents' })
    expect(await ui.find({ key: 'part-usage_clock' })).toBeUndefined()
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
    expect((await ui.find({ key: 'label-set-spawn_gate-shadow' }))?.props.backgroundColor).toBe('claude')
    expect((await ui.find({ key: 'label-set-spawn_gate-enforce' }))?.props.backgroundColor).toBeUndefined()
    expect((await ui.find({ key: 'label-set-sync-shadow' }))?.props.backgroundColor).toBe('claude')
    await ui.unmount()
  })

  test(`${surface}: parts that are not installed are grey and have no button`, async ($, on) => {
    world(on)
    const ui = await open($, surface)
    expect((await ui.find({ key: 'absent-soft_stop' }))?.text).toBe('not installed: install.py')
    for (const key of ['set-soft_stop-off', 'set-soft_stop-shadow', 'set-soft_stop-enforce', 'info-soft_stop']) {
      expect(await ui.find({ key })).toBeUndefined()
    }
    // The status line has no switch: the reason stands in its place
    expect((await ui.find({ key: 'fixed-statusline' }))?.text).toBe('In the tab Status line works so.')
    await ui.unmount()
  })

  test(`${surface}: i unfolds the text of show --json under its row and folds it again`, async ($, on) => {
    world(on)
    const ui = await open($, surface)
    expect(await ui.find({ key: 'info-text-spawn_gate' })).toBeUndefined()
    await ui.press({ key: 'info-spawn_gate' })
    expect((await ui.find({ key: 'info-text-spawn_gate' }))?.text).toBe('  About Spawn gate.\n  More: https://x/docs#spawn_gate')
    expect((await ui.find({ key: 'info-tab-spawn_gate' }))?.text).toBe('  In the tab Spawn gate works so.')
    await ui.press({ key: 'info-spawn_gate' })
    expect(await ui.find({ key: 'info-text-spawn_gate' })).toBeUndefined()
    await ui.press({ key: 'info-repo' })
    expect((await ui.find({ key: 'info-text-repo' }))?.text).toBe('  About the repo.')
    await ui.unmount()
  })

  test(`${surface}: the repository goes to check-repo unchanged as one word of the argv`, async ($, on) => {
    const seen = world(on, { check: 'public' })
    const ui = await open($, surface)
    expect((await ui.find({ key: 'repo' }))?.props.value).toBe('anna/usage-sync')
    await ui.input({ key: 'repo', text: SPECIAL })
    expect(settingsRuns(seen).at(-1)).toEqual([...PREFIX, 'check-repo', SPECIAL])
    await ui.unmount()
  })

  test(`${surface}: Save appears only after private_ok and runs set-repo with that name`, async ($, on) => {
    const seen = world(on)
    const ui = await open($, surface)
    expect(await ui.find({ key: 'save' })).toBeUndefined()
    await ui.press({ key: 'check' })
    expect(settingsRuns(seen).at(-1)).toEqual([...PREFIX, 'check-repo', 'anna/usage-sync'])
    expect((await ui.find({ key: 'check-text' }))?.text).toBe('Result private_ok.')
    expect(await ui.find({ key: 'save' })).toBeDefined()
    // Another name typed: its Check has not run, so there is no Save for it
    await ui.input({ key: 'repo', text: 'anna/other', kind: 'change' })
    expect(await ui.find({ key: 'save' })).toBeUndefined()
    await ui.press({ key: 'check' })
    await ui.press({ key: 'save' })
    expect(settingsRuns(seen).slice(-2)).toEqual([[...PREFIX, 'set-repo', 'anna/other'], [...PREFIX, 'show', '--json']])
    expect((await ui.find({ key: 'note' }))?.text).toBe('Sync: cloned.')
    await ui.unmount()
  })

  test(`${surface}: a check that is not private_ok shows its result and no Save`, async ($, on) => {
    world(on, { check: 'not_found_or_no_access' })
    const ui = await open($, surface)
    await ui.press({ key: 'check' })
    expect((await ui.find({ key: 'check-text' }))?.text).toBe('Result not_found_or_no_access.')
    expect(await ui.find({ key: 'save' })).toBeUndefined()
    await ui.unmount()
  })

  test(`${surface}: without settings_cli.py the tab is the one update line`, async ($, on) => {
    const seen = world(on, { hasCli: false })
    const ui = await open($, surface)
    expect((await ui.find({ key: 'settings-update' }))?.text).toBe(UPDATE_LINE)
    expect(await ui.find({ key: 'part-usage_clock' })).toBeUndefined()
    expect(await ui.find({ key: 'repo' })).toBeUndefined()
    expect(settingsRuns(seen)).toEqual([])
    await ui.unmount()
  })
}

test('terminal: the rows of the tab as text', async ($, on) => {
  world(on)
  const ui = await open($, 'terminal')
  const lines = []
  for (const key of ['part-statusline', 'fixed-statusline', 'part-usage_clock', 'set-usage_clock-true', 'set-usage_clock-false',
    'part-spawn_gate', 'set-spawn_gate-off', 'set-spawn_gate-shadow', 'set-spawn_gate-enforce', 'part-soft_stop',
    'absent-soft_stop', 'part-sync', 'set-sync-off', 'set-sync-shadow', 'set-sync-on', 'part-repo', 'check']) {
    lines.push((await ui.find({ key }))?.text.trim())
  }
  expect(lines).toEqual([
    'Status line  What Status line does.', 'In the tab Status line works so.',
    'Usage clock  What Usage clock does.', 'on', 'off',
    'Spawn gate  What Spawn gate does.', 'off', 'shadow (installed)', 'enforce',
    'Soft stop  What Soft stop does.', 'not installed: install.py',
    'Sync  What the sync does.', 'off', 'shadow', 'on',
    'Sync repository', 'Check',
  ])
  await ui.unmount()
})
