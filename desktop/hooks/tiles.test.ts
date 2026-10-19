import { describe, expect, test } from 'claude-code/testing'

import { REPORT } from './report-fixture'
import { LOOK, gaugeSvg, overviewTiles, peakScale, peaksSvg, sessionCards, sessionTiles, tilesPerRow } from './tiles'

const LIMITS = { yellowFrom: 50, redFrom: 75 }

describe('overviewTiles', () => {
  test('the four key figures: weekly limit, 5h windows, cost, cache', async () => {
    const { kpis } = overviewTiles(REPORT, LIMITS)
    expect(kpis.map(k => k.id)).toEqual(['week', 'five', 'cost', 'cache'])
    const [week, five, cost, cache] = kpis
    expect(week).toMatchObject({ value: '39', gauge: 39, lines: ['+39.0 pts in this period', expect.stringMatching(/^reset Mon \d\d:\d\d$/)] })
    expect(week?.tone).toBeUndefined()
    expect(five).toMatchObject({ value: '82', tone: 'error', peaks: [82, 20], lines: [expect.stringMatching(/^1 window · peak Tue 13 Oct/)] })
    expect(cost).toMatchObject({ value: '100', unit: 'USD', split: [0.75, 0.25], lines: ['main 75 · agents 25'] })
    expect(cache).toMatchObject({ value: '89', lines: ['cold starts 3', 'thinking 25 %', 'start 45k (median)'] })
  })

  test('bars: the largest first and as long as the tile, the others in proportion', async () => {
    const t = overviewTiles(REPORT, LIMITS)
    expect(t.projects.map(b => [b.label, b.value])).toEqual([
      ['webshop', '60 · 20.0'], ['my-company-frontend-admin-dashboard', '20 · 6.5'], ['blog', '9.0 · 3.0'],
      ['tools', '5.3 · 1.2'], ['docs', '5.0 · 1.5'],
    ])
    expect(t.projects[0]?.length).toBe(1)
    expect(Math.round((t.projects[1]?.length ?? 0) * 100)).toBe(33)
    expect(t.more).toBe('1 more · 0.40 USD · 2 sessions')
    expect(t.models.map(b => b.label)).toEqual(['Opus', 'Sonnet'])
    expect(t.agents.map(b => [b.label, b.value])).toEqual([['main', '75'], ['stripe-meister', '20'], ['reviewer', '5.0']])
  })

  test('what is notable gets a tag by its rule, an unknown rule NOTE', async () => {
    const report = { ...REPORT, overview: { ...REPORT.overview, notable: [...REPORT.overview.notable, { rule: 'new_rule', text: 'something new' }] } }
    expect(overviewTiles(report, LIMITS).notable).toEqual([
      { tag: '5H', text: 'one 5h window reached 82 % (Tue 13 Oct)' },
      { tag: 'NOTE', text: 'something new' },
    ])
  })

  test('a report without a weekly measurement or 5h window says so instead of a figure', async () => {
    const report = { ...REPORT, overview: { ...REPORT.overview, week: null, five_hour: { windows: 0, highest_percent: null, highest_at: null } } }
    const [week, five] = overviewTiles(report, LIMITS).kpis
    expect(week).toMatchObject({ value: '-', lines: ['no weekly measurement'] })
    expect(week?.gauge).toBeUndefined()
    expect(five).toMatchObject({ value: '-', lines: ['none measured'] })
  })
})

describe('sessionCards', () => {
  test('highest cost first; the own session comes first and is marked', async () => {
    expect(sessionCards(REPORT, null).map(c => [c.id, c.isOwn])).toEqual([['aaaaaaaa', false], ['bbbbbbbb', false]])
    const cards = sessionCards(REPORT, 'bbbbbbbb')
    expect(cards.map(c => [c.id, c.isOwn])).toEqual([['bbbbbbbb', true], ['aaaaaaaa', false]])
    expect(cards[1]).toMatchObject({ title: 'webshop aaaaaaaa', cost: '60 USD' })
    expect(cards[1]?.meta).toMatch(/^\S+ 13 Oct \d\d:\d\d · 300 requests · cache 90 % · week: ~20\.0 pts$/)
  })
})

describe('sessionTiles', () => {
  test('figures, tokens, models, subagents, then the context; free space is left out', async () => {
    const tiles = sessionTiles(REPORT, 'aaaaaaaa')
    expect(tiles.map(t => t.key)).toEqual(['figures', 'tokens', 'models', 'subagents', 'context', 'mcp-servers', 'mcp-tools', 'memory', 'skills', 'context-agents'])
    const context = tiles.find(t => t.key === 'context')
    expect(context?.kind === 'bars' && context.lead).toBe('Context after the first answer: 63,000 tokens of 1,000,000 (6 %)')
    expect(context?.kind === 'bars' && context.bars.map(b => b.label)).toEqual(['MCP tools', 'System prompt', 'Memory files'])
  })

  test('more than ten entries: the ten largest and a bar for the rest', async () => {
    const items = Array.from({ length: 12 }, (_, i) => ({ name: `skill-${i}`, tokens: 100 - i }))
    const cli = { ...REPORT.clis[0], context: { ...REPORT.clis[0]?.context, skills: { tokens: 1000, included: 12, total: 12, items } } }
    const skills = sessionTiles({ ...REPORT, clis: [cli] }, 'aaaaaaaa').find(t => t.key === 'skills')
    const labels = skills?.kind === 'bars' ? skills.bars.map(b => [b.label, b.value]) : []
    expect(labels).toHaveLength(11)
    expect(labels[10]).toEqual(['2 more', '179'])
  })

  test('a session from before the plugin says it has no context; one not in the report says so', async () => {
    expect(sessionTiles(REPORT, 'bbbbbbbb').at(-1)).toMatchObject({ kind: 'note', key: 'no-context' })
    expect(sessionTiles(REPORT, 'cccccccc')).toEqual([expect.objectContaining({ kind: 'note', key: 'gone' })])
  })
})

