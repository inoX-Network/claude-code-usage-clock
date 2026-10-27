// /report: a pane with the usage report. One row of buttons is its tabs: the views and Settings on the left, the
// periods on the right, each with a hotkey. Claude Code has no tab element and a mod cannot bind the arrow keys, so Tab, which walks
// the buttons, opens the view it lands on. The body below is one view of the report that tools/usage_report.py prints
// with --json. The command and the pane tell the model nothing.

import { atom, read, update } from 'claude-code'
import type { EngineInterface, On } from 'claude-code'

import type { ReportSlot, ReportState, SettingsSlot, SettingsState } from '../types/index'
import { DEFAULTS } from './config'
import { PERIODS, PERIOD_ONLY, VIEWS, localTime, parsePeriod, parseReport, reportArgv, sessionDetail, sessionList, viewLines } from './report'
import type { Line, Period, View } from './report'
import { LOOK, gaugeSvg, overviewTiles, peakScale, peaksSvg, sessionCards, sessionTiles, tilesPerRow, toneColor, viewTiles } from './tiles'
import type { Bar, Kpi, Tile } from './tiles'
import { INVALID_REPO, UPDATE_LINE, activeChoice, choiceLabel, docsLabel, infoParagraphs, parseAnswer, parseShown, printable, settingsArgv, validRepo } from './settings'
import type { Brake, Level, Part, Shown } from './settings'

const PANE = 'usage-report'
const TIMEOUT_MS = 30_000
// The settings script: Check asks GitHub twice (VISIBILITY_TIMEOUT_S of hooks/_sync_git.py, 15 s each). Save checks
// again, then probes once more (15 s) and clones (CLONE_TIMEOUT_S of hooks/_setup.py, 300 s): 345 s at most. 15 s
// more each for starting Python and writing; tests/test_settings_cli.py checks both against the constants
const SETTINGS_TIMEOUT_MS = 45_000
const SAVE_TIMEOUT_MS = 360_000

const CLOSED: SettingsState = { isOpen: false, slot: null, info: null, repo: null, drafts: {}, check: null, note: null }
const INITIAL: ReportState = { period: 'week', view: 'overview', session: null, own: null, slots: {}, settings: CLOSED }
const report = atom({ plugin: 'usage-clock-display', key: 'report' } as const, INITIAL)
// The thresholds register.tsx reads at the session start; the engine wants each module's atoms written in that module
const lineConfig = atom({ plugin: 'usage-clock-display', key: 'config' } as const, DEFAULTS)

const messageOf = (error: unknown): string => (error instanceof Error ? error.message : String(error))

/** Reads the report of a period with the interpreter of the usage-clock status line. Every failure is a line for the
 *  pane, none is thrown: the pane is the only place the person sees it. */
