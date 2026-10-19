// The usage report as tiles, for the desktop surface: what each tile says, read from the same JSON as the text views
// in report.ts. Pure functions of the report, so that the tests need no engine; pane.tsx draws them. The terminal
// keeps the text views.

import { localTime, pct, pts, tone, tokens, usd, whole } from './report'
import type { Limits, Report, Tone, View } from './report'

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
  id: string
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

// --- The other views ----------------------------------------------------------------------------------------------

/** A block of a view: a row of key figures, a tile of bars (with a line above them and a note under them), or a note. */
export type Tile =
  | { kind: 'kpis'; key: string; kpis: Kpi[] }
  | { kind: 'bars'; key: string; label: string; lead?: string; bars: Bar[]; note?: string }
  | { kind: 'note'; key: string; text: string }

/** Tokens in short: `78.9M`, `12k`, or the number itself below 10 000. */
const compact = (x: number | null): string =>
  x === null ? '-' : x >= 1_000_000 ? `${(x / 1_000_000).toFixed(1)}M` : tokens(x)

const ESTIMATE = 'Points (~) are estimates: a share of the measured rise, divided by cost.'
const TOP = 10

/** Bars of names and token counts, the `TOP` largest, then one bar for the rest: last, though its sum may be larger. */
function topBars(entries: [string, number][]): Bar[] {
  const sorted = [...entries].sort((a, b) => b[1] - a[1])
  const shown = bars(sorted.slice(0, TOP).map(([name, count]): [string, number, string] => [name, count, whole(count)]))
  const rest = sorted.slice(TOP)
  if (rest.length === 0) return shown
  const sum = rest.reduce((total, [, count]) => total + count, 0)
  const longest = sorted[0]?.[1] ?? 0
  return [...shown, { label: `${rest.length} more`, value: whole(sum), length: longest > 0 ? Math.min(1, sum / longest) : 0 }]
}

function tokenKinds(t: Record<string, unknown>): [string, number][] {
  return ([['input', t.input], ['cache write 5m', t.cache_write_5m], ['cache write 1h', t.cache_write_1h],
    ['cache read', t.cache_read], ['output', t.output]] as [string, unknown][])
    .map(([name, count]): [string, number] => [name, num(count) ?? 0])
}

function agentTiles(report: Report): Tile[] {
  const total = rec(report.total)
  const main = total.main === null || total.main === undefined ? null : rec(total.main)
  const subagents = rec(total.subagents)
  const byType = Object.entries(rec(subagents.by_type)).map(([name, entry]) => [name, rec(entry)] as const)
  if (main === null && byType.length === 0) return [{ kind: 'note', key: 'none', text: 'No agent data in this report.' }]
  const missing = list(rec(report.coverage).archive_days_without_agent_split).length
  const row = (name: string, entry: Record<string, unknown>): [string, number, string] =>
    [name, num(entry.cost_usd) ?? 0, `${usd(num(entry.cost_usd))} · ${whole(num(entry.requests))} · ${pts(num(entry.week_points))}`]
  return [
    { kind: 'kpis', key: 'split', kpis: [
      ...(main === null ? [] : [{ id: 'main', label: 'Main', value: usd(num(main.cost_usd)), unit: 'USD', lines: [
        `${whole(num(main.requests))} requests`, `~${pts(num(main.week_points))} week · ~${pts(num(main.five_points))} 5h pts`,
      ] }]),
      { id: 'subagents', label: 'Subagents', value: usd(num(subagents.cost_usd)), unit: 'USD', lines: [
        `${whole(num(subagents.requests))} requests`, `${byType.length} type${byType.length === 1 ? '' : 's'}`,
      ] },
    ] },
    { kind: 'bars', key: 'agents', label: 'By agent · USD · requests · ~week pts',
      bars: bars([...(main === null ? [] : [row('main', main)]), ...byType.map(([name, entry]) => row(name, entry))]),
      note: missing > 0
        ? `${ESTIMATE} ${missing} archived day${missing === 1 ? '' : 's'} without a split by agent type: their points are missing here.`
        : ESTIMATE },
  ]
}

function modelTiles(report: Report): Tile[] {
  const total = rec(report.total)
  const priced = Object.entries(rec(total.models)).map(([name, entry]) => [name, rec(entry)] as const)
    .sort((a, b) => (num(b[1].cost_usd) ?? 0) - (num(a[1].cost_usd) ?? 0))
  const unpriced = Object.entries(rec(total.unpriced)).map(([name, entry]) => [name, rec(entry)] as const)
  if (priced.length + unpriced.length === 0) return [{ kind: 'note', key: 'none', text: 'No model data in this report.' }]
  const kpi = (name: string, entry: Record<string, unknown>, hasPrice: boolean): Kpi => ({
    id: `model-${name}`, label: name, value: hasPrice ? usd(num(entry.cost_usd)) : '-', unit: 'USD', lines: [
      `${whole(num(entry.requests))} requests`,
      `thinking ${pct(num(entry.thinking_share), 'n/a')}`,
      `cache hits ${pct(num(entry.cache_hit_rate), 'n/a')}`,
    ],
  })
  return [
    { kind: 'kpis', key: 'models', kpis: [...priced.map(([name, entry]) => kpi(name, entry, true)), ...unpriced.map(([name, entry]) => kpi(name, entry, false))] },
    ...(priced.length < 2 ? [] : [{ kind: 'bars' as const, key: 'cost', label: 'Share of the cost · USD',
      bars: bars(priced.map(([name, entry]): [string, number, string] => [name, num(entry.cost_usd) ?? 0, usd(num(entry.cost_usd))])) }]),
    ...(unpriced.length === 0 ? [] : [{ kind: 'note' as const, key: 'unpriced', text: 'A model without a price has no USD: its tokens are counted, never estimated.' }]),
  ]
}

