import { expect, mock, test } from 'claude-code/testing'

const USAGE = {
  startedAt: 0,
  context: { tokens: 340_000, window: 1_000_000, percent: 34 },
  rateLimits: [{ kind: 'seven_day', percentUsed: 72 }],
}
const CONFIG = JSON.stringify({ statusline: { yellow_from: 70, red_from: 85 }, thresholds: { start_block_week: 90 } })

function world(on: Parameters<Parameters<typeof test>[1]>[1], files: Record<string, string>, read: string[], usage = USAGE) {
  mock.clock(on, { now: Date.parse('2026-10-17T06:00:00Z') })
  mock.env(on, { USERPROFILE: 'C:\\Users\\you' })
  on('session.usage', () => ({ value: usage }))
  on('session.start', ($, e) => e)
  on('session.measure', ($, e) => ({ changed: e.changed }))
  on('session.surfaces', () => ({ value: ['desktop'] }))
  on('settings.read', () => ({ value: {} }))
  on('turn.complete', ($, e) => ({ text: e.answer }))
  // The engine hands the path on absolute and in the system's spelling: compared with forward slashes, by its end
  on('fs.read', ($, e) => {
    const path = e.path.split(String.fromCharCode(92)).join('/')
    read.push(path)
    const hit = Object.keys(files).find(end => path.endsWith(end))
    return hit === undefined ? { deny: 'ENOENT' } : { value: files[hit] }
  })
  on('ui.render', { component: 'SessionMode' }, ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text dimColor>bypass</Text>
  })
  on('ui.render', { component: 'AbovePrompt' }, ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text dimColor>engine band</Text>
  })
}

const BAND_PROPS = { hasSurvey: false, isWorking: false, maxRows: 10, bodyColumns: 100, scroll: { offset: 0, bodyRows: 9 }, view: {} }

type Site = 'band' | 'footer'

async function mount($: Parameters<Parameters<typeof test>[1]>[0], surface: 'terminal' | 'desktop', site: Site, hasSurvey = false) {
  return site === 'band'
    ? $.ui.mount({ plugin: 'usage-clock-display', surface, component: 'AbovePrompt', props: { ...BAND_PROPS, hasSurvey } })
    : $.ui.mount({ plugin: 'usage-clock-display', surface, component: 'SessionMode', props: { modes: [] } })
}

// The default of modules.desktop_line is full: the line stands in the band above the prompt
async function line($: Parameters<Parameters<typeof test>[1]>[0], surface: 'terminal' | 'desktop', site: Site = 'band') {
  const ui = await mount($, surface, site)
  const found = {
    own: await ui.find({ type: 'Text', text: /bypass|engine band/ }),
    week: await ui.find({ type: 'Text', text: /week 72%/ }),
    ctx: await ui.find({ type: 'Text', text: /ctx 340k\/1000k 34%/ }),
    marker: await ui.find({ type: 'Text', text: /week gate/ }),
  }
  await ui.unmount()
  return found
}

test('the desktop app gets the line, coloured by the usage-clock config', async ($, on) => {
  const read: string[] = []
  world(on, { 'C:/Users/you/.claude/hooks/usage-clock/config.json': CONFIG }, read)
  await $.session.start({ cwd: 'C:/proj' })
  await $.session.measure({ ...USAGE, changed: ['context', 'rateLimits'] })
  // At the start, and again with the measurement: the Settings tab may have moved the line meanwhile
  expect(read).toHaveLength(2)
  expect(read.every(path => path.endsWith('C:/Users/you/.claude/hooks/usage-clock/config.json'))).toBe(true)
  const found = await line($, 'desktop')
  expect(found.own).toBeDefined()
  expect(found.week?.props.color).toBe('warning')   // 72 % is yellow from 70 on, red only from 85
  expect(found.ctx?.props.color).toBe('warning')
  expect(found.marker).toBeUndefined()                // 72 % is below start_block_week 90
})

