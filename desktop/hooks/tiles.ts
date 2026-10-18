// The usage report as tiles, for the desktop surface: what each tile says, read from the same JSON as the text views
// in report.ts. Pure functions of the report, so that the tests need no engine; pane.tsx draws them. The terminal
// keeps the text views.

import { localTime, pct, pts, tone, tokens, usd } from './report'
import type { Limits, Report, Tone } from './report'

/** The colours of the tiles: a dark ground of their own, so they read the same in a light and a dark app. */
export const LOOK = {
  card: '#1c1914',
  border: '#2a241d',
  accent: '#c47a3a',
  accentMuted: '#8b5a2b',
  text: '#f0ece5',
  muted: '#a09789',
  warning: '#c49a3a',
  error: '#c45a4a',
  ownCard: '#2b1f14',
} as const

export function toneColor(level: Tone | undefined): string {
  return level === 'error' ? LOOK.error : level === 'warning' ? LOOK.warning : LOOK.text
}

/** One bar of a tile: a label, the value beside it, and its length as a part of the longest bar (0 to 1). */
export type Bar = { label: string; value: string; length: number }
/** A key figure: a small label, the figure in large type, and up to three lines under it. */
export type Kpi = {
  id: 'week' | 'five' | 'cost' | 'cache'
  label: string
  value: string
  unit: string
  tone?: Tone
  lines: string[]
  /** The weekly limit used, 0 to 100, drawn as a half circle */
  gauge?: number
  /** The peaks of the 5h windows, 0 to 100, drawn as columns */
  peaks?: number[]
  /** Main and subagents as parts of the cost, 0 to 1 */
  split?: number[]
}
export type Badge = { tag: string; text: string }
export type OverviewTiles = { title: string; kpis: Kpi[]; projects: Bar[]; more: string | null; models: Bar[]; agents: Bar[]; notable: Badge[] }

const rec = (x: unknown): Record<string, unknown> =>
  typeof x === 'object' && x !== null && !Array.isArray(x) ? (x as Record<string, unknown>) : {}
const list = (x: unknown): unknown[] => (Array.isArray(x) ? x : [])
const num = (x: unknown): number | null => (typeof x === 'number' && Number.isFinite(x) ? x : null)
const str = (x: unknown): string | null => (typeof x === 'string' ? x : null)

/** Bars from names and amounts, the largest first and as long as the tile; `value` writes the figure beside each. */
function bars(entries: [string, number, string][]): Bar[] {
  const longest = Math.max(0, ...entries.map(([, amount]) => amount))
  return [...entries]
    .sort((a, b) => b[1] - a[1])
    .map(([label, amount, value]) => ({ label, value, length: longest > 0 ? amount / longest : 0 }))
}

/** `Mon 15:00` from the seconds since 1970 the report gives a reset in. */
function resetTime(seconds: number): string {
  const text = localTime(new Date(seconds * 1000).toISOString())
  const [day, , , time] = text.split(' ')
  return `${day} ${time}`
}

const TAGS: Record<string, string> = {
  gate_denied: 'GATE',
  cache_hits_low: 'CACHE',
  unexplained_rise: 'RISE',
  five_hour_window: '5H',
}