function cacheTiles(report: Report): Tile[] {
  const total = rec(report.total)
  const hits = num(total.cache_hit_rate)
  const thinking = num(total.thinking_share)
  return [
    { kind: 'kpis', key: 'cache', kpis: [
      { id: 'hits', label: 'Cache hit rate', value: hits === null ? 'n/a' : pct(hits).replace(' %', ''), unit: hits === null ? '' : '%', lines: ['cache reads of all input'] },
      { id: 'cold', label: 'Cold starts', value: whole(num(total.cold_starts)), unit: '', lines: ['≥ 50,000 tokens written after the first request'] },
      { id: 'thinking-tokens', label: 'Thinking tokens', value: compact(num(total.thinking_tokens)), unit: '',
        lines: num(total.thinking_tokens) === null ? ['n/a'] : [whole(num(total.thinking_tokens))] },
      { id: 'thinking-share', label: 'Thinking share', value: thinking === null ? 'n/a' : pct(thinking).replace(' %', ''),
        unit: thinking === null ? '' : '% of output', lines: ['thinking is part of the output tokens'] },
    ] },
    { kind: 'note', key: 'explain', text: 'Cache hit rate: cache reads divided by all input. Cold start: a request of a main session, after its first, '
      + 'that wrote at least 50,000 tokens to the cache. Thinking is part of the output tokens.' },
  ]
}

function tokenTiles(report: Report): Tile[] {
  const t = rec(rec(report.total).tokens)
  if (Object.keys(t).length === 0) return [{ kind: 'note', key: 'none', text: 'No token data in this report.' }]
  const kinds = tokenKinds(t)
  return [
    { kind: 'kpis', key: 'tokens', kpis: kinds.map(([name, count]) => ({ id: `tokens-${name}`, label: name, value: compact(count), unit: 'tokens', lines: [whole(count)] })) },
    { kind: 'bars', key: 'kinds', label: 'Tokens by kind', bars: bars(kinds.map(([name, count]): [string, number, string] => [name, count, compact(count)])) },
  ]
}

function projectTiles(report: Report): Tile[] {
  const entries = list(rec(report.overview).projects).map(rec)
  if (entries.length === 0) return [{ kind: 'note', key: 'none', text: 'No projects in this period.' }]
  return [{
    kind: 'bars', key: 'projects', label: 'Projects · USD · share · ~week pts · sessions',
    bars: bars(entries.map((entry): [string, number, string] => [
      str(entry.name) ?? '?', num(entry.cost_usd) ?? 0,
      `${usd(num(entry.cost_usd))} · ${pct(num(entry.share))} · ${pts(num(entry.week_points))} · ${whole(num(entry.sessions))}`,
    ])),
    note: '~week pts is an estimate: a share of the measured rise, divided by cost.',
  }]
}

/** The tiles of a view other than the Overview and the list of Sessions, which pane.tsx draws itself. */
export function viewTiles(view: View, report: Report): Tile[] {
  switch (view) {
    case 'agents': return agentTiles(report)
    case 'models': return modelTiles(report)
    case 'cache': return cacheTiles(report)
    case 'tokens': return tokenTiles(report)
    case 'projects': return projectTiles(report)
    case 'overview':
    case 'sessions': return list(report.clis).length === 0 && view === 'sessions' ? [{ kind: 'note', key: 'none', text: 'No sessions in this period.' }] : []
  }
}

