// The Settings tab of /report: the argv that runs hooks/settings_cli.py next to the status line, and what its JSON
// answers hold. Every text that explains a part comes from that script (show --json); the plugin has none of its own.

import { SCRIPT as STATUSLINE, statuslineArgv } from './record'

const SETTINGS_SCRIPT = 'settings_cli.py'

/** The one line the tab shows when the installation is older than the tab (no settings_cli.py next to the hooks). */
export const UPDATE_LINE = 'The Settings tab needs a newer installation: in the checkout run git pull, then python3 tools/install.py (on Windows python tools\\install.py).'

export type Part = {
  id: string
  label: string
  installed: boolean
  summary: string
  info: string
  tab: string
  /** The key `set` takes (`modules.usage_clock`), null for the status line, which has no switch. */
  key: string | null
  value: boolean | string | null
  choices: string[]
  /** Of a gate: the mode of its hook entry, which applies while `value` is empty. */
  installed_mode?: string | null
}

export type Sync = {
  installed: boolean
  repo: string
  mode: string
  choices: string[]
  summary: string
  last_success: string | null
  last_error: string | null
  info: string
  tab: string
  repo_info: string
  repo_tab: string
}

export type Shown = { modules: Part[]; sync: Sync; problems: string[] }

/** The settings script with `args` as words of their own, run by the interpreter of the status line; null if the
 *  command is not the usage-clock status line. */
export function settingsArgv(command: unknown, args: readonly string[]): { argv: string[]; script: string } | null {
  const argv = statuslineArgv(command)
  if (argv === null) return null
  const at = argv.findIndex(word => word.split(/[\\/]/).pop() === STATUSLINE)
  const word = argv[at] as string
  const script = word.slice(0, word.length - STATUSLINE.length) + SETTINGS_SCRIPT
  return { argv: [...argv.slice(0, at), script, ...args], script }
}

// owner/name as sync.repo takes it: _SYNC_REPO and valid_sync_repo of hooks/_config.py (tests/test_settings_cli.py
// compares them); the text of RESULTS['invalid'] in hooks/settings_cli.py
const SYNC_REPO = /^[A-Za-z0-9][A-Za-z0-9-]{0,38}\/[A-Za-z0-9._-]{1,100}$/
export const INVALID_REPO = '{repo} is no owner/name as on GitHub, for example anna/usage-sync.'
// C0 but the line break, DEL and C1, as hooks/settings_cli.py replaces them
const CONTROL = /[\u0000-\u0009\u000b-\u001f\u007f-\u009f]/g

/** A name the script would take as sync.repo: only such a name starts a process. */
export function validRepo(name: string): boolean {
  return SYNC_REPO.test(name) && !name.includes('..') && !name.endsWith('/.')
}

/** Every text of `value` without control characters: what the tab draws never carries an escape sequence. */
export function printable(value: unknown): unknown {
  if (typeof value === 'string') return value.replace(CONTROL, '?')
  if (Array.isArray(value)) return value.map(printable)
  if (typeof value === 'object' && value !== null) {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, printable(item)]))
  }
  return value
}

/** The JSON object the script prints, its texts printable; null for anything else. */
export function parseAnswer(stdout: string): Record<string, unknown> | null {
  try {
    const value: unknown = JSON.parse(stdout)
    return typeof value === 'object' && value !== null && !Array.isArray(value)
      ? (printable(value) as Record<string, unknown>) : null
  } catch {
    return null
  }
}

/** The answer of `show --json`, null if it is not one. */
export function parseShown(answer: Record<string, unknown>): Shown | null {
  const { modules, sync, problems } = answer
  if (!Array.isArray(modules) || typeof sync !== 'object' || sync === null) return null
  return { modules: modules as Part[], sync: sync as Sync, problems: Array.isArray(problems) ? (problems as string[]) : [] }
}

/** The choice of a part that is in effect: on or off, or the mode of a gate (that of its entry while it has none). */
export function activeChoice(part: Part): string {
  if (typeof part.value === 'boolean') return part.value ? 'true' : 'false'
  return part.value === '' || part.value === null ? (part.installed_mode ?? 'shadow') : part.value
}

/** What a button says: on and off for a switch, the mode itself otherwise. */
export function choiceLabel(choice: string): string {
  return choice === 'true' ? 'on' : choice === 'false' ? 'off' : choice
}

/** The info of a part as paragraphs. Its lines are broken for the terminal of the assistant, often mid-sentence: they
 *  are joined again, and a new paragraph starts only where the assistant starts `Writes:` or `More:`. */
export function infoParagraphs(info: string): string[] {
  const paragraphs: string[] = []
  for (const line of info.split('\n').map(line => line.trim()).filter(line => line !== '')) {
    if (paragraphs.length === 0 || line.startsWith('Writes:') || line.startsWith('More:')) {
      paragraphs.push(line)
    } else {
      paragraphs[paragraphs.length - 1] += ` ${line}`
    }
  }
  return paragraphs
}

/** What a link to the documentation says: `Docs: ` and its file with the anchor, the whole URL being the link. */
export function docsLabel(url: string): string {
  const at = url.indexOf('#')
  const path = at < 0 ? url : url.slice(0, at)
  return `Docs: ${path.split('/').pop() ?? ''}${at < 0 ? '' : url.slice(at)}`
}
