export type Usage = {
  startedAt: number
  context: { tokens?: number; window: number; percent?: number }
  rateLimits: { kind: string; percentUsed: number; resetsAt?: string }[]
  cost?: { usd: number }
}

export type LineConfig = {
  yellowFrom: number
  redFrom: number
  weekStopMarker: boolean
  showReset: boolean
  contextYellowFromK: number
  contextRedFromK: number
  cacheYellowBelowMin: number
  startBlockWeek: number
  /** What the spawn gate does from startBlockWeek on (thresholds.week_action); the weekly marker names it. */
  weekAction: WeekAction
}

export type WeekAction = 'warn' | 'ask' | 'deny'

export type ReportPeriod = 'day' | 'week' | 'month' | 'year'
export type ReportView = 'overview' | 'agents' | 'models' | 'cache' | 'tokens' | 'projects' | 'sessions'

/** What the pane holds for one period: being read, the reason it failed, or the parsed `usage_report.py --json`. */
export type ReportSlot =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ok'; report: Record<string, unknown> }

/** The Settings tab: what `settings_cli.py show --json` answered, or why there is nothing. */
export type SettingsSlot =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'missing' }
  | { status: 'ok'; shown: Record<string, unknown> }

/** The Settings tab of the pane: open or not, the part whose info is unfolded, the repository typed into the field,
 *  the answer of the last Check (Save is offered only after `private_ok` for exactly that name), and the last note. */
export type SettingsState = {
  isOpen: boolean
  slot: SettingsSlot | null
  info: string | null
  repo: string | null
  check: { result: string; repo: string; text: string } | null
  note: string | null
}

/** The open report pane: the tab chosen in each row, and what was read per period since `/report` was run. */
export type ReportState = {
  period: ReportPeriod
  view: ReportView
  /** The session of `clis` the Sessions view shows in detail; null shows the list. */
  session: string | null
  /** The session the pane was opened from, marked in the Sessions view; null when it could not be read. */
  own: string | null
  slots: { [period in ReportPeriod]?: ReportSlot }
  settings: SettingsState
}

declare module 'claude-code' {
  interface PluginState {
    'usage-clock-display': {
      usage: Usage | null
      config: LineConfig
      lastAnswer: number | null
      tick: number
      report: ReportState
      logDir: string
      isContextRecorded: boolean
      isSettled: boolean
      /** The gates config.json switched off or down to shadow, as the status line marks them (`spawn gate off`). */
      markers: string[]
    }
  }
}