async function fetchReport($: EngineInterface, period: Period): Promise<ReportSlot> {
  let command: unknown
  try {
    const line = (await $.settings.read({ source: 'user' })).statusLine      // never a project's command
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

type SettingsRun = { status: 'ok'; answer: Record<string, unknown> } | { status: 'missing' } | { status: 'error'; message: string }

/** Runs the settings script next to the status line with `args`, each a word of its own: no shell ever sees them. */
async function runSettings($: EngineInterface, args: string[], timeoutMs = SETTINGS_TIMEOUT_MS): Promise<SettingsRun> {
  let command: unknown
  try {
    const line = (await $.settings.read({ source: 'user' })).statusLine      // never a project's command
    command = typeof line === 'object' && line !== null ? (line as { command?: unknown }).command : undefined
  } catch (error) {
    return { status: 'error', message: `Could not read settings.json: ${messageOf(error)}` }
  }
  const found = settingsArgv(command, args)
  if (found === null) {
    return { status: 'error', message: 'No usage-clock status line in settings.json. Run tools/install.py: it installs the status line and the settings.' }
  }
  if (!(await $.fs.exists(found.script))) return { status: 'missing' }
  let run
  try {
    run = await $.process.run(found.argv, { timeoutMs })
  } catch (error) {
    return { status: 'error', message: `Could not start the settings: ${messageOf(error)}` }
  }
  const answer = parseAnswer(run.stdout)
  if (answer === null) {
    const reason = run.stderr.trim().split('\n')[0] ?? ''
    return { status: 'error', message: `The settings failed (exit code ${run.exitCode})${reason === '' ? '.' : `: ${reason}`}` }
  }
  return { status: 'ok', answer }
}

async function setSettings($: EngineInterface, change: Partial<SettingsState>): Promise<void> {
  await update($, report, state => ({ ...state, settings: { ...state.settings, ...change } }))
}

/** The text typed into the field of `key`, or none (undefined): the field shows the value of config.json again. */
async function setDraft($: EngineInterface, key: string, text: string | undefined): Promise<void> {
  await update($, report, state => {
    const drafts = { ...state.settings.drafts }
    if (text === undefined) delete drafts[key]
    else drafts[key] = text
    return { ...state, settings: { ...state.settings, drafts } }
  })
}

/** Reads show --json. Only the first read of an opened /report says loading: a later one leaves the tab as it is until
 *  the answer is there, so that its buttons, and the focus on one of them, stay in place. */
async function loadSettings($: EngineInterface): Promise<void> {
  await update($, report, state => (state.settings.slot !== null ? state
    : { ...state, settings: { ...state.settings, slot: { status: 'loading' } } }))
  const run = await runSettings($, ['show', '--json'])
  let slot: SettingsSlot
  if (run.status !== 'ok') {
    slot = run
  } else {
    const shown = parseShown(run.answer)
    slot = shown === null
      ? { status: 'error', message: typeof run.answer.error === 'string' ? run.answer.error : 'The answer is not the settings.' }
      : { status: 'ok', shown }
  }
  await setSettings($, { slot })
}

/** The note a write leaves: the error the script names, or what went wrong running it. */
function noteOf(run: SettingsRun): string | null {
  if (run.status === 'missing') return UPDATE_LINE
  if (run.status === 'error') return run.message
  return typeof run.answer.error === 'string' ? run.answer.error : null
}

/** Registers /report. Called from the one session.start hook of the plugin: the engine allows one per module. */
export async function registerReportCommand($: EngineInterface): Promise<void> {
  await $.command.register({
    name: 'report',
    description: 'Open the usage report: what a day, week, month or year of usage went into',
    argumentHint: '[day|week|month|year]',
  })
}

async function openSettings($: EngineInterface): Promise<void> {
  let isNew = false
  await update($, report, current => {
    isNew = current.settings.slot === null
    return current.settings.isOpen ? current : { ...current, settings: { ...current.settings, isOpen: true } }
  })
  if (isNew) await loadSettings($)
}

export function registerReport(on: On): void {
  // The answer carries no text and no context: what the pane shows never becomes a transcript row or model input
  on('command.run', { command: 'report' }, async ($, e) => {
    const period = parsePeriod(e.args)
    // The report names a session by the first 8 characters of its transcript name; the session id is the whole UUID
    const own = await $.session.id().then(id => id.slice(0, 8)).catch(() => null)
    await update($, report, (): ReportState => ({ period, view: 'overview', session: null, own, slots: {}, settings: CLOSED }))
    await $.ui.open({ id: PANE, title: 'Usage report', focus: true, closeOnEscape: true })
    await load($, period)
    return {}
  })

  // Tab walks the buttons of the pane; landing on a view's tab opens that view, as a tab row does. A period is only
  // chosen by its key or Enter: walking past Month and Year would read each of them
  on('ui.focus', { requestId: PANE }, async ($, e, next) => {
    const view = VIEWS.find(v => `view-${v.id}` === e.element)
    if (view !== undefined) {
      await update($, report, current => (current.view === view.id && !current.settings.isOpen ? current
        : { ...current, view: view.id, session: null, settings: { ...current.settings, isOpen: false } }))
    } else if (e.element === 'view-settings') {
      await openSettings($)
    }
    return next(e)
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button, Input, Link } = $.ui.resolve(e)
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
    // The plugin never places the focus ring itself: the engine draws it by inverting the key in white, which reads as
    // a second, louder tab. It shows only once the person walks the buttons with Tab
    const chooseView = (view: View) => () => update($, report, current => ({ ...current, view, session: null, settings: { ...current.settings, isOpen: false } }))
    const chooseSession = (session: string | null) => () => update($, report, current => ({ ...current, session }))
    const refresh = async () => {
      if (state.settings.isOpen) {
        await loadSettings($)
        return
      }
      let period: Period = 'week'
      await update($, report, current => {
        period = current.period
        return current
      })
      await load($, period)
    }

    // Every surface shows a plain Button's hotkey itself (the terminal as `b: `, the desktop beside the label). The
    // desktop draws its own native button and ignores the colours of the label inside, and its plain button has no
    // fill: there the open tab sits on a plate of the accent colour with no room around it, so it reads as filled. Its
    // key, which would choose what is open already, is written into the label instead of the app's grey key box, so
    // that it takes the colour of the text
    const isDesktop = e.surface === 'desktop'
    const keyOf = (hotkey: string, isActive: boolean) => (isDesktop && isActive ? {} : { hotkey })
    const keyText = (hotkey: string, isActive: boolean) => (isDesktop && isActive ? `${hotkey.toUpperCase()}  ` : '')
    const marked = (key: string, isActive: boolean, button: unknown) => (isDesktop && isActive
      ? <Box key={`tab-${key}`} backgroundColor={LOOK.accent} borderStyle="round" borderColor={LOOK.accent}>{button}</Box>
      : button)
    const viewTab = (id: View, hotkey: string, label: string) => {
      const isActive = id === state.view && !state.settings.isOpen
      return marked(`view-${id}`, isActive,
        <Button key={`view-${id}`} {...keyOf(hotkey, isActive)} plain onPress={chooseView(id)}>
          {isActive
            ? <Text key={`label-view-${id}`} backgroundColor="claude" color="inverseText" weight="bold">{` ${keyText(hotkey, isActive)}${label} `}</Text>
            : <Text key={`label-view-${id}`}>{` ${label} `}</Text>}
        </Button>,
      )
    }
    const periodTab = (id: Period, hotkey: string, label: string) => {
      const isActive = id === state.period
      return marked(`period-${id}`, isActive,
        <Button key={`period-${id}`} {...keyOf(hotkey, isActive)} plain onPress={choosePeriod(id)}>
          {isActive
            ? <Text key={`label-period-${id}`} color="claude" weight="bold">{`${keyText(hotkey, isActive)}${label}`}</Text>
            : <Text key={`label-period-${id}`} dimColor>{label}</Text>}
        </Button>,
      )
    }
    const settingsTab = () => {
      const isActive = state.settings.isOpen
      return marked('view-settings', isActive,
        <Button key="view-settings" {...keyOf('h', isActive)} plain onPress={() => openSettings($)}>
          {isActive
            ? <Text key="label-view-settings" backgroundColor="claude" color="inverseText" weight="bold">{` ${keyText('h', isActive)}Settings `}</Text>
            : <Text key="label-view-settings">{' Settings '}</Text>}
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
    // ground of their own, and as many stand side by side as the width of the pane allows, which the person can drag.
    // Every view draws its cards with `card`, Settings too, so that all tabs look alike
    const perRow = tilesPerRow(e.props.bodyColumns)
    const width = (n: 1 | 2 | 4) => (n === 1 ? '100%' : n === 2 ? '49%' : '24%')
    const card = (key: string, children: unknown[], look: { width?: string; isOwn?: boolean } = {}) => (
      <Box key={key} flexDirection="column" width={look.width ?? '100%'} padding="md" rowGap="sm" marginBottom="sm"
        backgroundColor={look.isOwn ? LOOK.ownCard : LOOK.card} borderStyle="round" borderColor={look.isOwn ? LOOK.accent : LOOK.border}>
        {children}
      </Box>
    )
    const grid = (key: string, tiles: unknown[]) => (
      <Box key={key} flexDirection="row" flexWrap="wrap" justifyContent="space-between" width="100%">{tiles}</Box>
    )
    if (e.surface === 'desktop' && slot?.status === 'ok' && !state.settings.isOpen) {
      const { Svg } = $.ui.resolve(e)
      const caption = (key: string, text: string) => <Text key={key} size="caption" color={LOOK.muted}>{text.toUpperCase()}</Text>
      const bar = (key: string, length: number, color: string) => (
        <Box key={key} flexDirection="row" width="100%" backgroundColor={LOOK.border}>
          {length > 0 ? <Box width={`${Math.max(1, Math.round(length * 100))}%`} backgroundColor={color}><Text size="caption"> </Text></Box> : null}
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
          muted('period-only', PERIOD_ONLY),
          ...cards.filter(c => !c.isOwn).map(sessionCard),
        ]
      } else if (state.view === 'sessions' && state.session !== null) {
        const [heading, figures] = sessionDetail(slot.report, state.session, e.props.bodyColumns, state.own)
        body = [
          <Button key="back" hotkey="q" plain onPress={chooseSession(null)}><Text key="label-back" dimColor>Back to the list</Text></Button>,
          ...(heading === undefined ? [] : [<Text key="detail-title" size="heading" weight="semibold" color={LOOK.text}>{heading.text}</Text>]),
          ...(figures === undefined || figures.text === '' ? [] : [muted('detail-figures', figures.text)]),
          muted('detail-period-only', PERIOD_ONLY),
          ...sessionTiles(slot.report, state.session).map(drawTile),
        ]
      } else {
        body = viewTiles(state.view, slot.report).map(drawTile)
      }
    }

    // Settings: a switch per part and the sync repository, written through settings_cli.py only. Every text that
    // explains a part comes from its answer
    if (state.settings.isOpen) {
      const settings = state.settings
      const found = settings.slot
      if (found === null || found.status === 'loading') {
        body = [<Text key="settings-loading" dimColor>loading…</Text>]
      } else if (found.status === 'missing') {
        body = [<Text key="settings-update" color="warning">{UPDATE_LINE}</Text>]
      } else if (found.status === 'error') {
        body = [<Text key="settings-error" color="error">{found.message}</Text>, <Text key="settings-retry" dimColor>Press r to try again.</Text>]
      } else {
        const shown = found.shown as unknown as Shown
        const sync = shown.sync
        const repo = settings.repo ?? sync.repo
        const canSave = settings.check?.result === 'private_ok' && settings.check.repo === repo
        const toggle = (id: string) => () => setSettings($, { info: settings.info === id ? null : id })
        const infoButton = (id: string) => (
          <Button key={`info-${id}`} plain onPress={toggle(id)}><Text key={`label-info-${id}`} dimColor>i</Text></Button>
        )
        // The terminal indents with a Box, never with spaces in the text: a wrapped line stays indented. The desktop
        // card has its own padding
        const indent = isDesktop ? {} : { paddingLeft: 2 }
        // What stands beside the name of a part: dim in the terminal; on the desktop muted and as small as the
        // footnotes of the tiles
        const quiet = isDesktop ? { size: 'footnote' as const, color: LOOK.muted } : { dimColor: true }
        // Below the sentence of its part and indented once more: the info as paragraphs, then the sentence of the tab.
        // The terminal shows the link of `More:` on a line of its own, the desktop as a link named by its file
        const infoLine = (id: string, line: string, i: number) => {
          if (!line.startsWith('More: ')) {
            return [<Text key={`info-${id}-${i}`} {...(isDesktop ? quiet : { mono: true, dimColor: true })}>{line}</Text>]
          }
          const url = line.slice('More: '.length)
          return isDesktop
            ? [<Text key={`info-${id}-${i}`} {...quiet}><Link href={url}>{docsLabel(url)}</Link></Text>]
            : [<Text key={`info-${id}-${i}`} mono dimColor>More:</Text>, <Text key={`info-${id}-${i}-url`} mono dimColor>{url}</Text>]
        }
        const infoText = (id: string, info: string, tab: string) => (settings.info !== id ? [] : [
          <Box key={`info-text-${id}`} flexDirection="column" {...indent}>
            {[...infoParagraphs(info), tab].flatMap((line, i) => infoLine(id, line, i))}
          </Box>,
        ])
        const choose = (key: string, value: string) => async () => {
          const run = await runSettings($, ['set', key, value])
          await setSettings($, { note: noteOf(run) })
          await loadSettings($)
        }
        // A number typed into a field: the script checks it (range, the order of the levels) and names what is wrong.
        // `--`: a typed -1 is a value, not an option of the script
        const setNumber = async (key: string, typed: string) => {
          const run = await runSettings($, ['set', '--', key, typed.trim()])
          await setDraft($, key, undefined)
          await setSettings($, { note: noteOf(run) })
          await loadSettings($)
        }
        // The active choice is marked: the desktop draws it without chrome on a plate of the accent, the others as its
        // own buttons. A Box has no radius: the round frame in the accent rounds the plate like the app's buttons, and no
        // padding and no spaces around the label keep it as high as they are (the open tab's plate, with spaces, is twice
        // as high). The app's primary button would be white. The terminal marks it as the open tab, on the accent
        const choiceButton = (id: string, key: string, choice: string, active: string, suffix: string) => (isDesktop
          ? choice === active
            ? <Box key={`plate-${id}-${choice}`} backgroundColor={LOOK.accent} borderStyle="round" borderColor={LOOK.accent} padding={0}>
                <Button key={`set-${id}-${choice}`} plain onPress={choose(key, choice)}>
                  <Text key={`label-set-${id}-${choice}`}>{`${choiceLabel(choice)}${suffix}`}</Text>
                </Button>
              </Box>
            : <Button key={`set-${id}-${choice}`} onPress={choose(key, choice)}>
                <Text key={`label-set-${id}-${choice}`}>{choiceLabel(choice)}</Text>
              </Button>
          : <Button key={`set-${id}-${choice}`} plain onPress={choose(key, choice)}>
              {choice === active
                ? <Text key={`label-set-${id}-${choice}`} backgroundColor="claude" color="inverseText" weight="bold">{` ${choiceLabel(choice)}${suffix} `}</Text>
                : <Text key={`label-set-${id}-${choice}`}>{choiceLabel(choice)}</Text>}
            </Button>)
        // Check and Save on the plate of the marked choice, so that they read as buttons. The desktop does not draw the
        // colours of a label: there Save stands on the plate of the open tab
        const actionButton = (key: string, label: string, onPress: () => Promise<void>) => (
          <Button key={key} plain onPress={onPress}>
            <Text key={`label-${key}`} backgroundColor="claude" color="inverseText" weight="bold">{` ${label} `}</Text>
          </Button>
        )
        // The desktop draws each part as a card of the other views, the parts side by side, two at most
        const partWidth = perRow === 1 ? '100%' : '49%'
        const container = (key: string, children: unknown[], cardWidth = '100%') => (isDesktop
          ? card(key, children, { width: cardWidth })
          : <Box key={key} flexDirection="column" marginBottom={1}>{children}</Box>)
        // The state in effect as a small label in capitals, at the right of the name on the desktop: the accent where
        // a part acts, the warning colour where it only watches, muted where it is off
        const badgeOf = (choice: string) => ({
          text: choiceLabel(choice).toUpperCase(),
          color: choice === 'shadow' ? LOOK.warning
            : ['true', 'on', 'enforce', 'short', 'full'].includes(choice) ? LOOK.accent : LOOK.muted,
        })
        // A row: the name on a line of its own, below it one indented block with the sentence, the buttons and what
        // they unfold. Every row has this shape, so its buttons start in the same column
        // On the desktop i stands in the head, right of the state: below the choices a narrow card wrapped it onto a line
        // of its own
        const heading = (id: string, label: string, isInstalled: boolean, badge?: { text: string; color: string }, info?: unknown) => (isDesktop
          ? <Box key={`head-${id}`} flexDirection="row" justifyContent="space-between" alignItems="center" columnGap="sm">
              <Text key={`part-${id}`} weight="semibold" color={isInstalled ? LOOK.text : LOOK.muted}>{label}</Text>
              <Box key={`head-right-${id}`} flexDirection="row" alignItems="center" columnGap="sm">
                {badge === undefined ? null : <Text key={`state-${id}`} size="caption" weight="bold" color={badge.color}>{badge.text}</Text>}
                {info ?? null}
              </Box>
            </Box>
          : <Text key={`part-${id}`} weight="bold" dimColor={!isInstalled}>{label}</Text>)
        const block = (id: string, children: unknown[]) => (
          <Box key={`body-${id}`} flexDirection="column" {...(isDesktop ? { rowGap: 'sm' as const } : indent)}>{children}</Box>
        )
        const sentence = (id: string, summary: string) => <Text key={`summary-${id}`} {...quiet}>{summary}</Text>
        const controls = (id: string, children: unknown[], look: { alignItems?: 'center' } = {}) => (
          <Box key={`controls-${id}`} flexDirection="row" flexWrap="wrap" columnGap={isDesktop ? 1 : 2} {...look}>{children}</Box>
        )
        // On the desktop the choices of a part stand in a frame of their own, which wraps inside a narrow card rather
        // than reach past its edge
        const group = (id: string, buttons: unknown[]) => (isDesktop && buttons.length > 0
          ? [<Box key={`group-${id}`} flexDirection="row" flexWrap="wrap" alignItems="center" columnGap={1} rowGap="xs" paddingX="xs" borderStyle="round" borderColor={LOOK.border}>{buttons}</Box>]
          : buttons)
        // i: in the head on the desktop, after the choices in the terminal
        const infoInHead = (id: string) => (isDesktop ? infoButton(id) : undefined)
        const infoInRow = (id: string) => (isDesktop ? [] : [infoButton(id)])
        const absent = (id: string, label: string, summary: string, cardWidth?: string) => container(`row-${id}`, [
          heading(id, label, false, { text: 'NOT INSTALLED', color: LOOK.muted }),
          block(id, [sentence(id, summary), <Text key={`absent-${id}`} {...quiet}>not installed: install.py</Text>]),
        ], cardWidth)
        // The field of a limit or a level: the typed text until Enter (or Set on the desktop) hands it to the script. It
        // gets the text without control characters: the engine draws no Input whose value holds one. The terminal frames
        // it as the field of the repository
        const numberField = (id: string, limit: { key: string; value: number; unit: string }) => {
          const field = (
            <Input key={`field-${id}`} label={limit.unit} value={settings.drafts[limit.key] ?? String(limit.value)}
              placeholder={String(limit.value)} submitLabel={isDesktop ? 'Set' : 'set'}
              onInput={(value: string) => setDraft($, limit.key, String(printable(value)))}
              onSubmit={(value: string) => setNumber(limit.key, value)} />
          )
          return isDesktop ? field : <Box key={`frame-${id}`} borderStyle="round" paddingX={1}>{field}</Box>
        }
        // A brake of the spawn gate or a level of the soft stop: its name, its sentence, then its choices and its field
        const subRow = (id: string, label: string, summary: string, children: unknown[]) => (
          <Box key={`sub-${id}`} flexDirection="column" {...(isDesktop ? { rowGap: 'xs' as const } : { marginTop: 1 })}>
            <Text key={`sub-label-${id}`} weight="semibold">{label}</Text>
            {sentence(id, summary)}
            {controls(id, children, { alignItems: 'center' })}
          </Box>
        )
        const brakeRow = (brake: Brake) => {
          const id = `brake-${brake.id}`
          return subRow(id, brake.label, brake.summary, [
            ...group(id, brake.choices.map(choice => choiceButton(id, brake.key, choice, brake.value, ''))),
            ...(brake.limit === null ? [] : [numberField(id, brake.limit)]),
          ])
        }
        const levelRow = (level: Level) => {
          const id = `level-${level.key.split('.').pop() ?? ''}`
          return subRow(id, level.label, level.summary, [numberField(id, level)])
        }
        const partRow = (part: Part) => {
          // The status line has no switch: installed it needs no card; missing, nothing measures for the other parts,
          // and its row warns of that
          if (part.key === null) {
            return part.installed ? null : container(`row-${part.id}`, [
              heading(part.id, part.label, false, { text: 'NOT INSTALLED', color: LOOK.warning }),
              block(part.id, [
                sentence(part.id, part.summary),
                <Text key={`warning-${part.id}`} color={isDesktop ? LOOK.warning : 'warning'}>{part.tab}</Text>,
              ]),
            ], partWidth)
          }
          if (!part.installed) return absent(part.id, part.label, part.summary, partWidth)
          const active = activeChoice(part)
          return container(`row-${part.id}`, [
            heading(part.id, part.label, true, badgeOf(active), infoInHead(part.id)),
            block(part.id, [
              sentence(part.id, part.summary),
              controls(part.id, [
                ...group(part.id, part.choices.map(choice => choiceButton(part.id, part.key as string, choice, active,
                  part.value === '' && choice === active ? ' (installed)' : ''))),
                ...infoInRow(part.id),
              ]),
              ...infoText(part.id, part.info, part.tab),
              ...(part.brakes ?? []).map(brakeRow),
              ...(part.levels ?? []).map(levelRow),
            ]),
          ], partWidth)
        }
        const checkRepo = async (name: string) => {
          // A name the script would refuse starts no process at all. The field gets it without control characters:
          // the engine draws no Input whose value holds one
          if (!validRepo(name)) {
            const shown = String(printable(name))
            await setSettings($, { repo: shown, note: null, check: { result: 'invalid', repo: shown, text: INVALID_REPO.replace('{repo}', shown) } })
            return
          }
          await setSettings($, { repo: name, check: null, note: null })
          // `--`: a name that starts with a dash is a name, not an option of the script
          const run = await runSettings($, ['check-repo', '--', name])
          if (run.status === 'ok' && typeof run.answer.result === 'string') {
            await setSettings($, { check: { result: run.answer.result, repo: name, text: String(run.answer.text ?? '') } })
          } else {
            await setSettings($, { note: noteOf(run) })
          }
        }
        const save = async () => {
          if (!validRepo(repo)) return      // never offered then (canSave), and never run
          const run = await runSettings($, ['set-repo', '--', repo], SAVE_TIMEOUT_MS)
          const text = run.status === 'ok' && typeof run.answer.text === 'string' ? run.answer.text : noteOf(run)
          await setSettings($, { check: null, repo: null, note: text })
          await loadSettings($)
        }
        // last_success is a UTC time, shown as the report shows its times; last_error is the category of the error
        const syncState = [sync.last_success === null ? null : `last success ${localTime(sync.last_success)}`,
          sync.last_error === null ? null : `last error ${sync.last_error}`].filter(part => part !== null).join(' · ')
        // The desktop draws a button of its own beside the field, which submits it: named Check, it is the one Check
        // there. The terminal names Enter beside the field and draws Check as a button
        const input = (
          <Input key="repo" label="owner/name" value={repo} placeholder="owner/name" submitLabel={isDesktop ? 'Check' : 'check'}
            onInput={(value: string) => setSettings($, { repo: value })} onSubmit={(value: string) => checkRepo(value)} />
        )
        const parts = shown.modules.map(partRow).filter(row => row !== null)
        body = [
          ...(isDesktop ? [grid('parts', parts)] : parts),
          ...(!sync.installed ? [absent('sync', 'Sync', sync.summary)] : [
            container('row-sync', [
              heading('sync', 'Sync', true, badgeOf(sync.mode), infoInHead('sync')),
              block('sync', [
                sentence('sync', sync.summary),
                controls('sync', [...group('sync', sync.choices.map(choice => choiceButton('sync', 'sync.mode', choice, sync.mode, ''))), ...infoInRow('sync')]),
                ...(syncState === '' ? [] : [<Text key="sync-state" {...quiet}>{syncState}</Text>]),
                ...infoText('sync', sync.info, sync.tab),
              ]),
            ]),
            container('row-repo', [
              heading('repo', 'Sync repository', true),
              block('repo', [
                // The terminal draws the field in a frame of its own, so that it reads as a field
                controls('repo', [
                  isDesktop ? input : <Box key="repo-field" borderStyle="round" paddingX={1}>{input}</Box>,
                  ...(isDesktop ? [] : [actionButton('check', 'Check', () => checkRepo(repo))]),
                  ...(canSave ? [marked('save', true, actionButton('save', 'Save', save))] : []),
                  infoButton('repo'),
                ], { alignItems: 'center' }),
                ...(settings.check === null ? [] : [
                  <Text key="check-text" color={settings.check.result === 'private_ok' ? 'success' : 'warning'}>{settings.check.text}</Text>,
                ]),
                ...infoText('repo', sync.repo_info, sync.repo_tab),
              ]),
            ]),
          ]),
          ...shown.problems.map((problem, i) => <Text key={`problem-${i}`} color="warning">{`config.json: ${problem}`}</Text>),
          ...(settings.note === null ? [] : [<Text key="note" color="warning">{settings.note}</Text>]),
        ]
      }
    }

    // Like the tab row of /config: the views on the left, the period and Refresh on the right
    return (
      <Box flexDirection="column">
        <Box flexDirection="row" flexWrap="wrap" justifyContent="space-between" columnGap={2}>
          <Box flexDirection="row" flexWrap="wrap" columnGap={1}>
            {VIEWS.map(v => viewTab(v.id, v.hotkey, v.label))}
            {settingsTab()}
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
