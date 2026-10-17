// /report: a pane with the usage report. Two rows of buttons are its tabs (the period, the view), each with a hotkey:
// Claude Code has no tab element, and Tab or the arrow keys only move the focus in a pane. The body below is one
// view of the report that tools/usage_report.py prints with --json. The command and the pane tell the model nothing.

import { atom, read, update } from 'claude-code'
import type { EngineInterface, On } from 'claude-code'

import type { ReportSlot, ReportState } from '../types/index'
import { PERIODS, VIEWS, parsePeriod, parseReport, reportArgv, viewLines } from './report'
import type { Period, View } from './report'

const PANE = 'usage-report'
const TIMEOUT_MS = 30_000

const INITIAL: ReportState = { period: 'week', view: 'overview', slots: {} }
const report = atom({ plugin: 'usage-clock-display', key: 'report' } as const, INITIAL)

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

export function registerReport(on: On): void {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'report',
      description: 'Open the usage report: what a day, week, month or year of usage went into',
      argumentHint: '[day|week|month|year]',
    })
    return next(e)
  })

  // The answer carries no text and no context: what the pane shows never becomes a transcript row or model input
  on('command.run', { command: 'report' }, async ($, e) => {
    const period = parsePeriod(e.args)
    await update($, report, () => ({ period, view: 'overview', slots: {} }))
    await $.ui.open({ id: PANE, title: 'Usage report', focus: true, closeOnEscape: true })
    await load($, period)
    return {}
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const state = await read($, report)
    const slot = state.slots[state.period]
    // The terminal draws a plain Button as `2: Week`; the desktop draws the label alone, so the key goes into it
    const named = (hotkey: string, label: string) => (e.surface === 'desktop' ? `${hotkey} ${label}` : label)

    const choosePeriod = (period: Period) => async () => {
      let isNew = false
      await update($, report, current => {
        isNew = current.slots[period] === undefined
        return { ...current, period }
      })
      if (isNew) await load($, period)
    }
    const chooseView = (view: View) => () => update($, report, current => ({ ...current, view }))
    const refresh = async () => {
      let period: Period = 'week'
      await update($, report, current => {
        period = current.period
        return current
      })
      await load($, period)
    }

    const tab = (key: string, hotkey: string, label: string, isActive: boolean, onPress: () => unknown) => (
      <Button key={key} hotkey={hotkey} plain dimColor={!isActive} variant={isActive ? 'primary' : 'secondary'} onPress={onPress}>
        {named(hotkey, label)}
      </Button>
    )

    let body
    if (slot === undefined || slot.status === 'loading') {
      body = [<Text key="loading" dimColor>loading…</Text>]
    } else if (slot.status === 'error') {
      body = [
        <Text key="error" color="error">{slot.message}</Text>,
        <Text key="retry" dimColor>Press r to try again.</Text>,
      ]
    } else {
      body = viewLines(state.view, slot.report, e.props.bodyColumns).map((line, i) =>
        line.style === 'heading'
          ? <Text key={`l${i}`} mono weight="bold">{line.text}</Text>
          : <Text key={`l${i}`} mono dimColor={line.style === 'dim'}>{line.text === '' ? ' ' : line.text}</Text>,
      )
    }

    return (
      <Box flexDirection="column">
        <Box flexDirection="row" flexWrap="wrap" columnGap={2}>
          {PERIODS.map(p => tab(`period-${p.id}`, p.hotkey, p.label, p.id === state.period, choosePeriod(p.id)))}
        </Box>
        <Box flexDirection="row" flexWrap="wrap" columnGap={2}>
          {VIEWS.map(v => tab(`view-${v.id}`, v.hotkey, v.label, v.id === state.view, chooseView(v.id)))}
          <Button key="refresh" hotkey="r" plain onPress={refresh}>{named('r', 'Refresh')}</Button>
        </Box>
        <Text> </Text>
        {body}
      </Box>
    )
  })
}
