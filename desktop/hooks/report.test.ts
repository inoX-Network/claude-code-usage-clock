import { describe, expect, test } from 'claude-code/testing'

import { REPORT } from './report-fixture'
import { VIEWS, parsePeriod, parseReport, reportArgv, viewLines } from './report'
import type { View } from './report'

const LINUX = 'python3 /home/you/.claude/hooks/usage-clock/statusline.py'
const WINDOWS = '"C:/Python314/python.exe" -X utf8 "C:/Users/you/.claude/hooks/usage-clock/statusline.py"'

function lines(view: View, columns = 100): string[] {
  const report = parseReport(JSON.stringify(REPORT))
  if ('error' in report) throw new Error(report.error)
  return viewLines(view, report.report, columns).map(line => line.text)
}

describe('parsePeriod', () => {
  test('takes the four periods, in any case and with blanks', async () => {
    expect(parsePeriod('day')).toBe('day')
    expect(parsePeriod(' Month ')).toBe('month')
    expect(parsePeriod('YEAR')).toBe('year')
    expect(parsePeriod('week')).toBe('week')
  })

  test('no argument or an unknown one means the week', async () => {
    expect(parsePeriod('')).toBe('week')
    expect(parsePeriod('fortnight')).toBe('week')
    expect(parsePeriod('week month')).toBe('week')
  })
})

describe('reportArgv', () => {
  test('swaps the status line for the report beside it, keeping the interpreter', async () => {
    expect(reportArgv(LINUX, 'week')).toEqual({
      argv: ['python3', '/home/you/.claude/hooks/usage-clock/usage_report.py', '--week', '--json'],
      script: '/home/you/.claude/hooks/usage-clock/usage_report.py',
    })
  })

  test('keeps the options and the quoting of a Windows command', async () => {
    expect(reportArgv(WINDOWS, 'year')?.argv).toEqual([
      'C:/Python314/python.exe', '-X', 'utf8', 'C:/Users/you/.claude/hooks/usage-clock/usage_report.py', '--year', '--json',
    ])
  })

  test('asks for the period it is given', async () => {
    for (const period of ['day', 'week', 'month', 'year'] as const) {
      expect(reportArgv(LINUX, period)?.argv.slice(-2)).toEqual([`--${period}`, '--json'])
    }
  })

  test('drops words after the script: they were meant for the status line', async () => {
    expect(reportArgv(`${LINUX} --refresh`, 'day')?.argv).toEqual([
      'python3', '/home/you/.claude/hooks/usage-clock/usage_report.py', '--day', '--json',
    ])
  })

  test('a script without a folder stays without one', async () => {
    expect(reportArgv('python3 statusline.py', 'day')?.argv).toEqual(['python3', 'usage_report.py', '--day', '--json'])
  })

  for (const [name, command] of [
    ['another status line', 'npx ccusage statusline'],
    ['shell syntax', `${LINUX} | tee /tmp/out`],
    ['two status line scripts', `${LINUX} /other/statusline.py`],
    ['the script as the program', '/home/you/.claude/hooks/usage-clock/statusline.py'],
    ['an open quote', 'python3 "/home/you/statusline.py'],
    ['nothing', undefined],
    ['a number', 42],
  ] as const) {
    test(`${name}: no command`, async () => {
      expect(reportArgv(command, 'week')).toBeNull()
    })
  }
})

describe('parseReport', () => {
  test('reads the report', async () => {
    const parsed = parseReport(JSON.stringify(REPORT))
    expect('report' in parsed).toBe(true)
  })

  test('text that is no JSON gives an error line', async () => {
    const parsed = parseReport('Traceback (most recent call last):')
    expect('error' in parsed && parsed.error).toMatch(/not valid JSON/)
  })

  test('JSON that is no report gives an error line', async () => {
    for (const text of ['[1, 2]', 'null', '"x"', '{}']) {
      const parsed = parseReport(text)
      expect('error' in parsed && parsed.error).toMatch(/not a usage report/)
    }
  })

  test('another format number gives an error line naming it', async () => {
    const parsed = parseReport(JSON.stringify({ ...REPORT, schema_version: 2 }))
    expect('error' in parsed && parsed.error).toMatch(/schema_version 2/)
  })
})