export function overviewTiles(report: Report, limits: Limits): OverviewTiles {
  const o = rec(report.overview)
  const kpis: Kpi[] = []

  const week = o.week === null || o.week === undefined ? null : rec(o.week)
  const used = week === null ? null : num(week.used_percent)
  const rise = week === null ? null : num(week.rise_points)
  const reset = week === null ? null : num(week.reset_at)
  kpis.push(used === null
    ? { id: 'week', label: 'Weekly limit', value: '-', unit: '', lines: ['no weekly measurement'] }
    : {
        id: 'week', label: 'Weekly limit', value: `${Math.round(used)}`, unit: '%', tone: tone(used, limits), gauge: used,
        lines: [
          ...(rise === null ? [] : [`+${pts(rise)} pts in this period`]),
          ...(reset === null ? [] : [`reset ${resetTime(reset)}`]),
        ],
      })

  const five = rec(o.five_hour)
  const peak = num(five.highest_percent)
  const windows = num(five.windows) ?? 0
  const at = str(five.highest_at)
  const peaks = list(rec(report.subscription).five_windows).map(w => num(rec(w).peak)).filter((p): p is number => p !== null)
  kpis.push(windows === 0 || peak === null
    ? { id: 'five', label: '5h windows', value: '-', unit: '', lines: ['none measured'] }
    : {
        id: 'five', label: '5h windows', value: `${Math.round(peak)}`, unit: '% highest', tone: tone(peak, limits),
        ...(peaks.length > 0 ? { peaks } : {}),
        lines: [`${windows} window${windows === 1 ? '' : 's'}${at === null ? '' : ` · peak ${localTime(at)}`}`],
      })

  const cost = num(o.cost_usd)
  const split = rec(o.split)
  const main = num(split.main_usd)
  const subagents = num(split.subagents_usd)
  const both = main !== null && subagents !== null && main + subagents > 0
  kpis.push({
    id: 'cost', label: 'Cost at API prices', value: usd(cost), unit: 'USD',
    ...(both ? { split: [main / (main + subagents), subagents / (main + subagents)] } : {}),
    lines: both ? [`main ${usd(main)} · agents ${usd(subagents)}`] : [],
  })

  const startup = o.startup === null || o.startup === undefined ? null : rec(o.startup)
  const mainStart = startup === null ? null : num(startup.main_median_tokens)
  const thinking = num(o.thinking_share)
  const cold = num(rec(report.total).cold_starts)
  kpis.push({
    id: 'cache', label: 'Cache', value: pct(num(o.cache_hit_rate), '-').replace(' %', ''), unit: '% hits',
    lines: [
      ...(cold === null ? [] : [`cold starts ${cold}`]),
      ...(thinking === null ? [] : [`thinking ${pct(thinking)}`]),
      ...(mainStart === null ? [] : [`start ${tokens(mainStart)} (median)`]),
    ],
  })

  const projects = list(o.projects).map(rec).slice(0, num(o.max_projects) ?? 5)
  const more = o.more === null || o.more === undefined ? null : rec(o.more)

  const total = rec(report.total)
  const byType = Object.entries(rec(rec(total.subagents).by_type)).map(([name, entry]) => [name, rec(entry)] as const)
  const mainEntry = total.main === null || total.main === undefined ? null : rec(total.main)

  return {
    title: str(o.title) ?? str(rec(report.period).label) ?? 'Report',
    kpis,
    projects: bars(projects.map(entry => {
      const amount = num(entry.cost_usd) ?? 0
      return [str(entry.name) ?? '?', amount, `${usd(amount)} · ${pts(num(entry.week_points))}`]
    })),
    more: more === null ? null : `${num(more.projects) ?? '?'} more · ${usd(num(more.cost_usd))} USD · ${num(more.sessions) ?? '?'} sessions`,
    models: bars(list(rec(o.models).families).map(rec).map(family => {
      const amount = num(family.cost_usd) ?? 0
      return [str(family.name) ?? '?', amount, usd(amount)]
    })),
    agents: bars([
      ...(mainEntry === null ? [] : [['main', num(mainEntry.cost_usd) ?? 0, usd(num(mainEntry.cost_usd))] as [string, number, string]]),
      ...byType.map(([name, entry]): [string, number, string] => [name, num(entry.cost_usd) ?? 0, usd(num(entry.cost_usd))]),
    ]).slice(0, 4),
    notable: list(o.notable).map(rec)
      .filter(entry => str(entry.text) !== null)
      .map(entry => ({ tag: TAGS[str(entry.rule) ?? ''] ?? 'NOTE', text: str(entry.text) as string })),
  }
}

