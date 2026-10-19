// The usage report in the pane: which command produces it, how its JSON is read, and what each view says. Pure
// functions of text and numbers, so that the tests need no engine. The fields are those of docs/report-format.md;
// a field that is missing or null is left out or shown as n/a, never a failure: the report may gain fields.

import type { ReportPeriod, ReportView } from '../types/index'
import { SCRIPT as STATUSLINE, statuslineArgv } from './record'

const REPORT_SCRIPT = 'usage_report.py'

export const PERIODS: readonly { id: ReportPeriod; hotkey: string; label: string }[] = [
  { id: 'day', hotkey: '1', label: 'Day' },
  { id: 'week', hotkey: '2', label: 'Week' },
  { id: 'month', hotkey: '3', label: 'Month' },
  { id: 'year', hotkey: '4', label: 'Year' },
]

export const VIEWS: readonly { id: ReportView; hotkey: string; label: string }[] = [
  { id: 'overview', hotkey: 'a', label: 'Overview' },
  { id: 'agents', hotkey: 'b', label: 'Agents' },
  { id: 'models', hotkey: 'c', label: 'Models' },
  { id: 'cache', hotkey: 'd', label: 'Cache/Thinking' },
  { id: 'tokens', hotkey: 'e', label: 'Tokens' },
  { id: 'projects', hotkey: 'f', label: 'Projects' },
  { id: 'sessions', hotkey: 'g', label: 'Sessions' },
]

export type Period = ReportPeriod
export type View = ReportView
export type Report = Record<string, unknown>

export type Tone = 'warning' | 'error' | 'claude'
/** A piece of a row with a colour of its own: a percentage of a limit past a threshold, the mark of the own session. */
export type Part = { text: string; tone?: Tone }
/** One row of a view: plain text for a monospaced Text, in a style; `parts`, when there, are the same text in pieces. */
export type Line = { text: string; style?: 'heading' | 'dim'; parts?: Part[] }
/** The thresholds of the status line (statusline.yellow_from, red_from), so the pane colours at the same numbers. */
export type Limits = { yellowFrom: number; redFrom: number }

const LIMITS: Limits = { yellowFrom: 50, redFrom: 75 }

export function tone(percent: number, limits: Limits): Tone | undefined {
  return percent >= limits.redFrom ? 'error' : percent >= limits.yellowFrom ? 'warning' : undefined
}

/** A row from pieces; the text is theirs joined. */
function pieces(parts: Part[], style?: Line['style']): Line {
  return { text: parts.map(part => part.text).join(''), parts, ...(style === undefined ? {} : { style }) }
}

/** The period `/report <argument>` asks for; no argument or an unknown one means the week. */
export function parsePeriod(args: string): Period {
  const word = args.trim().toLowerCase()
  return PERIODS.find(period => period.id === word)?.id ?? 'week'
}

/** The command that prints the report for a period, built from the usage-clock status line command: its interpreter
 *  and options, the report script in the same folder, then the period and --json. Words after the status line
 *  script are not taken over, they were meant for it. Null if the command is not the usage-clock status line. */
export function reportArgv(command: unknown, period: Period): { argv: string[]; script: string } | null {
  const argv = statuslineArgv(command)
  if (argv === null) return null
  const at = argv.findIndex(word => word.split(/[\\/]/).pop() === STATUSLINE)
  const word = argv[at] as string
  const script = word.slice(0, word.length - STATUSLINE.length) + REPORT_SCRIPT
  return { argv: [...argv.slice(0, at), script, `--${period}`, '--json'], script }
}

export function parseReport(stdout: string): { report: Report } | { error: string } {
  let value: unknown
  try {
    value = JSON.parse(stdout)
  } catch {
    return { error: 'The report output is not valid JSON.' }
  }
  if (typeof value !== 'object' || value === null || Array.isArray(value) || !('schema_version' in value)) {
    return { error: 'The output is not a usage report.' }
  }
  const report = value as Report
  if (report.schema_version !== 1) {
    return { error: `Unsupported report format (schema_version ${String(report.schema_version)}).` }
  }
  return { report }
}