test('the weekly gate marker follows the week with a space and names week_action, like the status line', async ($, on) => {
  world(on, { 'C:/Users/you/.claude/hooks/usage-clock/config.json': JSON.stringify({ thresholds: { start_block_week: 72, week_action: 'ask' } }) }, [])
  await $.session.start({ cwd: 'C:/proj' })
  await $.session.measure({ ...USAGE, changed: ['context', 'rateLimits'] })
  const found = await line($, 'desktop')
  expect(found.marker?.text).toBe(' <- week gate: agents ask')
  expect(found.ctx?.text).toBe(' | ctx 340k/1000k 34%')
})

test('the terminal keeps its own footer and band: the status line shows the same there', async ($, on) => {
  world(on, { 'C:/Users/you/.claude/hooks/usage-clock/config.json': JSON.stringify({ modules: { desktop_line: 'short' } }) }, [])
  await $.session.start({ cwd: 'C:/proj' })
  await $.session.measure({ ...USAGE, changed: ['context', 'rateLimits'] })
  const footer = await line($, 'terminal', 'footer')
  expect(footer.own).toBeDefined()
  expect(footer.week).toBeUndefined()
  expect((await line($, 'terminal', 'band')).week).toBeUndefined()
})

test('full, the default: the band above the prompt has the line in colour, the footer keeps only its own', async ($, on) => {
  const calm = JSON.stringify({ statusline: { context_yellow_from_k: 900, context_red_from_k: 950 } })
  const low = { ...USAGE, rateLimits: [{ kind: 'five_hour', percentUsed: 12 }, { kind: 'seven_day', percentUsed: 30 }] }
  world(on, { 'C:/Users/you/.claude/hooks/usage-clock/config.json': calm }, [], low)
  await $.session.start({ cwd: 'C:/proj' })
  await $.session.measure({ ...low, changed: ['context', 'rateLimits'] })
  await $.turn.complete({ answer: 'ok', durationMs: 10, isAborted: false, turnId: 't', reason: 'answer' })
  const ui = await mount($, 'desktop', 'band')
  expect((await ui.find({ type: 'Text', text: /5h 12%/ }))?.props.color).toBe('success')
  expect((await ui.find({ type: 'Text', text: /week 30%/ }))?.props.color).toBe('success')
  expect((await ui.find({ type: 'Text', text: /ctx 340k/ }))?.props.color).toBe('suggestion')
  expect((await ui.find({ type: 'Text', text: /cache ~/ }))?.props.color).toBe('claude')
  await ui.unmount()
  const footer = await line($, 'desktop', 'footer')
  expect(footer.own).toBeDefined()
  expect(footer.week).toBeUndefined()
})

test('the band gives way to a survey', async ($, on) => {
  world(on, {}, [])
  await $.session.start({ cwd: 'C:/proj' })
  await $.session.measure({ ...USAGE, changed: ['context', 'rateLimits'] })
  const ui = await mount($, 'desktop', 'band', true)
  expect(await ui.find({ type: 'Text', text: /week 72%/ })).toBeUndefined()
  await ui.unmount()
})

test('short: the line stays in the footer, cut as before, and the band is free', async ($, on) => {
  world(on, { 'C:/Users/you/.claude/hooks/usage-clock/config.json': JSON.stringify({ modules: { desktop_line: 'short' } }) }, [])
  await $.session.start({ cwd: 'C:/proj' })
  await $.session.measure({ ...USAGE, changed: ['context', 'rateLimits'] })
  const footer = await line($, 'desktop', 'footer')
  expect(footer.own).toBeDefined()
  expect(footer.week?.props.color).toBe('warning')
  expect((await line($, 'desktop', 'band')).week).toBeUndefined()
})

