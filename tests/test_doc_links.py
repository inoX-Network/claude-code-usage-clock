"""Every relative link in the documentation points to a file that exists, and every #anchor to a heading or a named
anchor in that file. Links to old README sections keep working through the named anchors under "Moved sections"."""
import glob
import os
import re

from conftest import ROOT

LINK = re.compile(r'(?<!\!)\[[^\]]*\]\(([^)\s]+)\)|!\[[^\]]*\]\(([^)\s]+)\)')
NAMED = re.compile(r'<a (?:name|id)="([^"]+)"')
FENCE = re.compile(r'^\s*(```|~~~)')


def doc_files():
    paths = [os.path.join(ROOT, name) for name in ('README.md', 'CONTRIBUTING.md', 'SECURITY.md', 'CHANGELOG.md')]
    paths += glob.glob(os.path.join(ROOT, 'docs', '*.md'))
    paths += [os.path.join(ROOT, 'windows', 'README.md'), os.path.join(ROOT, 'desktop', 'README.md')]
    return [path for path in paths if os.path.isfile(path)]


def prose_lines(path):
    """The lines of a Markdown file outside fenced code blocks."""
    inside = False
    with open(path, encoding='utf-8') as f:
        for line in f.read().splitlines():
            if FENCE.match(line):
                inside = not inside
                continue
            if not inside:
                yield line


def slug(heading):
    """GitHub's anchor for a heading: lower case, punctuation dropped, spaces as hyphens."""
    text = re.sub(r'`', '', heading.strip().lower())
    text = re.sub(r'[^\w\- ]', '', text)
    return text.replace(' ', '-')


def anchors(path):
    seen = {}
    found = set()
    for line in prose_lines(path):
        match = re.match(r'(#{1,6}) (.+)', line)
        if match:
            base = slug(match.group(2))
            count = seen.get(base, 0)
            found.add(base if count == 0 else f'{base}-{count}')
            seen[base] = count + 1
        found |= set(NAMED.findall(line))
    return found


def links(path):
    for number, line in enumerate(prose_lines(path), 1):
        line = re.sub(r'`[^`]*`', '', line)          # inline code is no link
        for match in LINK.finditer(line):
            target = match.group(1) or match.group(2)
            if re.match(r'[a-z]+:', target):
                continue
            yield number, target


def test_every_relative_link_and_anchor_in_the_documentation_resolves():
    broken = []
    for path in doc_files():
        for number, target in links(path):
            file_part, _, anchor = target.partition('#')
            goal = os.path.normpath(os.path.join(os.path.dirname(path), file_part)) if file_part else path
            name = os.path.relpath(path, ROOT)
            if not os.path.exists(goal):
                broken.append(f'{name}: {target} (no such file)')
            elif anchor and goal.endswith('.md') and anchor not in anchors(goal):
                broken.append(f'{name}: {target} (no such anchor)')
    assert broken == []


def test_every_section_the_readme_used_to_have_still_has_its_anchor_there():
    old = ('install', 'uninstall-completely', 'what-the-model-sees', 'the-context-window', 'the-wake-up-watcher',
           'what-the-status-line-shows', 'in-the-claude-desktop-app', 'files-the-status-line-writes', 'the-two-gates',
           'from-shadow-to-enforce', 'reports-what-was-used', 'the-overview', 'the-report-pane',
           'what-a-session-started-with', 'ask-your-agent-about-your-usage', 'configuration',
           'model-names-effort-values-and-exceptions', 'recipe-wake-up-when-the-window-resets',
           'limits-and-threat-model', 'working-with-a-guard-hook', 'development', 'maintenance', 'license',
           'requirements')
    readme = anchors(os.path.join(ROOT, 'README.md'))
    assert [a for a in old if a not in readme] == []