describe('points that are not measured', () => {
  const unmeasured = {
    ...REPORT,
    overview: { ...REPORT.overview, projects: REPORT.overview.projects.map(p => (p.name === 'blog'
      ? { ...p, week_points: 0, five_points: 0, measured_share: { five: 0, week: 0 } }
      : { ...p, measured_share: { five: 1, week: 1 } })) },
    clis: REPORT.clis.map(c => (c.id === 'bbbbbbbb'
      ? { ...c, estimated_share: { five: 0, week: 0 }, measured_share: { five: 0, week: 0 } }
      : { ...c, measured_share: { five: 1, week: 1 } })),
  }

  test('show a dash, a measured zero stays 0, a report without the field shows the figure', async () => {
    expect(overviewTiles(unmeasured, LIMITS).projects.find(b => b.label === 'blog')?.value).toBe('9.0 · -')
    expect(sessionCards(unmeasured, null).find(c => c.id === 'bbbbbbbb')?.meta).toMatch(/· week: - pts$/)
    expect(sessionTiles(unmeasured, 'bbbbbbbb')[0]).toMatchObject({ kind: 'kpis' })
    const figures = sessionTiles(unmeasured, 'bbbbbbbb')[0]
    expect(figures?.kind === 'kpis' && figures.kpis[0]?.lines).toEqual(['week: - pts', '5h: - pts'])
    const zero = { ...unmeasured, clis: unmeasured.clis.map(c => ({ ...c, measured_share: { five: 1, week: 1 } })) }
    expect(sessionCards(zero, null).find(c => c.id === 'bbbbbbbb')?.meta).toMatch(/· week: 0 pts$/)
    expect(sessionCards(REPORT, null).find(c => c.id === 'bbbbbbbb')?.meta).toMatch(/· week: ~3\.0 pts$/)
  })
})

describe('tilesPerRow', () => {
  test('one tile on a narrow pane, two at a medium width, four on a wide one', async () => {
    expect(tilesPerRow(30)).toBe(1)
    expect(tilesPerRow(43)).toBe(1)
    expect(tilesPerRow(44)).toBe(2)
    expect(tilesPerRow(95)).toBe(2)
    expect(tilesPerRow(96)).toBe(4)
  })
})

describe('the drawings', () => {
  test('the gauge takes the colour of the threshold, the accent below it', async () => {
    expect(gaugeSvg(39, LIMITS)).toContain(`stroke="${LOOK.accent}"`)
    expect(gaugeSvg(60, LIMITS)).toContain(`stroke="${LOOK.warning}" stroke-width="10"`)
    expect(gaugeSvg(94, LIMITS)).toContain(`stroke="${LOOK.error}"`)
    expect(gaugeSvg(94, LIMITS)).toContain('>94%</text>')
    expect(gaugeSvg(0, LIMITS).match(/<path/g)).toHaveLength(1)
  })

  test('the columns: one per window, the highest in the accent colour', async () => {
    const svg = peaksSvg([22, 27, 7], LIMITS)
    expect(svg.match(/<rect/g)).toHaveLength(3)
    expect(svg.match(new RegExp(`fill="${LOOK.accent}"`, 'g'))).toHaveLength(1)
  })

  test('the scale ends at the highest peak rounded up to ten, at least 50 %, at most 100 %', async () => {
    expect(peakScale([22, 27, 7])).toBe(50)
    expect(peakScale([3, 61])).toBe(70)
    expect(peakScale([82, 20])).toBe(90)
    expect(peakScale([100])).toBe(100)
    expect(peakScale([])).toBe(50)
    // 27 % on a scale to 50 % is more than half the height, not a quarter of it
    const height = Number(/height="([\d.]+)" rx="2" fill="#c47a3a"/.exec(peaksSvg([22, 27, 7], LIMITS))?.[1])
    expect(Math.round(height)).toBe(24)
  })

  test('a dashed line where yellow starts, when it lies on the scale', async () => {
    expect(peaksSvg([22, 27], LIMITS)).toContain(`stroke="${LOOK.warning}" stroke-width="1" stroke-dasharray="3 3"`)
    expect(peaksSvg([22, 27], { yellowFrom: 60, redFrom: 80 })).not.toContain('stroke-dasharray')
  })
})
