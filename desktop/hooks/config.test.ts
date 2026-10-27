import { expect, test } from 'claude-code/testing'

import { DEFAULTS, LOG_DIR, expandHome, parseConfig, parseLogDir } from './config'

test('the log folder: CCUC_LOG_DIR, else log_dir of config.json, else the default of hooks/_config.py', () => {
  expect(LOG_DIR).toBe('~/.cache/claude-usage-clock')
  expect(parseLogDir(undefined, undefined)).toBe(LOG_DIR)
  expect(parseLogDir('{ not json', undefined)).toBe(LOG_DIR)
  expect(parseLogDir(JSON.stringify({ log_dir: 7 }), undefined)).toBe(LOG_DIR)
  expect(parseLogDir(JSON.stringify({ log_dir: '' }), undefined)).toBe(LOG_DIR)
  expect(parseLogDir(JSON.stringify({ log_dir: '~/logs' }), undefined)).toBe('~/logs')
  expect(parseLogDir(JSON.stringify({ log_dir: '~/logs' }), '/tmp/x')).toBe('/tmp/x')
  expect(parseLogDir(JSON.stringify({ log_dir: '~/logs' }), '')).toBe('~/logs')
})

test('no file or broken JSON: the defaults of hooks/_config.py', () => {
  expect(parseConfig(undefined)).toEqual(DEFAULTS)
  expect(parseConfig('{ not json')).toEqual(DEFAULTS)
  expect(parseConfig('[]')).toEqual(DEFAULTS)
  expect(DEFAULTS).toEqual({
    yellowFrom: 50, redFrom: 75, weekStopMarker: true, showReset: true,
    contextYellowFromK: 300, contextRedFromK: 500, cacheYellowBelowMin: 5, startBlockWeek: 75, weekAction: 'warn',
    desktopLine: 'full',
  })
})

test('modules.desktop_line: off, short or full; anything else keeps full, as _CHOICES in _config.py', () => {
  for (const value of ['off', 'short', 'full'] as const) {
    expect(parseConfig(JSON.stringify({ modules: { desktop_line: value } })).desktopLine).toBe(value)
  }
  for (const value of ['wide', 'Full', true, 1, null]) {
    expect(parseConfig(JSON.stringify({ modules: { desktop_line: value } })).desktopLine).toBe('full')
  }
})

test('every valid key overrides its default', () => {
  const text = JSON.stringify({
    statusline: {
      yellow_from: 70, red_from: 85, week_stop_marker: false, show_reset: false,
      context_yellow_from_k: 200, context_red_from_k: 400, cache_yellow_below_min: 10,
    },
    thresholds: { start_block_week: 90, warn: 1, week_action: 'deny' },
    wake_watcher: { from_k: 300 },
    modules: { desktop_line: 'short' },
  })
  expect(parseConfig(text)).toEqual({
    yellowFrom: 70, redFrom: 85, weekStopMarker: false, showReset: false,
    contextYellowFromK: 200, contextRedFromK: 400, cacheYellowBelowMin: 10, startBlockWeek: 90, weekAction: 'deny',
    desktopLine: 'short',
  })
})

test('a key of the wrong type keeps its default, the others still count', () => {
  const text = JSON.stringify({ statusline: { yellow_from: '70', red_from: 90, show_reset: 1, context_red_from_k: null },
                                thresholds: { start_block_week: true } })
  expect(parseConfig(text)).toEqual({ ...DEFAULTS, redFrom: 90 })
  expect(parseConfig(JSON.stringify({ statusline: 'none', thresholds: [] }))).toEqual(DEFAULTS)
  expect(parseConfig('{"statusline": {"red_from": 1e999, "yellow_from": 60}}')).toEqual({ ...DEFAULTS, yellowFrom: 60 })
})

test('week_action: each choice of _config.py counts, anything else keeps warn', () => {
  const action = (value: unknown) => parseConfig(JSON.stringify({ thresholds: { week_action: value } })).weekAction
  expect(action('warn')).toBe('warn')
  expect(action('ask')).toBe('ask')
  expect(action('deny')).toBe('deny')
  expect(action('shadow')).toBe('shadow')
  expect(action('off')).toBe('off')
  expect(action('later')).toBe('warn')
  expect(action('ASK')).toBe('warn')
  expect(action(1)).toBe('warn')
  expect(action(null)).toBe('warn')
})

test('the home directory replaces a leading ~ only', () => {
  expect(expandHome('~/.claude/hooks/usage-clock/config.json', 'C:\\Users\\you'))
    .toBe('C:\\Users\\you/.claude/hooks/usage-clock/config.json')
  expect(expandHome('~/x', '/home/you/')).toBe('/home/you/x')
  expect(expandHome('~', '/home/you')).toBe('/home/you')
  expect(expandHome('/etc/config.json', '/home/you')).toBe('/etc/config.json')
  expect(expandHome('~other/x', '/home/you')).toBe('~other/x')
  expect(expandHome('~/x', undefined)).toBe('~/x')
})
