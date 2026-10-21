// Writes the numbers of the app into the shared usage file, which the hooks and gates read. The app does not run
// the statusLine, so nothing else would write them in a desktop session. The plugin does not write the file itself:
// $.fs.write is neither atomic nor locked. It hands the numbers to the installed hooks/statusline.py in the shape of
// its own input, so the same code merges them under the same lock, appends to the history at most once, and saves
// the context state of the session.

import type { SessionUsage } from 'claude-code'

export const SCRIPT = 'statusline.py'
const LIMITS = ['five_hour', 'seven_day']

type Block = { used_percentage: number; resets_at: number }

export type StatusInput = {
  session_id: string
  rate_limits: Record<string, Block>
  context_window: { context_window_size: number; total_input_tokens: number; used_percentage?: number }
}

/** What Claude Code hands the status line, built from the app's numbers. Reset times become Unix seconds; a window
 *  without a valid one is left out, as the merge would drop it. No prompt_cache: the app tells no expiry. */
export function statusInput(usage: Pick<SessionUsage, 'context' | 'rateLimits'>, sessionId: string): StatusInput {
  const limits: Record<string, Block> = {}
  for (const limit of usage.rateLimits) {
    const moment = limit.resetsAt === undefined ? NaN : Date.parse(limit.resetsAt)
    if (LIMITS.includes(limit.kind) && Number.isFinite(moment)) {
      limits[limit.kind] = { used_percentage: limit.percentUsed, resets_at: moment / 1000 }
    }
  }
  const { tokens, window, percent } = usage.context
  // 0 tokens means "not measured yet" to the status line, as before its own first answer
  const context: StatusInput['context_window'] = { context_window_size: window, total_input_tokens: tokens ?? 0 }
  if (percent !== undefined) context.used_percentage = percent
  return { session_id: sessionId, rate_limits: limits, context_window: context }
}

/** The words of a command, split like shlex.split (which install.py reads it with); null for shell syntax outside
 *  quotes or an open quote: such a command is not run without a shell. `plain` takes shell syntax as text, as
 *  shlex.split does (`|| true` of a hook entry is two words then), for a command that is only read. */
export function words(command: string, plain = false): string[] | null {
  const result: string[] = []
  let word: string | null = null
  for (let i = 0; i < command.length; i++) {
    const char = command[i] as string
    if (char === ' ' || char === '\t') {
      if (word !== null) result.push(word)
      word = null
    } else if (char === "'") {
      const end = command.indexOf("'", i + 1)
      if (end < 0) return null
      word = (word ?? '') + command.slice(i + 1, end)
      i = end
    } else if (char === '"') {
      let text = ''
      for (i++; i < command.length && command[i] !== '"'; i++) {
        if (command[i] === '\\' && i + 1 < command.length && '"\\$`'.includes(command[i + 1] as string)) i++
        text += command[i]
      }
      if (i >= command.length) return null
      word = (word ?? '') + text
    } else if (!plain && '|&;<>()$`\n'.includes(char)) {
      return null
    } else {
      if (char === '\\') {
        i++
        if (i >= command.length) return null
      }
      word = (word ?? '') + command[i]
    }
  }
  if (word !== null) result.push(word)
  return result
}

/** The argument vector of the usage-clock status line: an interpreter, its options and exactly one statusline.py;
 *  null for any other command. */
export function statuslineArgv(command: unknown): string[] | null {
  if (typeof command !== 'string') return null
  const argv = words(command)
  if (argv === null || argv.length < 2) return null
  const scripts = argv.filter(word => word.split(/[\\/]/).pop() === SCRIPT)
  return scripts.length === 1 && argv[0] !== scripts[0] ? argv : null
}
