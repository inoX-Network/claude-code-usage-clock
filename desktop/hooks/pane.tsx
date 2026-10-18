// /report: a pane with the usage report. One row of buttons is its tabs: the views on the left, the periods on the
// right, each with a hotkey. Claude Code has no tab element and a mod cannot bind the arrow keys, so Tab, which walks
// the buttons, opens the view it lands on. The body below is one view of the report that tools/usage_report.py prints
// with --json. The command and the pane tell the model nothing.

import { atom, read, update } from 'claude-code'
import type { EngineInterface, On } from 'claude-code'

import type { ReportSlot, ReportState } from '../types/index'
import { DEFAULTS } from './config'
import { PERIODS, VIEWS, parsePeriod, parseReport, reportArgv, sessionDetail, sessionList, viewLines } from './report'
import type { Line, Period, View } from './report'

const PANE = 'usage-report'
const TIMEOUT_MS = 30_000

const INITIAL: ReportState = { period: 'week', view: 'overview', session: null, own: null, slots: {} }
const report = atom({ plugin: 'usage-clock-display', key: 'report' } as const, INITIAL)
// The thresholds register.tsx reads at the session start; the engine wants each module's atoms written in that module
const lineConfig = atom({ plugin: 'usage-clock-display', key: 'config' } as const, DEFAULTS)

const messageOf = (error: unknown): string => (error instanceof Error ? error.message : String(error))

/** Reads the report of a period with the interpreter of the usage-clock status line. Every failure is a line for the
 *  pane, none is thrown: the pane is the only place the person sees it. */
async function fetchReport($: EngineInterface, period: Period): Promise<ReportSlot> {
  let command: unknown
  try {
    const line = (await $.settings.read()).statusLine
    command = typeof line === 'object' && line !== null ? (line as { command?: unknown }).command : undefined
  } catch (error) {
    return { status: 'error', message: `Could not read settings.json: ${messageOf(error)}` }
  }
  const found = reportArgv(command, period)
  if (found === null) {
    return { status: 'error', message: 'No usage-clock status line in settings.json. Run tools/install.py: it installs the status line and the report.' }
  }
  if (!(await $.fs.exists(found.script))) {
    return { status: 'error', message: `The report is not installed (${found.script} is missing). Run tools/install.py to install it next to the hooks.` }
  }
  let run
  try {
    run = await $.process.run(found.argv, { timeoutMs: TIMEOUT_MS })
  } catch (error) {
    return { status: 'error', message: `Could not start the report: ${messageOf(error)}` }
  }
  if (run.exitCode !== 0) {
    const reason = run.stderr.trim().split('\n')[0] ?? ''
    return { status: 'error', message: `The report failed (exit code ${run.exitCode})${reason === '' ? '.' : `: ${reason}`}` }
  }
  const parsed = parseReport(run.stdout)
  return 'error' in parsed ? { status: 'error', message: parsed.error } : { status: 'ok', report: parsed.report }
}

async function setSlot($: EngineInterface, period: Period, slot: ReportSlot): Promise<void> {
  await update($, report, state => ({ ...state, slots: { ...state.slots, [period]: slot } }))
}

async function load($: EngineInterface, period: Period): Promise<void> {
  await setSlot($, period, { status: 'loading' })
  await setSlot($, period, await fetchReport($, period))
}

/** Registers /report. Called from the one session.start hook of the plugin: the engine allows one per module. */
export async function registerReportCommand($: EngineInterface): Promise<void> {
  await $.command.register({
    name: 'report',
    description: 'Open the usage report: what a day, week, month or year of usage went into',
    argumentHint: '[day|week|month|year]',
  })
}

