import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import { DEFAULTS, LOG_DIR, expandHome, parseConfig, parseLogDir } from './config'
import type { DesktopLine } from './config'
import { recordContext } from './context'
import { gateMarkers, parts } from './line'
import type { Part } from './line'
import { registerReport, registerReportCommand } from './pane'
import { statusInput, statuslineArgv } from './record'

const usage = atom({ plugin: 'usage-clock-display', key: 'usage' } as const, null)
const config = atom({ plugin: 'usage-clock-display', key: 'config' } as const, DEFAULTS)
const lastAnswer = atom({ plugin: 'usage-clock-display', key: 'lastAnswer' } as const, null)
// Counts up every minute, so the cache clock moves on without a new answer
const tick = atom({ plugin: 'usage-clock-display', key: 'tick' } as const, 0)
// Where the hooks log, and whether this session's context is written down yet
const logDir = atom({ plugin: 'usage-clock-display', key: 'logDir' } as const, LOG_DIR)
const isContextRecorded = atom({ plugin: 'usage-clock-display', key: 'isContextRecorded' } as const, false)
// Whether config.json and the log folder are read: /clear starts a session without session.start, its atoms at their
// initials, so the first answer reads them then
const isSettled = atom({ plugin: 'usage-clock-display', key: 'isSettled' } as const, false)
// The gates config.json switched off or down to shadow (gateMarkers), renewed with every measurement in the app
const markers = atom({ plugin: 'usage-clock-display', key: 'markers' } as const, [] as string[])

// The engine follows $ only into functions declared at the top of the file, so the config path comes as an argument
async function settle($: EngineInterface, configFile: string) {
  const home = (await $.env.get('USERPROFILE')) ?? (await $.env.get('HOME'))
  const path = expandHome(configFile, home)
  // A missing or unreadable config.json means the defaults, as for the hooks
  const text = path === '' ? undefined : await $.fs.read(path).catch(() => undefined)
  await update($, config, () => parseConfig(text))
  const logEnv = await $.env.get('CCUC_LOG_DIR')
  await update($, logDir, () => expandHome(parseLogDir(text, logEnv), home))
  await update($, isSettled, () => true)
}

// config.json read again: the Settings tab, the installer or a command may switch a gate or the place of the line while
// the session runs
async function renewConfig($: EngineInterface, configFile: string, settings: unknown) {
  const path = expandHome(configFile, (await $.env.get('USERPROFILE')) ?? (await $.env.get('HOME')))
  const text = path === '' ? undefined : await $.fs.read(path).catch(() => undefined)
  await update($, config, () => parseConfig(text))
  await update($, markers, () => gateMarkers(text, settings))
}

// The parts of the line for a site of the app: none on the terminal, which has the status line, none before the first
// measurement, and none where modules.desktop_line puts it elsewhere
async function lineParts($: EngineInterface, surface: string, site: DesktopLine): Promise<Part[]> {
  if (surface === 'terminal') return []
  const measured = await read($, usage)
  const cfg = await read($, config)
  if (measured === null || cfg.desktopLine !== site) return []
  await read($, tick)
  return parts(measured, cfg, await read($, lastAnswer), await $.clock.now(), undefined, await read($, markers))
}

export const register: Register = (on, options) => {
  registerReport(on)
  const configFile = String(options.config_file ?? '')

  on('session.start', async ($, e, next) => {
    await settle($, configFile)
    await update($, isContextRecorded, () => false)
    await update($, usage, () => null)
    const now = await $.session.usage()
    await update($, usage, () => now)
    $.clock.every(60_000, () => update($, tick, n => n + 1))
    // A refused /report (a built-in of that name, say) only means no report pane; the status line stays
    await registerReportCommand($).catch(() => undefined)
    return next(e)
  })

  on('session.measure', async ($, e, next) => {
    const now = await $.session.usage()
    await update($, usage, () => now)
    const result = await next(e)
    // The terminal runs the status line itself, with the cache clock the app does not have
    if (!(await $.session.surfaces()).includes('terminal')) {
      // The user's own settings.json, as install.py writes it: a project cannot hand the plugin a command to run
      const settings = await $.settings.read({ source: 'user' })
      await renewConfig($, configFile, settings)
      const line = settings.statusLine
      const argv = statuslineArgv(typeof line === 'object' && line !== null ? (line as { command?: unknown }).command : undefined)
      // Claude Code runs no status line while the merged settings say disableAllHooks; neither does the plugin
      if (argv !== null && (await $.settings.read()).disableAllHooks !== true) {
        const stdin = JSON.stringify(statusInput(e, await $.session.id()))
        // The line comes from the plugin; a status line that fails only means no new numbers for the hooks
        await $.process.run(argv, { stdin, timeoutMs: 10_000 }).catch(() => undefined)
      }
    }
    return result
  })

  // Only the main loop renews this session's cache; subagents have their own
  on('turn.complete', async ($, e, next) => {
    if (e.agentId === undefined) {
      const now = await $.clock.now()
      await update($, lastAnswer, () => now)
      // Once per session, after its first answer: what its context holds, for the report. Not awaited: a full count
      // asks once per tool and memory file and must not hold up the next turn; a failure writes nothing
      if (!(await read($, isContextRecorded))) {
        await update($, isContextRecorded, () => true)
        if (!(await read($, isSettled))) await settle($, configFile)
        void recordContext($, await read($, logDir)).catch(() => undefined)
      }
    }
    return next(e)
  })

  // full: a row of its own above the prompt, coloured below the thresholds too, over what other plugins draw there; it
  // gives way to a survey
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey) return next(e)
    const list = await lineParts($, e.surface, 'full')
    if (list.length === 0) return next(e)

    const { Box, Text } = $.ui.resolve(e)
    return (
      <Box flexDirection="column">
        <Box>
          {list.map((part, i) => (
            <Text key={`p${i}`} color={part.color ?? part.calm} dimColor={part.color === undefined && part.calm === undefined}>
              {i === 0 ? '' : part.attached ? ' ' : ' | '}
              {part.text}
            </Text>
          ))}
        </Box>
        {await next(e)}
      </Box>
    )
  })

  // short: in the footer beside the model, which a narrow window cuts
  on('ui.render', { component: 'SessionMode' }, async ($, e, next) => {
    const list = await lineParts($, e.surface, 'short')
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