// --- Reading ----------------------------------------------------------------------------------------------------

const rec = (x: unknown): Record<string, unknown> =>
  typeof x === 'object' && x !== null && !Array.isArray(x) ? (x as Record<string, unknown>) : {}
const list = (x: unknown): unknown[] => (Array.isArray(x) ? x : [])
const num = (x: unknown): number | null => (typeof x === 'number' && Number.isFinite(x) ? x : null)
const str = (x: unknown): string | null => (typeof x === 'string' ? x : null)

// --- Numbers as the text report writes them ---------------------------------------------------------------------

/** USD at API prices: two decimals below 1, one below 10, none above. */
export const usd = (x: number | null): string => (x === null ? '-' : x < 1 ? x.toFixed(2) : x < 10 ? x.toFixed(1) : x.toFixed(0))
/** A part from 0 to 1 as a percentage; above 0 and below 1 % is `<1 %`. */
export const pct = (x: number | null, none = '-'): string =>
  x === null ? none : x > 0 && x < 0.01 ? '<1 %' : `${Math.round(x * 100)} %`
/** Points of a limit, an estimate: `<1` is more than 0 and less than one point. */
export const pts = (x: number | null): string => (x === null ? '-' : x === 0 ? '0' : x < 1 ? '<1' : x.toFixed(1))
export const whole = (x: number | null): string => (x === null ? '-' : String(Math.round(x)).replace(/\B(?=(\d{3})+(?!\d))/g, ','))
/** Points of a limit where the report says how much of the cost was measured (`measured_share`): `-` if none of it
 *  lay in a counted section and so the points are not measured, not zero. A report without the field shows the figure. */
export const measuredPts = (points: number | null, measured: unknown): string =>
  num(measured) === 0 && (points ?? 0) === 0 ? '-' : pts(points)
/** The same with `~` before a figure, as an estimate stands in running text; `0`, `<1` and `-` stay without. */
export const estimate = (points: number | null, measured?: unknown): string => {
  const text = measuredPts(points, measured)
  return /^[\d.]+$/.test(text) && text !== '0' ? `~${text}` : text
}
/** A number with at most one decimal, without a trailing zero. */
const plain = (x: number): string => String(Number(x.toFixed(1)))
/** Tokens, with `k` for thousands from 10 000 on. */
export const tokens = (x: number): string => (x >= 10_000 ? `${Math.round(x / 1000)}k` : String(Math.round(x)))

const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/** A UTC time of the report in local time, as the text report writes it (`Tue 13 Oct 14:30`). */
export function localTime(iso: string): string {
  const moment = new Date(iso)
  if (Number.isNaN(moment.getTime())) return iso
  const two = (n: number) => String(n).padStart(2, '0')
  return `${DAYS[moment.getDay()]} ${moment.getDate()} ${MONTHS[moment.getMonth()]} ${two(moment.getHours())}:${two(moment.getMinutes())}`
}

// --- Tables -----------------------------------------------------------------------------------------------------

/** A text cut to a width by losing its middle for a `…`. */
function fit(text: string, width: number): string {
  if (text.length <= width) return text
  if (width <= 1) return '…'.slice(0, width)
  const head = Math.ceil((width - 1) / 2)
  return `${text.slice(0, head)}…${text.slice(text.length - (width - 1 - head))}`
}

/** An MCP tool as its server lists it: `computer`, not `mcp__claude-in-chrome__computer` under `claude-in-chrome`. */
export function toolName(server: string, name: string): string {
  const prefix = `mcp__${server}__`
  return name.startsWith(prefix) ? name.slice(prefix.length) : name
}

/** A file path, short: the home folder as `~`, then cut to `width` by a `…`, the end keeping two thirds of the room:
 *  it is the part that tells two files apart. */
