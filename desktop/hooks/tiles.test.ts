import { describe, expect, test } from 'claude-code/testing'

import { REPORT } from './report-fixture'
import { LOOK, gaugeSvg, overviewTiles, peaksSvg, sessionCards, tilesPerRow } from './tiles'

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
    expect(cards[1]?.meta).toMatch(/^\S+ 13 Oct \d\d:\d\d · 300 requests · cache 90 % · ~20\.0 week pts$/)
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
    const svg = peaksSvg([22, 27, 7])
    expect(svg.match(/<rect/g)).toHaveLength(3)
    expect(svg.match(new RegExp(`fill="${LOOK.accent}"`, 'g'))).toHaveLength(1)
  })
})