describe('the views', () => {
  test('there are six, with the keys a to f', async () => {
    expect(VIEWS.map(view => view.hotkey)).toEqual(['a', 'b', 'c', 'd', 'e', 'f'])
    expect(VIEWS.map(view => view.label)).toEqual(['Overview', 'Agents', 'Models', 'Cache/Thinking', 'Tokens', 'Projects'])
  })

  test('overview: the numbers of the text overview', async () => {
    const text = lines('overview').join('\n')
    expect(text).toContain('Week to Mon 19 Oct 09:00 · 39 % of the weekly limit used (+39 points) · 100 USD at API prices')
    expect(text).toMatch(/5h windows: 1, highest 82 % \(/)
    expect(text).toMatch(/project\s+USD\s+share\s+~week pts\s+sessions/)
    expect(text).toMatch(/webshop\s+60\s+60 %\s+20\.0\s+1\b/)
    expect(text).toMatch(/1 more\s+0\.40\s+<1 %\s+<1\s+2\b/)
    expect(text).not.toMatch(/scratch/)
    expect(text).toContain('Main 75 USD · subagents 25 USD (top: stripe-meister 20 USD, ~6.6 week pts)')
    expect(text).toContain('Opus 92 USD · Sonnet 8.0 USD · 3 requests without a price · thinking 25 % of output · cache hits 89 %')
    expect(text).toContain('Start: main 45k tokens (median), subagents 36k-61k (median per type)')
    expect(text).toMatch(/Notable\n\s+one 5h window reached 82 % \(Tue 13 Oct\)/)
  })

  test('overview: a week with several windows and no 5h measurement', async () => {
    const report = {
      ...REPORT,
      overview: {
        ...REPORT.overview,
        week: { used_percent: 12, rise_points: 55.5, windows: 2, reset_at: null },
        five_hour: { windows: 0, highest_percent: null, highest_at: null },
        notable: [],
      },
    }
    const parsed = parseReport(JSON.stringify(report))
    if ('error' in parsed) throw new Error(parsed.error)
    const text = viewLines('overview', parsed.report, 100).map(line => line.text).join('\n')
    expect(text).toContain('weekly limit at 12 % now · +55.5 points over 2 weekly windows')
    expect(text).toContain('5h windows: none measured')
    expect(text).not.toContain('Notable')
  })

  test('overview: no weekly measurement says so', async () => {
    const report = { ...REPORT, overview: { ...REPORT.overview, week: null } }
    const parsed = parseReport(JSON.stringify(report))
    if ('error' in parsed) throw new Error(parsed.error)
    const text = viewLines('overview', parsed.report, 100).map(line => line.text).join('\n')
    expect(text).toContain('no weekly measurement')
  })

  test('agents: main first, then the types by cost, with the estimated points of both limits', async () => {
    const text = lines('agents')
    const header = text.findIndex(line => /agent\s+USD\s+requests\s+~week pts\s+~5h pts/.test(line))
    expect(header).toBeGreaterThanOrEqual(0)
    expect(text[header + 1]).toMatch(/^main\s+75\s+300\s+30\.5\s+12\.5$/)
    expect(text[header + 2]).toMatch(/^stripe-meister\s+20\s+40\s+6\.6\s+2\.0$/)
    expect(text[header + 3]).toMatch(/^reviewer\s+5\.0\s+160\s+<1\s+0$/)
  })

  test('models: priced ones by cost, then the ones without a price', async () => {
    const text = lines('models')
    const header = text.findIndex(line => /model\s+USD\s+requests\s+thinking\s+cache hits/.test(line))
    expect(header).toBeGreaterThanOrEqual(0)
    expect(text[header + 1]).toMatch(/^Opus 5\.5\s+92\s+97\s+30 %\s+90 %$/)
    expect(text[header + 2]).toMatch(/^Sonnet 5\.x\s+8\.0\s+400\s+10 %\s+70 %$/)
    expect(text[header + 3]).toMatch(/^Mystery 1\s+-\s+3\s+n\/a\s+n\/a$/)
  })

  test('cache/thinking: hit rate, cold starts, thinking tokens and share', async () => {
    const text = lines('cache').join('\n')
    expect(text).toMatch(/Cache hit rate\s+89 %/)
    expect(text).toMatch(/Cold starts\s+3\b/)
    expect(text).toMatch(/Thinking tokens\s+123,000/)
    expect(text).toMatch(/Thinking share\s+25 % of output/)
  })

  test('tokens: the five kinds', async () => {
    const text = lines('tokens').join('\n')
    expect(text).toMatch(/input\s+1,200/)
    expect(text).toMatch(/cache write 5m\s+3,400/)
    expect(text).toMatch(/cache write 1h\s+5,600,000/)
    expect(text).toMatch(/cache read\s+78,900,000/)
    expect(text).toMatch(/output\s+123,456/)
  })

  test('projects: every project, not only the first five', async () => {
    const text = lines('projects')
    expect(text.join('\n')).toMatch(/project\s+USD\s+share\s+~week pts\s+sessions/)
    expect(text.some(line => /^webshop\s+60\s+60 %\s+20\.0\s+1$/.test(line))).toBe(true)
    expect(text.some(line => /^blog\s+9\.0\s+9 %\s+3\.0\s+2$/.test(line))).toBe(true)
    expect(text.some(line => /^scratch\s+0\.40\s+<1 %\s+<1\s+2$/.test(line))).toBe(true)
  })

  test('a long name loses its middle where the width is short, and stays whole where there is room', async () => {
    const name = 'my-company-frontend-admin-dashboard'
    expect(lines('projects', 100).some(line => line.startsWith(name))).toBe(true)
    const narrow = lines('projects', 40).find(line => line.includes('…'))
    expect(narrow).toBeDefined()
    expect(narrow).toContain('…')
    expect((narrow ?? '').length).toBeLessThanOrEqual(40)
  })

  test('a report with nothing in it gives a line, never a failure', async () => {
    const parsed = parseReport(JSON.stringify({ schema_version: 1 }))
    if ('error' in parsed) throw new Error(parsed.error)
    for (const view of VIEWS) {
      expect(viewLines(view.id, parsed.report, 80).length).toBeGreaterThan(0)
    }
  })
})