export function shortPath(path: string, width = 48): string {
  const text = path.replace(/^(?:[A-Za-z]:[\\/]Users[\\/][^\\/]+|\/home\/[^/]+|\/Users\/[^/]+)(?=[\\/]|$)/, '~')
  if (text.length <= width) return text
  const head = Math.floor((width - 1) / 3)
  return `${text.slice(0, head)}…${text.slice(text.length - (width - 1 - head))}`
}

const GAP = '  '
const MIN_FIRST = 6

/** A table in monospace: the first column left-aligned and as wide as the room allows, the others right-aligned. */
function table(head: string[], rows: string[][], columns: number): Line[] {
  const others = head.slice(1).map((title, i) => Math.max(title.length, ...rows.map(row => (row[i + 1] ?? '').length)))
  const natural = Math.max((head[0] ?? '').length, ...rows.map(row => (row[0] ?? '').length))
  const room = columns - others.reduce((sum, width) => sum + width + GAP.length, 0)
  const first = Math.max(MIN_FIRST, Math.min(natural, room))
  const draw = (cells: string[]) =>
    [fit(cells[0] ?? '', first).padEnd(first), ...others.map((width, i) => (cells[i + 1] ?? '').padStart(width))]
      .join(GAP)
      .trimEnd()
  return [{ text: draw(head), style: 'dim' }, ...rows.map(row => ({ text: draw(row) }))]
}

/** Label and value rows; numbers are right-aligned under each other, texts start at the same column. */
function facts(rows: [string, string][], align: 'left' | 'right'): Line[] {
  const label = Math.max(...rows.map(([name]) => name.length))
  const value = Math.max(...rows.map(([, text]) => text.length))
  return rows.map(([name, text]) => ({ text: `${name.padEnd(label)}${GAP}${align === 'right' ? text.padStart(value) : text}` }))
}

const BLANK: Line = { text: '' }
const none = (text: string): Line[] => [{ text, style: 'dim' }]

// --- The views --------------------------------------------------------------------------------------------------

const PROJECT_HEAD = ['project', 'USD', 'share', '~week pts', 'sessions']

function projectRow(name: string, entry: Record<string, unknown>): string[] {
  return [name, usd(num(entry.cost_usd)), pct(num(entry.share)), measuredPts(num(entry.week_points), rec(entry.measured_share).week),
    whole(num(entry.sessions))]
}

