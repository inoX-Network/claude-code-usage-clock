import { expect, mock, test } from 'claude-code/testing'

const USAGE = {
  startedAt: 0,
  context: { tokens: 340_000, window: 1_000_000, percent: 34 },
  rateLimits: [{ kind: 'seven_day', percentUsed: 72 }],
}
const CONFIG = JSON.stringify({ statusline: { yellow_from: 70, red_from: 85 }, thresholds: { start_block_week: 90 } })

function world(on: Parameters<Parameters<typeof test>[1]>[1], files: Record<string, string>, read: string[]) {
  mock.clock(on, { now: Date.parse('2026-10-17T06:00:00Z') })
  mock.env(on, { USERPROFILE: 'C:\\Users\\you' })
  on('session.usage', () => ({ value: USAGE }))
  on('session.start', ($, e) => e)
  on('session.measure', ($, e) => ({ changed: e.changed }))
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
}

async function line($: Parameters<Parameters<typeof test>[1]>[0], surface: 'terminal' | 'desktop') {
  const ui = await $.ui.mount({ plugin: 'usage-clock-display', surface, component: 'SessionMode', props: { modes: [] } })
  const found = {
    own: await ui.find({ type: 'Text', text: /bypass/ }),
    week: await ui.find({ type: 'Text', text: /week 72%/ }),
    ctx: await ui.find({ type: 'Text', text: /ctx 340k\/1000k 34%/ }),
    marker: await ui.find({ type: 'Text', text: /weekly limit reached/ }),
  }
  await ui.unmount()
  return found
}

test('the desktop app gets the line, coloured by the usage-clock config', async ($, on) => {
  const read: string[] = []
  world(on, { 'C:/Users/you/.claude/hooks/usage-clock/config.json': CONFIG }, read)
  await $.session.start({ cwd: 'C:/proj' })
  await $.session.measure({ ...USAGE, changed: ['context', 'rateLimits'] })
  expect(read).toEqual(['C:/Users/you/.claude/hooks/usage-clock/config.json'])
  const found = await line($, 'desktop')
  expect(found.own).toBeDefined()
  expect(found.week?.props.color).toBe('warning')   // 72 % is yellow from 70 on, red only from 85
  expect(found.ctx?.props.color).toBe('warning')
  expect(found.marker).toBeUndefined()                // 72 % is below start_block_week 90
})

test('the weekly stop marker follows the week with a space, like the status line', async ($, on) => {
  world(on, { 'C:/Users/you/.claude/hooks/usage-clock/config.json': JSON.stringify({ thresholds: { start_block_week: 72 } }) }, [])
  await $.session.start({ cwd: 'C:/proj' })
  await $.session.measure({ ...USAGE, changed: ['context', 'rateLimits'] })
  const found = await line($, 'desktop')
  expect(found.marker?.text).toBe(' <- weekly limit reached')
  expect(found.ctx?.text).toBe(' | ctx 340k/1000k 34%')
})

test('the terminal keeps its own footer: the status line shows the same there', async ($, on) => {
  world(on, {}, [])
  await $.session.start({ cwd: 'C:/proj' })
  await $.session.measure({ ...USAGE, changed: ['context', 'rateLimits'] })
  const found = await line($, 'terminal')
  expect(found.own).toBeDefined()
  expect(found.week).toBeUndefined()
  expect(found.ctx).toBeUndefined()
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
    expect(read).toHaveLength(1)
    expect(read[0]?.endsWith('/etc/usage-clock/config.json')).toBe(true)
    expect((await line($, 'desktop')).week?.props.color).toBe('error')
  })
