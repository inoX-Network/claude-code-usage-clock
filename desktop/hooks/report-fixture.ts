// A fixed example of `usage_report.py --json` for the tests, in the shape docs/report-format.md describes. The
// numbers are made up and chosen so that every formatting rule shows: USD with two, one and no decimals, points
// below one, a share below one percent, a value that is not known (null).

const tokens = { input: 1200, cache_write_5m: 3400, cache_write_1h: 5600000, cache_read: 78900000, output: 123456 }

export const REPORT = {
  schema_version: 1,
  period: { kind: 'week', label: 'week to Mon 19 Oct 09:00', start: '2026-10-12T07:00:00Z', end: '2026-10-19T07:00:00Z' },
  coverage: { transcript_files: 12, archive_days: [], archive_days_without_agent_split: [] },
  overview: {
    title: 'Week to Mon 19 Oct 09:00',
    cost_usd: 100,
    week: { used_percent: 39, rise_points: 39, windows: 1, reset_at: 1792393200 },
    five_hour: { windows: 1, highest_percent: 82, highest_at: '2026-10-13T12:30:00Z' },
    projects: [
      { name: 'webshop', cost_usd: 60, share: 0.6, week_points: 20, five_points: 8, sessions: 1, cache_hit_rate: 0.9 },
      { name: 'my-company-frontend-admin-dashboard', cost_usd: 20, share: 0.2, week_points: 6.5, five_points: 2, sessions: 3, cache_hit_rate: 0.8 },
      { name: 'blog', cost_usd: 9, share: 0.09, week_points: 3, five_points: 1, sessions: 2, cache_hit_rate: 0.95 },
      { name: 'docs', cost_usd: 5, share: 0.05, week_points: 1.5, five_points: 0.5, sessions: 1, cache_hit_rate: 0.95 },
      { name: 'tools', cost_usd: 5.25, share: 0.0525, week_points: 1.2, five_points: 0.4, sessions: 1, cache_hit_rate: 0.9 },
      { name: 'scratch', cost_usd: 0.4, share: 0.004, week_points: 0.3, five_points: 0, sessions: 2, cache_hit_rate: null },
    ],
    max_projects: 5,
    more: { projects: 1, cost_usd: 0.4, share: 0.004, week_points: 0.3, five_points: 0, sessions: 2 },
    split: { main_usd: 75, subagents_usd: 25, top_subagent: { type: 'stripe-meister', cost_usd: 20, week_points: 6.6 } },
    models: { families: [{ name: 'Opus', cost_usd: 92 }, { name: 'Sonnet', cost_usd: 8 }], unpriced_requests: 3 },
    thinking_share: 0.25,
    cache_hit_rate: 0.89,
    startup: { main_median_tokens: 45000, subagent_type_median_tokens: { min: 36000, max: 61000 } },
    notable: [{ rule: 'five_hour_window', text: 'one 5h window reached 82 % (Tue 13 Oct)' }],
    details: { project: 'webshop', session: 'aaaaaaaa' },
  },
  total: {
    requests: 500,
    tokens,
    cost_usd: 100,
    main: { requests: 300, cost_usd: 75, week_points: 30.5, five_points: 12.5 },
    subagents: {
      requests: 200,
      cost_usd: 25,
      by_type: {
        reviewer: { requests: 160, cost_usd: 5, week_points: 0.4, five_points: 0 },
        'stripe-meister': { requests: 40, cost_usd: 20, week_points: 6.6, five_points: 2 },
      },
    },
    models: {
      'Sonnet 5.x': { requests: 400, cost_usd: 8, tokens, cache_hit_rate: 0.7, thinking_tokens: 1000, thinking_share: 0.1 },
      'Opus 5.5': { requests: 97, cost_usd: 92, tokens, cache_hit_rate: 0.9, thinking_tokens: 122000, thinking_share: 0.3 },
    },
    unpriced: {
      'Mystery 1': { requests: 3, tokens, cache_hit_rate: null, thinking_tokens: null, thinking_share: null },
    },
    cache_hit_rate: 0.89,
    cold_starts: 3,
    thinking_tokens: 123000,
    thinking_share: 0.25,
    web_searches: null,
    web_fetches: null,
  },
  // Two sessions: one with the context its plugin wrote down after the first answer, one from before the plugin
  clis: [
    {
      id: 'aaaaaaaa', label: 'webshop', start: '2026-10-13T08:00:00Z', requests: 300, cost_usd: 60, tokens,
      models: { 'Opus 5.5': 55, 'Sonnet 5.x': 5 }, subagents: { 'stripe-meister': { requests: 40, cost_usd: 20 } },
      cache_hit_rate: 0.9, cold_starts: 1, thinking_tokens: 90000, thinking_share: 0.3, web_searches: null,
      web_fetches: null, estimated_share: { five: 8, week: 20 },
      context: {
        schema_version: 1, at: '2026-10-13T08:01:00Z', cwd: '/home/you/webshop', model: 'claude-opus-5-5', detail: 'full',
        total_tokens: 63000, max_tokens: 1000000,
        categories: [
          { name: 'System prompt', tokens: 3100, kind: 'used' },
          { name: 'MCP tools', tokens: 12400, kind: 'used' },
          { name: 'Memory files', tokens: 2200, kind: 'used' },
          { name: 'Free space', tokens: 900000, kind: 'free' },
        ],
        memory_files: [{ path: '/home/you/.claude/CLAUDE.md', type: 'User', tokens: 2200 }],
        mcp_tools: [
          { server: 'context7', name: 'query-docs', tokens: 800, loaded: true },
          { server: 'context7', name: 'resolve-library-id', tokens: 700, loaded: true },
          { server: 'playwright', name: 'navigate', tokens: 450, loaded: false },
        ],
        skills: { tokens: 5600, included: 38, total: 40, items: [{ name: 'spec-graph', tokens: 310 }] },
        agents: [{ type: 'arbeiter-bauen', source: 'userSettings', tokens: 120 }],
      },
    },
    {
      id: 'bbbbbbbb', label: 'blog', start: '2026-10-14T19:30:00Z', requests: 40, cost_usd: 9, tokens,
      models: { 'Sonnet 5.x': 9 }, subagents: {}, cache_hit_rate: 0.95, cold_starts: 0, thinking_tokens: null,
      thinking_share: null, web_searches: null, web_fetches: null, estimated_share: { five: 1, week: 3 }, context: null,
    },
  ],
}