function overview(report: Report, columns: number, limits: Limits): Line[] {
  const o = rec(report.overview)
  const out: Line[] = []

  const week = o.week === null || o.week === undefined ? null : rec(o.week)
  const used = week === null ? null : num(week.used_percent)
  const rise = week === null ? null : num(week.rise_points)
  let weekParts: Part[] = [{ text: 'no weekly measurement' }]
  if (used !== null && rise !== null) {
    const windows = num(week?.windows) ?? 1
    const level = tone(used, limits)
    weekParts = windows > 1
      ? [{ text: 'weekly limit at ' }, { text: `${Math.round(used)} %`, tone: level },
          { text: ` now · +${plain(rise)} points over ${windows} weekly windows` }]
      : [{ text: `${Math.round(used)} %`, tone: level }, { text: ` of the weekly limit used (+${plain(rise)} points)` }]
  }
  const cost = num(o.cost_usd)
  const title = str(o.title) ?? str(rec(report.period).label) ?? 'Report'
  out.push(pieces([
    { text: `${title} · ` }, ...weekParts, ...(cost === null ? [] : [{ text: ` · ${usd(cost)} USD at API prices` }]),
  ], 'heading'))

  const five = rec(o.five_hour)
  const peak = num(five.highest_percent)
  const at = str(five.highest_at)
  out.push((num(five.windows) ?? 0) > 0 && peak !== null
    ? pieces([
        { text: `5h windows: ${num(five.windows)}, highest ` },
        { text: `${Math.round(peak)} %`, tone: tone(peak, limits) },
        { text: at === null ? '' : ` (${localTime(at)})` },
      ])
    : { text: '5h windows: none measured' })

  const projects = list(o.projects).map(rec)
  if (projects.length > 0) {
    const shown = projects.slice(0, num(o.max_projects) ?? 5)
    const rows = shown.map(entry => projectRow(str(entry.name) ?? '?', entry))
    if (o.more !== null && o.more !== undefined) {
      const more = rec(o.more)
      rows.push(projectRow(`${num(more.projects) ?? '?'} more`, more))
    }
    out.push(BLANK, ...table(PROJECT_HEAD, rows, columns))
  }

  const split = rec(o.split)
  const main = num(split.main_usd)
  const subagents = num(split.subagents_usd)
  if (main !== null && subagents !== null) {
    const top = split.top_subagent === null || split.top_subagent === undefined ? null : rec(split.top_subagent)
    const topPart = top === null || str(top.type) === null
      ? ''
      : ` (top: ${str(top.type)} ${usd(num(top.cost_usd))} USD, ~${pts(num(top.week_points))} week pts)`
    out.push(BLANK, { text: `Main ${usd(main)} USD · subagents ${usd(subagents)} USD${topPart}` })
  }

  const models = rec(o.models)
  const unpriced = num(models.unpriced_requests) ?? 0
  const thinking = num(o.thinking_share)
  const hits = num(o.cache_hit_rate)
  const mixture = [
    ...list(models.families).map(rec).map(family => `${str(family.name) ?? '?'} ${usd(num(family.cost_usd))} USD`),
    unpriced > 0 ? `${unpriced} request${unpriced === 1 ? '' : 's'} without a price` : null,
    thinking === null ? null : `thinking ${pct(thinking)} of output`,
    hits === null ? null : `cache hits ${pct(hits)}`,
  ].filter(Boolean)
  if (mixture.length > 0) out.push({ text: mixture.join(' · ') })

  const startup = o.startup === null || o.startup === undefined ? null : rec(o.startup)
  if (startup !== null) {
    const mainStart = num(startup.main_median_tokens)
    const range = startup.subagent_type_median_tokens === null || startup.subagent_type_median_tokens === undefined
      ? null
      : rec(startup.subagent_type_median_tokens)
    const low = range === null ? null : num(range.min)
    const high = range === null ? null : num(range.max)
    const parts = [
      mainStart === null ? null : `main ${tokens(mainStart)} tokens (median)`,
      low === null || high === null ? null : `subagents ${low === high ? tokens(low) : `${tokens(low)}-${tokens(high)}`} (median per type)`,
    ].filter(Boolean)
    if (parts.length > 0) out.push({ text: `Start: ${parts.join(', ')}` })
  }

  const notable = list(o.notable).map(rec).map(entry => str(entry.text)).filter((text): text is string => text !== null)
  if (notable.length > 0) out.push(BLANK, { text: 'Notable', style: 'heading' }, ...notable.map(text => ({ text: `  ${text}` })))
  return out
}

function agents(report: Report, columns: number): Line[] {
  const total = rec(report.total)
  const byType = rec(rec(total.subagents).by_type)
  const row = (name: string, entry: Record<string, unknown>) =>
    [name, usd(num(entry.cost_usd)), whole(num(entry.requests)), pts(num(entry.week_points)), pts(num(entry.five_points))]
  const rows: string[][] = []
  if (total.main !== undefined && total.main !== null) rows.push(row('main', rec(total.main)))
  rows.push(
    ...Object.entries(byType)
      .map(([name, entry]) => [name, rec(entry)] as const)
      .sort((a, b) => (num(b[1].cost_usd) ?? 0) - (num(a[1].cost_usd) ?? 0))
      .map(([name, entry]) => row(name, entry)),
  )
  if (rows.length === 0) return none('No agent data in this report.')
  const missing = list(rec(report.coverage).archive_days_without_agent_split).length
  return [
    ...table(['agent', 'USD', 'requests', '~week pts', '~5h pts'], rows, columns),
    BLANK,
    ...none('Points (~) are estimates: a share of the measured rise, divided by cost.'),
    ...(missing > 0 ? none(`${missing} archived day${missing === 1 ? '' : 's'} without a split by agent type: their points are missing here.`) : []),
  ]
}

