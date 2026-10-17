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
}

declare module 'claude-code' {
  interface PluginState {
    'usage-clock-display': {
      usage: Usage | null
      config: LineConfig
      lastAnswer: number | null
      tick: number
    }
  }
}