export function registerReport(on: On): void {
  // The answer carries no text and no context: what the pane shows never becomes a transcript row or model input
  on('command.run', { command: 'report' }, async ($, e) => {
    const period = parsePeriod(e.args)
    // The report names a session by the first 8 characters of its transcript name; the session id is the whole UUID
    const own = await $.session.id().then(id => id.slice(0, 8)).catch(() => null)
    await update($, report, (): ReportState => ({ period, view: 'overview', session: null, own, slots: {} }))
    await $.ui.open({ id: PANE, title: 'Usage report', focus: true, closeOnEscape: true })
    await load($, period)
    return {}
  })

  // Tab walks the buttons of the pane; landing on a view's tab opens that view, as a tab row does. A period is only
  // chosen by its key or Enter: walking past Month and Year would read each of them
  on('ui.focus', { requestId: PANE }, async ($, e, next) => {
    const view = VIEWS.find(v => `view-${v.id}` === e.element)
    if (view !== undefined) {
      await update($, report, current => (current.view === view.id ? current : { ...current, view: view.id, session: null }))
    }
    return next(e)
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const state = await read($, report)
    const limits = await read($, lineConfig)
    const slot = state.slots[state.period]

    const choosePeriod = (period: Period) => async () => {
      let isNew = false
      await update($, report, current => {
        isNew = current.slots[period] === undefined
        return { ...current, period, session: null }
      })
      if (isNew) await load($, period)
    }
    // A view chosen by its key takes the focus ring along, so the ring and the highlighted tab do not point apart. The
    // engine refuses the move while the pane does not hold the keys; the view is chosen all the same
    const chooseView = (view: View) => async () => {
      await update($, report, current => ({ ...current, view, session: null }))
      await $.ui.focus({ requestId: PANE, key: `view-${view}` }).catch(() => undefined)
    }
    const chooseSession = (session: string | null) => () => update($, report, current => ({ ...current, session }))
    const refresh = async () => {
      let period: Period = 'week'
      await update($, report, current => {
        period = current.period
        return current
      })
      await load($, period)
    }

    // Every surface shows a plain Button's hotkey itself (the terminal as `b: `, the desktop beside the label)
    const viewTab = (id: View, hotkey: string, label: string) => {
      const isActive = id === state.view
      return (
        <Button key={`view-${id}`} hotkey={hotkey} plain onPress={chooseView(id)} {...(isActive ? { autoFocus: true as const } : {})}>
          {isActive
            ? <Text key={`label-view-${id}`} backgroundColor="claude" color="inverseText" weight="bold">{` ${label} `}</Text>
            : <Text key={`label-view-${id}`}>{` ${label} `}</Text>}
        </Button>
      )
    }
    const periodTab = (id: Period, hotkey: string, label: string) => {
      const isActive = id === state.period
      return (
        <Button key={`period-${id}`} hotkey={hotkey} plain onPress={choosePeriod(id)}>
          {isActive
            ? <Text key={`label-period-${id}`} color="claude" weight="bold">{label}</Text>
            : <Text key={`label-period-${id}`} dimColor>{label}</Text>}
        </Button>
      )
    }
    const row = (line: Line, i: number) => {
      const look = line.style === 'heading' ? { color: 'claude', weight: 'bold' as const } : line.style === 'dim' ? { dimColor: true } : {}
      const parts = line.parts ?? [{ text: line.text === '' ? ' ' : line.text }]
      return (
        <Text key={`l${i}`} mono {...look}>
          {parts.map((part, j) => (part.tone === undefined ? part.text : <Text key={`p${j}`} color={part.tone}>{part.text}</Text>))}
        </Text>
      )
    }

    let body
    if (slot === undefined || slot.status === 'loading') {
      body = [<Text key="loading" dimColor>loading…</Text>]
    } else if (slot.status === 'error') {
      body = [
        <Text key="error" color="error">{slot.message}</Text>,
        <Text key="retry" dimColor>Press r to try again.</Text>,
      ]
    } else {
      body = viewLines(state.view, slot.report, e.props.bodyColumns, limits).map(row)
      if (state.view === 'sessions' && state.session !== null) {
        body = [
          <Button key="back" hotkey="q" plain onPress={chooseSession(null)}><Text key="label-back" dimColor>Back to the list</Text></Button>,
          <Text key="gap"> </Text>,
          ...sessionDetail(slot.report, state.session, e.props.bodyColumns, state.own).map(row),
        ]
      } else if (state.view === 'sessions' && body.length === 0) {
        // One button per session: Tab or Up and Down walk them, Enter opens one
        const { head, rows } = sessionList(slot.report, e.props.bodyColumns, state.own)
        body = [
          row(head, 0),
          ...rows.map(({ id, line }) => (
            <Button key={`session-${id}`} plain onPress={chooseSession(id)}>
              <Text key={`label-session-${id}`} mono>
                {(line.parts ?? [{ text: line.text }]).map((part, j) => (part.tone === undefined ? part.text : <Text key={`p${j}`} color={part.tone}>{part.text}</Text>))}
              </Text>
            </Button>
          )),
        ]
      }
    }

    // Like the tab row of /config: the views on the left, the period and Refresh on the right
    return (
      <Box flexDirection="column">
        <Box flexDirection="row" flexWrap="wrap" justifyContent="space-between" columnGap={2}>
          <Box flexDirection="row" flexWrap="wrap" columnGap={1}>
            {VIEWS.map(v => viewTab(v.id, v.hotkey, v.label))}
          </Box>
          <Box flexDirection="row" flexWrap="wrap" columnGap={2}>
            {PERIODS.map(p => periodTab(p.id, p.hotkey, p.label))}
            <Button key="refresh" hotkey="r" plain onPress={refresh}><Text key="label-refresh" dimColor>Refresh</Text></Button>
          </Box>
        </Box>
        <Text> </Text>
        {body}
      </Box>
    )
  })
}