function models(report: Report, columns: number): Line[] {
  const total = rec(report.total)
  const row = (name: string, entry: Record<string, unknown>, priced: boolean) => [
    name,
    priced ? usd(num(entry.cost_usd)) : '-',
    whole(num(entry.requests)),
    pct(num(entry.thinking_share), 'n/a'),
    pct(num(entry.cache_hit_rate), 'n/a'),
  ]
  const priced = Object.entries(rec(total.models))
    .map(([name, entry]) => [name, rec(entry)] as const)
    .sort((a, b) => (num(b[1].cost_usd) ?? 0) - (num(a[1].cost_usd) ?? 0))
  const unpriced = Object.entries(rec(total.unpriced)).map(([name, entry]) => [name, rec(entry)] as const)
  if (priced.length + unpriced.length === 0) return none('No model data in this report.')
  const rows = [...priced.map(([name, entry]) => row(name, entry, true)), ...unpriced.map(([name, entry]) => row(name, entry, false))]
  return [
    ...table(['model', 'USD', 'requests', 'thinking', 'cache hits'], rows, columns),
    ...(unpriced.length > 0 ? [BLANK, ...none('A model without a price has no USD: its tokens are counted, never estimated.')] : []),
  ]
}

function cache(report: Report): Line[] {
  const total = rec(report.total)
  const thinking = num(total.thinking_share)
  return [
    ...facts([
      ['Cache hit rate', pct(num(total.cache_hit_rate), 'n/a')],
      ['Cold starts', whole(num(total.cold_starts))],
      ['Thinking tokens', num(total.thinking_tokens) === null ? 'n/a' : whole(num(total.thinking_tokens))],
      ['Thinking share', thinking === null ? 'n/a' : `${pct(thinking)} of output`],
    ], 'left'),
    BLANK,
    // One row, so the surface wraps it at the width of the pane
    ...none('Cache hit rate: cache reads divided by all input. Cold start: a request of a main session, after its first, '
      + 'that wrote at least 50,000 tokens to the cache. Thinking is part of the output tokens.'),
  ]
}

function tokenCounts(report: Report): Line[] {
  const t = rec(rec(report.total).tokens)
  if (Object.keys(t).length === 0) return none('No token data in this report.')
  return facts([
    ['input', whole(num(t.input))],
    ['cache write 5m', whole(num(t.cache_write_5m))],
    ['cache write 1h', whole(num(t.cache_write_1h))],
    ['cache read', whole(num(t.cache_read))],
    ['output', whole(num(t.output))],
  ], 'right')
}

function projects(report: Report, columns: number): Line[] {
  const entries = list(rec(report.overview).projects).map(rec)
  if (entries.length === 0) return none('No projects in this period.')
  return [
    ...table(PROJECT_HEAD, entries.map(entry => projectRow(str(entry.name) ?? '?', entry)), columns),
    BLANK,
    ...none('~week pts is an estimate: a share of the measured rise, divided by cost.'),
  ]
}

// --- Sessions ---------------------------------------------------------------------------------------------------

const SESSION_HEAD = ['session', 'started', 'USD', '~week pts', 'requests']
/** A session can reach past its period: what the report counts of it is the part within. */
export const PERIOD_ONLY = 'Figures of the period shown: requests of the session before or after it are not counted.'
const TOP = 10
const OWN_MARK = '▶ '

/** The list of the Sessions view: the head, then one row per session of `clis` (highest cost first, as the report
 *  orders them), each with the id the pane selects it by. The session the pane was opened from (`own`) is marked;
 *  when it is in the list, every row moves two cells right so the columns stay aligned. */
