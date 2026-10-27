// The parts of the status line (hooks/statusline.py) for the app, which does not show the statusLine.
// Same texts and the same thresholds; one difference: the cache clock is an estimate (`cache ~54m`), because
// the app tells a plugin no prompt_cache expiry, only when the last answer came.

import type { SessionUsage } from 'claude-code'

import type { LineConfig, WeekAction } from './config'
import { SCRIPT, statuslineArgv, words } from './record'

export const CACHE_TTL_MS = 60 * 60_000   // the one-hour cache of a Claude subscription
// The gates whose teeth modules of config.json can take: key in modules (and name of the script), marker; as
// GATE_MARKERS in hooks/statusline.py
const GATES = [['spawn_gate', 'spawn gate'], ['soft_stop', 'soft stop']] as const
// What the spawn gate does from startBlockWeek on, as WEEK_MARKERS in hooks/statusline.py; none when the brake is off
const WEEK_MARKERS: Record<Exclude<WeekAction, 'off'>, string> = {
  shadow: '<- week gate: shadow only',
  warn: '<- week gate: agents warn',
  ask: '<- week gate: agents ask',
  deny: '<- week gate: agents blocked',
}

export type Color = 'warning' | 'error' | undefined
// The colour of a part below its thresholds in the band above the prompt, which has the room for colour; the footer
// leaves it dim
export type Calm = 'success' | 'suggestion' | 'claude'
// attached: follows the part before it with a space, not with ' | ' (the weekly gate marker)
export type Part = { text: string; color: Color; calm?: Calm; attached?: true }

/** Half to even, like '%.0f' in the status line: 74.5 → 74, 75.5 → 76. */
export function rounded(value: number): number {
  const down = Math.floor(value)
  const rest = value - down
  return rest > 0.5 || (rest === 0.5 && down % 2 !== 0) ? down + 1 : down
}

function level(value: number, yellow: number, red: number): Color {
  return value >= red ? 'error' : value >= yellow ? 'warning' : undefined
}

/** ` ↻19:10` in the given zone (the system's when absent), with the weekday ` ↻Mon 15:00` unless the reset is later
 *  today (the app cuts its footer: a weekday only where it says something); '' when past or broken. */
export function resetText(iso: string | undefined, now: number, withDay: boolean, timeZone?: string): string {
  const moment = iso === undefined ? NaN : Date.parse(iso)
  if (!Number.isFinite(moment) || moment <= now) return ''
  const time = new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', hourCycle: 'h23', timeZone })
    .format(moment)
  const date = new Intl.DateTimeFormat('en-CA', { year: 'numeric', month: '2-digit', day: '2-digit', timeZone })
  const isToday = date.format(moment) === date.format(now)
  const day = withDay && !isToday ? new Intl.DateTimeFormat('en-US', { weekday: 'short', timeZone }).format(moment) + ' ' : ''
  return ` ↻${day}${time}`
}

function object(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? (value as Record<string, unknown>) : {}
}

/** A path as install.py's points_to compares it on Windows: forward slashes, any case. */
const spelling = (path: string): string => path.split('\\').join('/').toLowerCase()

/** `spawn gate off`, `soft stop shadow`, as hooks/statusline.py shows them: a gate installed next to the status line
 *  of `settings` (its PreToolUse entry runs the script in that folder) that modules of config.json (`configText`)
 *  switches off, or sets to shadow while the entry says --mode=enforce. A missing or broken file gives none. */
export function gateMarkers(configText: string | undefined, settings: unknown): string[] {
  let modules: Record<string, unknown>
  try {
    modules = object(object(JSON.parse(configText ?? '{}')).modules)
  } catch {
    return []
  }
  const script = statuslineArgv(object(object(settings).statusLine).command)?.find(word => word.split(/[\\/]/).pop() === SCRIPT)
  if (script === undefined) return []
  const folder = script.slice(0, script.length - SCRIPT.length)
  const groups = object(object(settings).hooks).PreToolUse
  const entries = (Array.isArray(groups) ? groups : []).flatMap(group => {
    const hooks = object(group).hooks
    return Array.isArray(hooks) ? hooks.map(hook => object(hook).command) : []
  }).flatMap(command => (typeof command === 'string' ? [words(command, true) ?? []] : []))
  const result: string[] = []
  for (const [key, label] of GATES) {
    const value = modules[key]
    if (value !== 'off' && value !== 'shadow') continue
    const path = spelling(`${folder}${key}.py`)
    const entry = entries.find(found => found.some(word => spelling(word) === path))
    // modules takes the teeth; the entry's own --mode no longer hides it
    if (entry !== undefined) result.push(`${label} ${value}`)
  }
  return result
}

/** `markers` are those of gateMarkers: they follow the week, as in the status line. */
export function parts(usage: SessionUsage, cfg: LineConfig, lastAnswer: number | null, now: number,
                      timeZone?: string, markers: readonly string[] = []): Part[] {
  const result: Part[] = []
  const limit = (kind: string) => usage.rateLimits.find(r => r.kind === kind)

  const five = limit('five_hour')
  if (five !== undefined) {
    const number = rounded(five.percentUsed)
    const reset = cfg.showReset ? resetText(five.resetsAt, now, false, timeZone) : ''
    result.push({ text: `5h ${number}%${reset}`, color: level(number, cfg.yellowFrom, cfg.redFrom), calm: 'success' })
  }
  const week = limit('seven_day')
  if (week !== undefined) {
    const number = rounded(week.percentUsed)
    // the weekly reset only from yellow on: below that it is days away and only takes room
    const reset = cfg.showReset && number >= cfg.yellowFrom ? resetText(week.resetsAt, now, true, timeZone) : ''
    result.push({ text: `week ${number}%${reset}`, color: level(number, cfg.yellowFrom, cfg.redFrom), calm: 'success' })
    if (cfg.weekStopMarker && cfg.weekAction !== 'off' && number >= cfg.startBlockWeek) {
      result.push({ text: WEEK_MARKERS[cfg.weekAction], color: 'error', attached: true })
    }
  }
  for (const marker of markers) result.push({ text: marker, color: 'error', attached: true })

  const { tokens, window, percent } = usage.context
  if (window >= 1000) {
    const size = `${Math.floor(window / 1000)}k`
    if (tokens === undefined || !(tokens > 0)) {
      result.push({ text: `ctx –/${size}`, color: undefined, calm: 'suggestion' })
    } else {
      const share = percent === undefined ? '' : ` ${rounded(percent)}%`
      result.push({
        text: `ctx ${Math.floor(tokens / 1000)}k/${size}${share}`,
        color: level(tokens, cfg.contextYellowFromK * 1000, cfg.contextRedFromK * 1000),
        calm: 'suggestion',
      })
    }
  }

  if (lastAnswer !== null) {
    const minutes = Math.ceil((lastAnswer + CACHE_TTL_MS - now) / 60_000)
    result.push(minutes <= 0
      ? { text: 'cache cold', color: 'error' }
      : { text: `cache ~${minutes}m`, color: minutes < cfg.cacheYellowBelowMin ? 'warning' : undefined, calm: 'claude' })
  }

  if (usage.cost !== undefined) {
    result.push({ text: `$${usage.cost.usd.toFixed(2)}`, color: undefined })
  }
  return result
}