test('off: neither the band nor the footer has the line, and a change of config.json counts from the next measurement', async ($, on) => {
  let config = JSON.stringify({ modules: { desktop_line: 'off' } })
  mock.clock(on, { now: Date.parse('2026-10-17T06:00:00Z') })
  mock.env(on, { USERPROFILE: 'C:\\Users\\you' })
  on('session.usage', () => ({ value: USAGE }))
  on('session.surfaces', () => ({ value: ['desktop'] }))
  on('settings.read', () => ({ value: {} }))
  on('session.start', ($, e) => e)
  on('session.measure', ($, e) => ({ changed: e.changed }))
  on('fs.read', ($, e) => (e.path.split(String.fromCharCode(92)).join('/').endsWith('/config.json') ? { value: config } : { deny: 'ENOENT' }))
  on('ui.render', { component: 'SessionMode' }, ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text dimColor>bypass</Text>
  })
  on('ui.render', { component: 'AbovePrompt' }, ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text dimColor>engine band</Text>
  })
  await $.session.start({ cwd: 'C:/proj' })
  await $.session.measure({ ...USAGE, changed: ['context', 'rateLimits'] })
  expect((await line($, 'desktop', 'band')).week).toBeUndefined()
  const footer = await line($, 'desktop', 'footer')
  expect(footer.own).toBeDefined()
  expect(footer.week).toBeUndefined()
  config = JSON.stringify({ modules: { desktop_line: 'full' } })
  await $.session.measure({ ...USAGE, changed: ['context', 'rateLimits'] })
  expect((await line($, 'desktop', 'band')).week).toBeDefined()
})

test('without a config.json the defaults colour it', async ($, on) => {
  world(on, {}, [])
  await $.session.start({ cwd: 'C:/proj' })
  await $.session.measure({ ...USAGE, changed: ['context', 'rateLimits'] })
  expect((await line($, 'desktop')).week?.props.color).toBe('warning')   // default yellow from 50, red from 75
})

test('a config_file from the plugin options is read instead', { options: { config_file: '/etc/usage-clock/config.json' } },
  async ($, on) => {
    const read: string[] = []
    world(on, { '/etc/usage-clock/config.json': JSON.stringify({ statusline: { red_from: 72 } }) }, read)
    await $.session.start({ cwd: 'C:/proj' })
    await $.session.measure({ ...USAGE, changed: ['context', 'rateLimits'] })
    expect(read).toHaveLength(2)
    expect(read.every(path => path.endsWith('/etc/usage-clock/config.json'))).toBe(true)
    expect((await line($, 'desktop')).week?.props.color).toBe('error')
  })

test('a gate config.json switched off shows after the week, read again with every measurement', async ($, on) => {
  const folder = 'C:/Users/you/.claude/hooks/usage-clock'
  const gate = `"C:/Python314/python.exe" -X utf8 "${folder}/spawn_gate.py" --mode=enforce || { echo "spawn gate broken - start denied" >&2; exit 2; }`
  let config = JSON.stringify({ modules: { spawn_gate: 'off' } })
  mock.clock(on, { now: Date.parse('2026-10-17T06:00:00Z') })
  mock.env(on, { USERPROFILE: 'C:\\Users\\you' })
  on('session.usage', () => ({ value: USAGE }))
  on('session.id', () => ({ value: 'desk-1' }))
  on('session.surfaces', () => ({ value: ['desktop'] }))
  on('settings.read', () => ({
    value: {
      statusLine: { type: 'command', command: `"C:/Python314/python.exe" -X utf8 "${folder}/statusline.py"` },
      hooks: { PreToolUse: [{ matcher: 'Agent|Workflow|Skill', hooks: [{ type: 'command', command: gate }] }] },
    },
  }))
  on('session.start', ($, e) => e)
  on('session.measure', ($, e) => ({ changed: e.changed }))
  on('fs.read', ($, e) => (e.path.split(String.fromCharCode(92)).join('/').endsWith('/config.json') ? { value: config } : { deny: 'ENOENT' }))
  on('process.run', () => ({ value: { exitCode: 0, stdout: '', stderr: '' } }))
  on('ui.render', { component: 'SessionMode' }, ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text dimColor>bypass</Text>
  })
  on('ui.render', { component: 'AbovePrompt' }, ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text dimColor>engine band</Text>
  })
  const marker = async () => {
    const ui = await mount($, 'desktop', 'band')
    const found = await ui.find({ type: 'Text', text: /spawn gate/ })
    await ui.unmount()
    return found
  }
  await $.session.start({ cwd: 'C:/proj' })
  await $.session.measure({ ...USAGE, changed: ['context', 'rateLimits'] })
  const found = await marker()
  expect(found?.text).toBe(' spawn gate off')
  expect(found?.props.color).toBe('error')
  config = JSON.stringify({ modules: { spawn_gate: '' } })
  await $.session.measure({ ...USAGE, changed: ['context', 'rateLimits'] })
  expect(await marker()).toBeUndefined()
})