export function sessionList(report: Report, columns: number, own: string | null = null): { head: Line; rows: { id: string; line: Line }[] } {
  const clis = list(report.clis).map(rec).filter(cli => str(cli.id) !== null)
  const isMarked = own !== null && clis.some(cli => str(cli.id) === own)
  const indent = (cli: Record<string, unknown> | null) => (!isMarked ? '' : cli !== null && str(cli.id) === own ? OWN_MARK : '  ')
  const cells = clis.map(cli => [
    `${indent(cli)}${str(cli.label) ?? '?'} ${str(cli.id)}`,
    str(cli.start) === null ? '-' : localTime(str(cli.start) as string),
    usd(num(cli.cost_usd)),
    measuredPts(num(rec(cli.estimated_share).week), rec(cli.measured_share).week),
    whole(num(cli.requests)),
  ])
  const [head, ...lines] = table([`${indent(null)}${SESSION_HEAD[0]}`, ...SESSION_HEAD.slice(1)], cells, columns)
  return {
    head: head as Line,
    rows: lines.map((line, i) => {
      const id = str(clis[i]?.id) as string
      return { id, line: id === own ? pieces([{ text: OWN_MARK, tone: 'claude' }, { text: line.text.slice(OWN_MARK.length) }]) : line }
    }),
  }
}

/** A table of names and tokens, the largest first, cut to the `TOP` largest with a line for the rest. */
function tokenTable(head: string, entries: [string, number][], columns: number): Line[] {
  const sorted = [...entries].sort((a, b) => b[1] - a[1])
  const rows = sorted.slice(0, TOP).map(([name, count]) => [name, whole(count)])
  const rest = sorted.slice(TOP)
  if (rest.length > 0) rows.push([`${rest.length} more`, whole(rest.reduce((sum, [, count]) => sum + count, 0))])
  return table([head, 'tokens'], rows, columns)
}

/** What a session's context held after its first answer, as the plugin wrote it down (`clis[].context`). */
function contextLines(context: Record<string, unknown>, columns: number): Line[] {
  const total = num(context.total_tokens)
  const window = num(context.max_tokens)
  const out: Line[] = [{
    text: `Context after the first answer: ${total === null ? '-' : whole(total)} tokens`
      + (total !== null && window !== null && window > 0 ? ` of ${whole(window)} (${pct(total / window)})` : ''),
    style: 'heading',
  }]
  const categories = list(context.categories).map(rec).filter(row => str(row.kind) !== 'free' && str(row.kind) !== 'buffer')
  const named = (rows: Record<string, unknown>[]): [string, number][] => rows.map(row => [str(row.name) ?? '?', num(row.tokens) ?? 0])
  // Deferred tools are listed with their size but not loaded: their tokens are not part of the total above
  const used = categories.filter(row => str(row.kind) !== 'deferred')
  const deferred = categories.filter(row => str(row.kind) === 'deferred')
  if (used.length > 0) out.push(BLANK, ...tokenTable('category', named(used), columns))
  if (deferred.length > 0) out.push(BLANK, ...tokenTable('deferred, not in the context', named(deferred), columns))
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
    out.push(BLANK, ...tokenTable('MCP server', [...servers].map(([name, sum]) => [label(name, sum), sum.tokens]), columns))
    out.push(BLANK, ...tokenTable('MCP tool', tools.map(tool => {
      const server = str(tool.server) ?? '?'
      return [`${server} ${toolName(server, str(tool.name) ?? '?')}`, num(tool.tokens) ?? 0]
    }), columns))
  }
  const memory = list(context.memory_files).map(rec)
  if (memory.length > 0) {
    out.push(BLANK, ...tokenTable('memory file', memory.map(file => [shortPath(str(file.path) ?? '?'), num(file.tokens) ?? 0]), columns))
  }
  const skills = context.skills === null || context.skills === undefined ? null : rec(context.skills)
  if (skills !== null) {
    out.push(BLANK, { text: `Skills: ${whole(num(skills.tokens))} tokens, ${whole(num(skills.included))} of ${whole(num(skills.total))} listed` })
    const items = list(skills.items).map(rec)
    if (items.length > 0) out.push(...tokenTable('skill', items.map(item => [str(item.name) ?? '?', num(item.tokens) ?? 0]), columns))
  }
  const agents = list(context.agents).map(rec)
  if (agents.length > 0) {
    out.push(BLANK, ...tokenTable('agent', agents.map(agent => [str(agent.type) ?? '?', num(agent.tokens) ?? 0]), columns))
  }
  return out
}