/** The detail of one session as tiles: its figures, tokens, models, subagents, and what its context held. */
export function sessionTiles(report: Report, id: string): Tile[] {
  const cli = list(report.clis).map(rec).find(entry => str(entry.id) === id)
  if (cli === undefined) return [{ kind: 'note', key: 'gone', text: 'This session is not in the report any more. Press q for the list.' }]
  const share = rec(cli.estimated_share)
  const thinking = num(cli.thinking_share)
  const out: Tile[] = [
    { kind: 'kpis', key: 'figures', kpis: [
      { id: 'cost', label: 'Cost', value: usd(num(cli.cost_usd)), unit: 'USD', lines: [`~${pts(num(share.week))} week · ~${pts(num(share.five))} 5h pts`] },
      { id: 'requests', label: 'Requests', value: whole(num(cli.requests)), unit: '', lines: [] },
      { id: 'cache', label: 'Cache', value: pct(num(cli.cache_hit_rate), 'n/a').replace(' %', ''), unit: num(cli.cache_hit_rate) === null ? '' : '% hits',
        lines: [`cold starts ${whole(num(cli.cold_starts))}`] },
      { id: 'thinking', label: 'Thinking', value: thinking === null ? 'n/a' : pct(thinking).replace(' %', ''), unit: thinking === null ? '' : '% of output', lines: [] },
    ] },
    { kind: 'bars', key: 'tokens', label: 'Tokens by kind', bars: bars(tokenKinds(rec(cli.tokens)).map(([name, count]): [string, number, string] => [name, count, whole(count)])) },
  ]
  const models = Object.entries(rec(cli.models)).map(([name, cost]): [string, number, string] => [name, num(cost) ?? 0, usd(num(cost))])
  if (models.length > 0) out.push({ kind: 'bars', key: 'models', label: 'Models · USD', bars: bars(models) })
  const subagents = Object.entries(rec(cli.subagents)).map(([name, entry]): [string, number, string] =>
    [name, num(rec(entry).cost_usd) ?? 0, `${usd(num(rec(entry).cost_usd))} · ${whole(num(rec(entry).requests))} req`])
  if (subagents.length > 0) out.push({ kind: 'bars', key: 'subagents', label: 'Subagents · USD · requests', bars: bars(subagents) })

  const context = cli.context === null || cli.context === undefined ? null : rec(cli.context)
  if (context === null) {
    out.push({ kind: 'note', key: 'no-context', text: 'No context recorded for this session: the plugin writes it down from version 0.4.0 on, after the first answer.' })
    return out
  }
  const totalTokens = num(context.total_tokens)
  const window = num(context.max_tokens)
  const lead = `Context after the first answer: ${totalTokens === null ? '-' : whole(totalTokens)} tokens`
    + (totalTokens !== null && window !== null && window > 0 ? ` of ${whole(window)} (${pct(totalTokens / window)})` : '')
  const categories = list(context.categories).map(rec).filter(row => str(row.kind) !== 'free' && str(row.kind) !== 'buffer')
  const named = (rows: Record<string, unknown>[]): [string, number][] => rows.map(row => [str(row.name) ?? '?', num(row.tokens) ?? 0])
  const used = categories.filter(row => str(row.kind) !== 'deferred')
  const deferred = categories.filter(row => str(row.kind) === 'deferred')
  out.push({ kind: 'bars', key: 'context', label: 'Context · tokens', lead, bars: topBars(named(used)) })
  if (deferred.length > 0) out.push({ kind: 'bars', key: 'deferred', label: 'Deferred, not in the context', bars: topBars(named(deferred)) })
  const tools = list(context.mcp_tools).map(rec)
  if (tools.length > 0) {
    const servers = new Map<string, { tools: number; deferred: number; tokens: number }>()
    for (const tool of tools) {
      const name = str(tool.server) ?? '?'
      const sum = servers.get(name) ?? { tools: 0, deferred: 0, tokens: 0 }
      servers.set(name, { tools: sum.tools + 1, deferred: sum.deferred + (tool.loaded === false ? 1 : 0), tokens: sum.tokens + (num(tool.tokens) ?? 0) })
    }
    const label = (name: string, sum: { tools: number; deferred: number }) =>
      `${name} (${sum.tools} tool${sum.tools === 1 ? '' : 's'}${sum.deferred === 0 ? '' : sum.deferred === sum.tools ? ', deferred' : `, ${sum.deferred} deferred`})`
    out.push({ kind: 'bars', key: 'mcp-servers', label: 'MCP servers · tokens', bars: topBars([...servers].map(([name, sum]) => [label(name, sum), sum.tokens])) })
    out.push({ kind: 'bars', key: 'mcp-tools', label: 'MCP tools · tokens', bars: topBars(tools.map(tool => [`${str(tool.server) ?? '?'} ${str(tool.name) ?? '?'}`, num(tool.tokens) ?? 0])) })
  }
  const memory = list(context.memory_files).map(rec)
  if (memory.length > 0) out.push({ kind: 'bars', key: 'memory', label: 'Memory files · tokens', bars: topBars(memory.map(file => [str(file.path) ?? '?', num(file.tokens) ?? 0])) })
  const skills = context.skills === null || context.skills === undefined ? null : rec(context.skills)
  if (skills !== null) {
    out.push({ kind: 'bars', key: 'skills', label: 'Skills · tokens',
      lead: `Skills: ${whole(num(skills.tokens))} tokens, ${whole(num(skills.included))} of ${whole(num(skills.total))} listed`,
      bars: topBars(list(skills.items).map(rec).map(item => [str(item.name) ?? '?', num(item.tokens) ?? 0])) })
  }
  const agents = list(context.agents).map(rec)
  if (agents.length > 0) out.push({ kind: 'bars', key: 'context-agents', label: 'Agents · tokens', bars: topBars(agents.map(agent => [str(agent.type) ?? '?', num(agent.tokens) ?? 0])) })
  return out
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
