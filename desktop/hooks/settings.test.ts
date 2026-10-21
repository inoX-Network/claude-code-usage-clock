import { expect, test } from 'claude-code/testing'

import { docsLabel, infoParagraphs } from './settings'

// The info of the spawn gate as _setup.INFO holds it: lines broken for a terminal, mid-sentence
const GATE_INFO = '  Checks every start of an agent, workflow or skill: no new block from 75 % of the 5-hour usage, a warning or\n'
  + '  a stop near the weekly limit, and model and effort in the agent definitions (thresholds in config.json).\n'
  + '  Writes: a PreToolUse hook (spawn_gate.py, matcher Agent|Workflow|Skill) in\n'
  + '  settings.json.\n'
  + '  More: https://x/blob/main/docs/gates.md#the-two-gates'

test('the info joins the lines of a terminal into paragraphs, a new one before Writes: and More:', () => {
  expect(infoParagraphs(GATE_INFO)).toEqual([
    'Checks every start of an agent, workflow or skill: no new block from 75 % of the 5-hour usage, a warning or a stop '
      + 'near the weekly limit, and model and effort in the agent definitions (thresholds in config.json).',
    'Writes: a PreToolUse hook (spawn_gate.py, matcher Agent|Workflow|Skill) in settings.json.',
    'More: https://x/blob/main/docs/gates.md#the-two-gates',
  ])
  expect(infoParagraphs('  One.\n\n  Two.')).toEqual(['One. Two.'])
  expect(infoParagraphs('')).toEqual([])
})

test('a link to the docs is named by its file and anchor', () => {
  expect(docsLabel('https://github.com/o/r/blob/main/docs/sync.md#what-it-does')).toBe('Docs: sync.md#what-it-does')
  expect(docsLabel('https://github.com/o/r/blob/main/docs/sync.md')).toBe('Docs: sync.md')
  expect(docsLabel('https://x/docs#spawn_gate')).toBe('Docs: docs#spawn_gate')
})