/** The detail of one session of `clis`: cost and points, tokens, models, subagents and its recorded context. `own` is
 *  the session the pane was opened from. */
export function sessionDetail(report: Report, id: string, columns: number, own: string | null = null): Line[] {
  const cli = list(report.clis).map(rec).find(entry => str(entry.id) === id)
  if (cli === undefined) return none('This session is not in the report any more. Press q for the list.')
  const share = rec(cli.estimated_share)
  const measured = rec(cli.measured_share)
  const start = str(cli.start)
  const out: Line[] = [
    {
      text: `${str(cli.label) ?? '?'} ${id}${id === own ? ' · this session' : ''} · ${start === null ? '-' : localTime(start)}`
        + ` · ${usd(num(cli.cost_usd))} USD · week: ${estimate(num(share.week), measured.week)} pts`
        + ` · 5h: ${estimate(num(share.five), measured.five)} pts`,
      style: 'heading',
    },
    {
      text: [
        `${whole(num(cli.requests))} requests`,
        `cache hits ${pct(num(cli.cache_hit_rate), 'n/a')}`,
        `cold starts ${whole(num(cli.cold_starts))}`,
        num(cli.thinking_share) === null ? null : `thinking ${pct(num(cli.thinking_share))} of output`,
      ].filter(Boolean).join(' · '),
    },
    { text: PERIOD_ONLY, style: 'dim' },
  ]
  const t = rec(cli.tokens)
  out.push(BLANK, ...facts([
    ['input', whole(num(t.input))],
    ['cache write 5m', whole(num(t.cache_write_5m))],
    ['cache write 1h', whole(num(t.cache_write_1h))],
    ['cache read', whole(num(t.cache_read))],
    ['output', whole(num(t.output))],
  ], 'right'))
  const models = Object.entries(rec(cli.models)).map(([name, cost]) => [name, usd(num(cost))])
  if (models.length > 0) out.push(BLANK, ...table(['model', 'USD'], models, columns))
  const subagents = Object.entries(rec(cli.subagents)).map(([name, entry]) => [name, whole(num(rec(entry).requests)), usd(num(rec(entry).cost_usd))])
  if (subagents.length > 0) out.push(BLANK, ...table(['subagent', 'requests', 'USD'], subagents, columns))
  out.push(BLANK)
  const context = cli.context === null || cli.context === undefined ? null : rec(cli.context)
  out.push(...(context === null
    ? none('No context recorded for this session: the plugin writes it down from version 0.4.0 on, after the first answer.')
    : contextLines(context, columns)))
  return out
}

/** The rows of one view, for a body `columns` cells wide; percentages of a limit coloured at `limits`. */
export function viewLines(view: View, report: Report, columns: number, limits: Limits = LIMITS): Line[] {
  switch (view) {
    case 'overview': return overview(report, columns, limits)
    case 'agents': return agents(report, columns)
    case 'models': return models(report, columns)
    case 'cache': return cache(report)
    case 'tokens': return tokenCounts(report)
    case 'projects': return projects(report, columns)
    // The pane draws the list itself, a button per session; this is what stands without one
    case 'sessions': return list(report.clis).length === 0 ? none('No sessions in this period.') : []
  }
}
