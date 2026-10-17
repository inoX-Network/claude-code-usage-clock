import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import { DEFAULTS, expandHome, parseConfig } from './config'
import { parts } from './line'

const usage = atom({ plugin: 'usage-clock-display', key: 'usage' } as const, null)
const config = atom({ plugin: 'usage-clock-display', key: 'config' } as const, DEFAULTS)
const lastAnswer = atom({ plugin: 'usage-clock-display', key: 'lastAnswer' } as const, null)
// Counts up every minute, so the cache clock moves on without a new answer
const tick = atom({ plugin: 'usage-clock-display', key: 'tick' } as const, 0)

export const register: Register = (on, options) => {
  on('session.start', async ($, e, next) => {
    const home = (await $.env.get('USERPROFILE')) ?? (await $.env.get('HOME'))
    const path = expandHome(String(options.config_file ?? ''), home)
    // A missing or unreadable config.json means the defaults, as for the hooks
    const text = path === '' ? undefined : await $.fs.read(path).catch(() => undefined)
    await update($, config, () => parseConfig(text))
    await update($, usage, () => null)
    const now = await $.session.usage()
    await update($, usage, () => now)
    $.clock.every(60_000, () => update($, tick, n => n + 1))
    return next(e)
  })

  on('session.measure', async ($, e, next) => {
    const now = await $.session.usage()
    await update($, usage, () => now)
    return next(e)
  })

  // Only the main loop renews this session's cache; subagents have their own
  on('turn.complete', async ($, e, next) => {
    if (e.agentId === undefined) {
      const now = await $.clock.now()
      await update($, lastAnswer, () => now)
    }
    return next(e)
  })

  on('ui.render', { component: 'SessionMode' }, async ($, e, next) => {
    // The terminal has the status line already
    if (e.surface === 'terminal') return next(e)
    const measured = await read($, usage)
    if (measured === null) return next(e)
    await read($, tick)
    const list = parts(measured, await read($, config), await read($, lastAnswer), await $.clock.now())
    if (list.length === 0) return next(e)

    const { Box, Text } = $.ui.resolve(e)
    return (
      <Box>
        {await next(e)}
        {list.map((part, i) => (
          <Text key={`p${i}`} color={part.color} dimColor={part.color === undefined}>
            {i === 0 || part.attached ? ' ' : ' | '}
            {part.text}
          </Text>
        ))}
      </Box>
    )
  })
}
