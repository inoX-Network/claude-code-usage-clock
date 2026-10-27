// The thresholds of the status line, read from the usage-clock config.json, so the app and the terminal
// colour at the same numbers. The defaults are those of hooks/_config.py (config.example.json).

import type { DesktopLine, LineConfig, WeekAction } from '../types/index'

export type { DesktopLine, LineConfig, WeekAction }

export const DEFAULTS: LineConfig = {
  yellowFrom: 50,
  redFrom: 75,
  weekStopMarker: true,
  showReset: true,
  contextYellowFromK: 300,
  contextRedFromK: 500,
  cacheYellowBelowMin: 5,
  startBlockWeek: 75,
  weekAction: 'warn',
  desktopLine: 'full',
}

const WEEK_ACTIONS: readonly WeekAction[] = ['off', 'shadow', 'warn', 'ask', 'deny']
const DESKTOP_LINES: readonly DesktopLine[] = ['off', 'short', 'full']

// config.json key → field, per section
const STATUSLINE: Record<string, keyof LineConfig> = {
  yellow_from: 'yellowFrom',
  red_from: 'redFrom',
  week_stop_marker: 'weekStopMarker',
  show_reset: 'showReset',
  context_yellow_from_k: 'contextYellowFromK',
  context_red_from_k: 'contextRedFromK',
  cache_yellow_below_min: 'cacheYellowBelowMin',
}
const THRESHOLDS: Record<string, keyof LineConfig> = { start_block_week: 'startBlockWeek' }

function section(given: unknown): Record<string, unknown> {
  return typeof given === 'object' && given !== null && !Array.isArray(given) ? (given as Record<string, unknown>) : {}
}

/** Like _config.py: every valid key overrides its default, a missing or broken one keeps it. */
export function parseConfig(text: string | undefined): LineConfig {
  const result: LineConfig = { ...DEFAULTS }
  if (text === undefined) return result
  let data: unknown
  try {
    data = JSON.parse(text)
  } catch {
    return result
  }
  const root = section(data)
  for (const [keys, given] of [[STATUSLINE, section(root.statusline)], [THRESHOLDS, section(root.thresholds)]] as const) {
    for (const [key, field] of Object.entries(keys)) {
      const value = given[key]
      if (typeof value === typeof DEFAULTS[field] && (typeof value !== 'number' || Number.isFinite(value))) {
        Object.assign(result, { [field]: value })
      }
    }
  }
  // A string outside the choices keeps the default, as _CHOICES in _config.py
  const action = section(root.thresholds).week_action
  if (WEEK_ACTIONS.includes(action as WeekAction)) result.weekAction = action as WeekAction
  const line = section(root.modules).desktop_line
  if (DESKTOP_LINES.includes(line as DesktopLine)) result.desktopLine = line as DesktopLine
  return result
}

export const LOG_DIR = '~/.cache/claude-usage-clock'

/** The log folder of the hooks, as hooks/_config.py settles it: CCUC_LOG_DIR, else `log_dir` of config.json, else the
 *  default. Not yet expanded. */
export function parseLogDir(text: string | undefined, env: string | undefined): string {
  if (env !== undefined && env !== '') return env
  if (text === undefined) return LOG_DIR
  let data: unknown
  try {
    data = JSON.parse(text)
  } catch {
    return LOG_DIR
  }
  const given = section(data).log_dir
  return typeof given === 'string' && given !== '' ? given : LOG_DIR
}

/** `~/…` against the home directory; anything else as written. */
export function expandHome(path: string, home: string | undefined): string {
  if (home === undefined || !(path === '~' || path.startsWith('~/'))) return path
  return home.replace(/[\\/]+$/, '') + path.slice(1)
}
