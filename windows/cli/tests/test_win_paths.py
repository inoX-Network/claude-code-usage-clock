"""Path normalisation for paths that come out of hook input (backslashes, drive letters, Git Bash spellings)."""
import pytest

import _win


@pytest.mark.parametrize('given, expected', [
    ('C:\\Users\\Bjoer\\proj', 'C:/Users/Bjoer/proj'),
    ('C:/Users/Bjoer/proj', 'C:/Users/Bjoer/proj'),
    ('c:\\users\\x', 'C:/users/x'),                       # only the drive letter is upper-cased, never the rest
    ('C:\\Users\\x\\', 'C:/Users/x'),                     # no trailing separator ...
    ('a/', 'a'),                                          # also on a short relative path
    ('C:\\', 'C:/'),                                      # ... except on a drive root
    ('C:/', 'C:/'),
    ('C:', 'C:'),                                         # a drive-relative path stays one
    ('C:\\a\\\\b//c', 'C:/a/b/c'),                        # doubled separators collapse
    ('C:\\a\\..\\b', 'C:/a/../b'),                        # nothing is resolved: callers still look for ".."
    ('\\\\?\\C:\\Users\\x', 'C:/Users/x'),                # the long-path prefix goes
    ('\\\\?\\Z:\\x', 'Z:/x'),                             # a drive late in the alphabet is no UNC path
    ('\\\\?\\UNC\\server\\share\\d', '//server/share/d'),
    ('\\\\server\\share\\dir\\file.txt', '//server/share/dir/file.txt'),
    ('/c/Users/x', 'C:/Users/x'),                         # the Git Bash spelling of a drive
    ('/d', 'D:/'),
    ('/c/', 'C:/'),
    ('/tmp/x', '/tmp/x'),                                 # only a single letter is a drive
    ('/cc/x', '/cc/x'),
    ('a\\b\\c.txt', 'a/b/c.txt'),
    ('', ''),
    ('/', '/'),
])
def test_normalize_path(given, expected):
    assert _win.normalize_path(given) == expected


def test_normalising_twice_changes_nothing():
    for path in ('C:\\Users\\x\\', '/c/a', '\\\\?\\C:\\a', '\\\\srv\\sh\\d', 'rel\\x'):
        once = _win.normalize_path(path)
        assert _win.normalize_path(once) == once
