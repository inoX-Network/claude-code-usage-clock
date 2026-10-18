// What the context of a session holds after its first answer, as /context shows it: the categories, every MCP tool,
// memory file, skill and agent with its tokens. The transcripts have only the sum of each request, so the plugin
// writes this down once per session, one file per session in <log_dir>/context-breakdown/ (context/ holds the status
// line's state files), for tools/usage_report.py to read. Names and numbers only, never content.

import type { EngineInterface, SessionContextBreakdown } from 'claude-code'

export const CONTEXT_DIR = 'context-breakdown'

/** The file of one session, in the format docs/report-format.md describes (`clis[].context`). */
export function contextRecord(breakdown: SessionContextBreakdown, sessionId: string, cwd: string, at: string) {
  const skills = breakdown.skills
  return {
    schema_version: 1,
    session_id: sessionId,
    at,
    cwd,
    model: breakdown.model,
    detail: 'full',
    total_tokens: breakdown.totalTokens,
    max_tokens: breakdown.maxTokens,
    categories: breakdown.categories.map(row => ({ name: row.name, tokens: row.tokens, kind: row.kind })),
    memory_files: breakdown.memoryFiles.map(file => ({ path: file.path, type: file.type, tokens: file.tokens })),
    mcp_tools: breakdown.mcpTools.map(tool => ({ server: tool.serverName, name: tool.name, tokens: tool.tokens, loaded: tool.isLoaded })),
    skills: skills === undefined
      ? null
      : {
          tokens: skills.tokens,
          included: skills.includedSkills,
          total: skills.totalSkills,
          items: skills.skillFrontmatter.map(skill => ({ name: skill.name, tokens: skill.tokens })),
        },
    agents: breakdown.agents.map(agent => ({ type: agent.agentType, source: agent.source, tokens: agent.tokens })),
  }
}

/** Measures the context as /context does (one token count per tool and memory file) and writes the session's file.
 *  A session without a breakdown writes nothing. */
export async function recordContext($: EngineInterface, logDir: string): Promise<void> {
  const breakdown = (await $.session.usage({ breakdown: 'full' })).context.breakdown
  if (breakdown === undefined) return
  const sessionId = await $.session.id()
  const record = contextRecord(breakdown, sessionId, await $.session.cwd(), new Date(await $.clock.now()).toISOString())
  await $.fs.write(`${logDir.replace(/[\\/]+$/, '')}/${CONTEXT_DIR}/${sessionId}.json`, `${JSON.stringify(record, null, 2)}\n`)
}