/** One session as a card of the Sessions view; the session the pane was opened from comes first, marked `isOwn`. */
export type SessionCard = { id: string; title: string; cost: string; meta: string; isOwn: boolean }

export function sessionCards(report: Report, own: string | null): SessionCard[] {
  const cards = list(report.clis).map(rec).filter(cli => str(cli.id) !== null).map((cli): SessionCard => {
    const id = str(cli.id) as string
    const start = str(cli.start)
    const hits = num(cli.cache_hit_rate)
    return {
      id,
      title: `${str(cli.label) ?? '?'} ${id}`,
      cost: `${usd(num(cli.cost_usd))} USD`,
      meta: [
        start === null ? null : localTime(start),
        `${num(cli.requests) ?? '-'} requests`,
        hits === null ? null : `cache ${pct(hits)}`,
        `~${pts(num(rec(cli.estimated_share).week))} week pts`,
      ].filter(Boolean).join(' · '),
      isOwn: id === own,
    }
  })
  return [...cards.filter(card => card.isOwn), ...cards.filter(card => !card.isOwn)]
}

/** How many tiles stand side by side in a body `columns` cells wide: the pane can be dragged wider or narrower. */
export function tilesPerRow(columns: number): 1 | 2 | 4 {
  return columns >= 96 ? 4 : columns >= 44 ? 2 : 1
}

/** The weekly limit as a half circle: the used part in the colour of its threshold, a mark where red starts. */
export function gaugeSvg(percent: number, limits: Limits): string {
  const point = (p: number) => {
    const angle = Math.PI * (1 - Math.min(Math.max(p, 0), 100) / 100)
    return `${(60 + 48 * Math.cos(angle)).toFixed(2)} ${(60 - 48 * Math.sin(angle)).toFixed(2)}`
  }
  const level = tone(percent, limits)
  const color = level === undefined ? LOOK.accent : toneColor(level)
  const mark = Math.PI * (1 - limits.redFrom / 100)
  const inner = { x: 60 + 41 * Math.cos(mark), y: 60 - 41 * Math.sin(mark) }
  const outer = { x: 60 + 55 * Math.cos(mark), y: 60 - 55 * Math.sin(mark) }
  return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 66" width="120" height="66">'
    + `<path d="M12 60 A48 48 0 0 1 108 60" fill="none" stroke="${LOOK.border}" stroke-width="10" stroke-linecap="round"/>`
    + (percent > 0 ? `<path d="M12 60 A48 48 0 0 1 ${point(percent)}" fill="none" stroke="${color}" stroke-width="10" stroke-linecap="round"/>` : '')
    + `<line x1="${inner.x.toFixed(2)}" y1="${inner.y.toFixed(2)}" x2="${outer.x.toFixed(2)}" y2="${outer.y.toFixed(2)}" stroke="${LOOK.warning}" stroke-width="2"/>`
    + `<text x="60" y="58" text-anchor="middle" font-family="monospace" font-size="22" font-weight="700" fill="${LOOK.text}">${Math.round(percent)}%</text>`
    + '</svg>'
}

/** The peaks of the 5h windows as columns on a scale of 0 to 100 %, the highest in the accent colour. */
export function peaksSvg(peaks: number[]): string {
  const shown = peaks.slice(-8)
  const step = 120 / Math.max(shown.length, 1)
  const highest = Math.max(...shown)
  const columns = shown.map((peak, i) => {
    const height = Math.max(1.5, (Math.min(Math.max(peak, 0), 100) / 100) * 44)
    return `<rect x="${(i * step + step * 0.15).toFixed(2)}" y="${(45 - height).toFixed(2)}" width="${(step * 0.7).toFixed(2)}" `
      + `height="${height.toFixed(2)}" rx="2" fill="${peak === highest ? LOOK.accent : LOOK.accentMuted}"/>`
  }).join('')
  return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 46" width="120" height="46">'
    + `<line x1="0" y1="45.5" x2="120" y2="45.5" stroke="${LOOK.border}" stroke-width="1"/>${columns}</svg>`
}
