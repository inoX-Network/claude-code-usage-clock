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
import { LOOK, gaugeSvg, overviewTiles, peakScale, peaksSvg, sessionCards, sessionTiles, tilesPerRow, toneColor, viewTiles } from './tiles'
import type { Bar, Kpi, Tile } from './tiles'

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

    // Every surface shows a plain Button's hotkey itself (the terminal as `b: `, the desktop beside the label). The
    // desktop draws its own native button and ignores the colours of the label inside: there the open tab is its
    // primary button, framed in the accent colour, and the others are plain
    const isDesktop = e.surface === 'desktop'
    const marked = (key: string, isActive: boolean, button: unknown) => (isDesktop && isActive
      ? <Box key={`tab-${key}`} backgroundColor={LOOK.ownCard} borderStyle="round" borderColor={LOOK.accent}>{button}</Box>
      : button)
    const look = (isActive: boolean) => (isDesktop && isActive ? { variant: 'primary' as const } : { plain: true as const })
    const viewTab = (id: View, hotkey: string, label: string) => {
      const isActive = id === state.view
      return marked(`view-${id}`, isActive,
        <Button key={`view-${id}`} hotkey={hotkey} {...look(isActive)} onPress={chooseView(id)} {...(isActive ? { autoFocus: true as const } : {})}>
          {isActive
            ? <Text key={`label-view-${id}`} backgroundColor="claude" color="inverseText" weight="bold">{` ${label} `}</Text>
            : <Text key={`label-view-${id}`}>{` ${label} `}</Text>}
        </Button>,
      )
    }
    const periodTab = (id: Period, hotkey: string, label: string) => {
      const isActive = id === state.period
      return marked(`period-${id}`, isActive,
        <Button key={`period-${id}`} hotkey={hotkey} {...look(isActive)} onPress={choosePeriod(id)}>
          {isActive
            ? <Text key={`label-period-${id}`} color="claude" weight="bold">{label}</Text>
            : <Text key={`label-period-${id}`} dimColor>{label}</Text>}
        </Button>,
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

    // The desktop draws tiles in place of the text: key figures, bars, and the sessions as cards. They carry a dark
    // ground of their own, and as many stand side by side as the width of the pane allows, which the person can drag
    if (e.surface === 'desktop' && slot?.status === 'ok') {
      const { Svg } = $.ui.resolve(e)
      const perRow = tilesPerRow(e.props.bodyColumns)
      const width = (n: 1 | 2 | 4) => (n === 1 ? '100%' : n === 2 ? '49%' : '24%')
      const card = (key: string, children: unknown[], look: { width?: string; isOwn?: boolean } = {}) => (
        <Box key={key} flexDirection="column" width={look.width ?? '100%'} padding="md" rowGap="sm" marginBottom="sm"
          backgroundColor={look.isOwn ? LOOK.ownCard : LOOK.card} borderStyle="round" borderColor={look.isOwn ? LOOK.accent : LOOK.border}>
          {children}
        </Box>
      )
      const caption = (key: string, text: string) => <Text key={key} size="caption" color={LOOK.muted}>{text.toUpperCase()}</Text>
      const bar = (key: string, length: number, color: string) => (
        <Box key={key} flexDirection="row" width="100%" backgroundColor={LOOK.border}>
          <Box width={`${Math.max(2, Math.round(length * 100))}%`} backgroundColor={color}><Text size="caption"> </Text></Box>
        </Box>
      )
      const barRows = (prefix: string, bars: Bar[]) => bars.map((b, i) => (
        <Box key={`${prefix}-${i}`} flexDirection="column">
          <Box flexDirection="row" justifyContent="space-between" columnGap="sm">
            <Text color={LOOK.text} wrap="truncate-middle">{b.label}</Text>
            <Text color={LOOK.text} tabular>{b.value}</Text>
          </Box>
          {bar(`${prefix}-bar-${i}`, b.length, i === 0 ? LOOK.accent : LOOK.accentMuted)}
        </Box>
      ))
      const kpi = (k: Kpi) => card(`kpi-${k.id}`, [
        caption(`kpi-${k.id}-label`, k.label),
        k.gauge !== undefined
          ? <Svg key={`kpi-${k.id}-gauge`} source={gaugeSvg(k.gauge, limits)} alt={`${k.value} % of the weekly limit used`} width={120} height={66} />
          : <Text key={`kpi-${k.id}-value`}>
              <Text key="figure" size="title" weight="bold" tabular color={toneColor(k.tone)}>{k.value}</Text>
              <Text key="unit" size="footnote" color={LOOK.muted}>{` ${k.unit}`}</Text>
            </Text>,
        ...(k.peaks === undefined ? [] : [
          <Svg key={`kpi-${k.id}-peaks`} source={peaksSvg(k.peaks, limits)} alt={`Peaks of the 5h windows: ${k.peaks.join(', ')} %, scale to ${peakScale(k.peaks)} %`} width={120} height={46} />,
        ]),
        ...(k.split === undefined ? [] : [
          <Box key={`kpi-${k.id}-split`} flexDirection="row" width="100%">
            <Box width={`${Math.round((k.split[0] ?? 0) * 100)}%`} backgroundColor={LOOK.accent}><Text size="caption"> </Text></Box>
            <Box width={`${Math.round((k.split[1] ?? 0) * 100)}%`} backgroundColor={LOOK.muted}><Text size="caption"> </Text></Box>
          </Box>,
        ]),
        ...k.lines.map((line, i) => <Text key={`kpi-${k.id}-line-${i}`} size="footnote" color={LOOK.muted}>{line}</Text>),
      ], { width: width(perRow) })
      const grid = (key: string, tiles: unknown[]) => (
        <Box key={key} flexDirection="row" flexWrap="wrap" justifyContent="space-between" width="100%">{tiles}</Box>
      )
      const muted = (key: string, text: string) => <Text key={key} size="footnote" color={LOOK.muted}>{text}</Text>
      const drawTile = (t: Tile) =>
        t.kind === 'kpis' ? grid(t.key, t.kpis.map(kpi))
          : t.kind === 'note' ? card(t.key, [muted(`${t.key}-text`, t.text)])
            : card(t.key, [
                caption(`${t.key}-label`, t.label),
                ...(t.lead === undefined ? [] : [<Text key={`${t.key}-lead`} color={LOOK.text}>{t.lead}</Text>]),
                ...barRows(t.key, t.bars),
                ...(t.note === undefined ? [] : [muted(`${t.key}-note`, t.note)]),
              ])

      if (state.view === 'overview') {
        const t = overviewTiles(slot.report, limits)
        const half = perRow === 1 ? '100%' : '49%'
        body = [
          <Text key="title" size="heading" weight="semibold" color={LOOK.text}>{t.title}</Text>,
          grid('kpis', t.kpis.map(kpi)),
          ...(t.projects.length === 0 ? [] : [card('projects', [
            caption('projects-label', 'Projects · USD · week pts'),
            ...barRows('project', t.projects),
            ...(t.more === null ? [] : [<Text key="projects-more" size="footnote" color={LOOK.muted}>{t.more}</Text>]),
          ])]),
          grid('mix', [
            ...(t.models.length === 0 ? [] : [card('models', [caption('models-label', 'Models · USD'), ...barRows('model', t.models)], { width: half })]),
            ...(t.agents.length === 0 ? [] : [card('agents', [caption('agents-label', 'Main and agents · USD'), ...barRows('agent', t.agents)], { width: half })]),
          ]),
          ...(t.notable.length === 0 ? [] : [card('notable', [
            caption('notable-label', 'Notable'),
            ...t.notable.map((n, i) => (
              <Text key={`notable-${i}`} color={LOOK.text}>
                <Text key="tag" size="caption" weight="bold" color={LOOK.warning}>{`${n.tag}  `}</Text>
                {n.text}
              </Text>
            )),
          ])]),
        ]
      } else if (state.view === 'sessions' && state.session === null && sessionCards(slot.report, state.own).length > 0) {
        const cards = sessionCards(slot.report, state.own)
        const own = cards.filter(c => c.isOwn)
        const sessionCard = (c: (typeof cards)[number]) => card(`card-${c.id}`, [
          <Button key={`session-${c.id}`} plain onPress={chooseSession(c.id)}>
            <Text key={`label-session-${c.id}`} weight="semibold" color={LOOK.text}>{c.title}</Text>
            <Text key={`cost-session-${c.id}`} tabular color={LOOK.text}>{`  ${c.cost}`}</Text>
          </Button>,
          <Text key={`meta-${c.id}`} size="footnote" color={LOOK.muted}>
            {c.meta}
            {c.isOwn ? <Text key="live" size="caption" weight="bold" color={LOOK.accent}>{'  LIVE'}</Text> : ''}
          </Text>,
        ], { isOwn: c.isOwn })
        body = [
          ...(own.length === 0 ? [] : [caption('own-label', 'This session'), ...own.map(sessionCard)]),
          caption('all-label', own.length === 0 ? 'Sessions · highest cost first' : 'All sessions · highest cost first'),
          ...cards.filter(c => !c.isOwn).map(sessionCard),
        ]
      } else if (state.view === 'sessions' && state.session !== null) {
        const [heading, figures] = sessionDetail(slot.report, state.session, e.props.bodyColumns, state.own)
        body = [
          <Button key="back" hotkey="q" plain onPress={chooseSession(null)}><Text key="label-back" dimColor>Back to the list</Text></Button>,
          ...(heading === undefined ? [] : [<Text key="detail-title" size="heading" weight="semibold" color={LOOK.text}>{heading.text}</Text>]),
          ...(figures === undefined || figures.text === '' ? [] : [muted('detail-figures', figures.text)]),
          ...sessionTiles(slot.report, state.session).map(drawTile),
        ]
      } else {
        body = viewTiles(state.view, slot.report).map(drawTile)
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
