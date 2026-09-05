"""curator.fuzzy_match — the strategy chain behind ``skill_manage action=patch``."""

from __future__ import annotations


def test_exact_and_ambiguity():
    from curator.fuzzy_match import fuzzy_find_and_replace
    content = "a\nb\nc\n"
    new, n, strategy, err = fuzzy_find_and_replace(content, "b", "B")
    assert (new, n, strategy, err) == ("a\nB\nc\n", 1, "exact", None)
    new, n, strategy, err = fuzzy_find_and_replace("x x", "x", "y")
    assert n == 0 and "Found 2 matches" in err and "L1: x x" in err
    new, n, strategy, err = fuzzy_find_and_replace("x x", "x", "y", replace_all=True)
    assert new == "y y" and n == 2


def test_empty_whitespace_identical_and_no_match():
    from curator.fuzzy_match import IDENTICAL_STRINGS_ERROR, fuzzy_find_and_replace
    assert fuzzy_find_and_replace("abc", "", "x")[3] == "old_string cannot be empty"
    assert "only whitespace" in fuzzy_find_and_replace("abc", "   ", "x")[3]
    assert fuzzy_find_and_replace("abc", "abc", "abc")[3] == IDENTICAL_STRINGS_ERROR
    assert fuzzy_find_and_replace("abc", "zzz", "x")[3] == "Could not find a match for old_string in the file"


def test_whitespace_and_indentation_strategies_reindent():
    from curator.fuzzy_match import fuzzy_find_and_replace
    content = "def f():\n    return  1\n"
    new, n, strategy, err = fuzzy_find_and_replace(content, "return 1", "return 2")
    assert err is None and strategy == "whitespace_normalized" and new == "def f():\n    return 2\n"
    content = "    if x:\n        y()\n"
    new, n, strategy, err = fuzzy_find_and_replace(content, "if x:\n    y()", "if x:\n    z()")
    assert err is None and strategy in {"line_trimmed", "indentation_flexible"}
    assert new == "    if x:\n        z()\n"


def test_escape_and_unicode_strategies():
    from curator.fuzzy_match import fuzzy_find_and_replace
    content = "line1\nline2\n"
    new, n, strategy, err = fuzzy_find_and_replace(content, "line1\\nline2", "L1\nL2")
    assert err is None and strategy == "escape_normalized" and new == "L1\nL2\n"
    content = "it’s — done\n"
    new, n, strategy, err = fuzzy_find_and_replace(content, "it's -- done", "it's -- finished")
    assert err is None and strategy == "unicode_normalized"
    assert new == "it’s — finished\n"  # file's typography preserved


def test_block_anchor_and_context_aware():
    from curator.fuzzy_match import fuzzy_find_and_replace
    content = "start\nmiddle line one\nmiddle line two\nend\n"
    pattern = "start\nmiddle line uno\nmiddle line two\nend"
    new, n, strategy, err = fuzzy_find_and_replace(content, pattern, "replaced")
    assert err is None and strategy == "block_anchor" and new == "replaced\n"
    content = "alpha beta\ngamma delta\n"
    new, n, strategy, err = fuzzy_find_and_replace(content, "alpha betas\ngamma delta", "x")
    assert err is None and strategy in {"block_anchor", "context_aware"}
    twice = "start\nmiddle line one\nend\nstart\nmiddle line one\nend\n"
    _, n, _, err = fuzzy_find_and_replace(twice, "start\nmiddle line uno\nend", "x", replace_all=True)
    assert n == 0 and "approximate matches" in err


def test_escape_drift_guards():
    from curator.fuzzy_match import fuzzy_find_and_replace
    content = "don't\n"
    _, n, _, err = fuzzy_find_and_replace(content, "don\\'t", "won\\'t")
    assert n == 0 and "Escape-drift detected" in err
    content = "a \\\\ b \\\\ c\n"
    _, n, _, err = fuzzy_find_and_replace(content, "a \\\\\\\\ b \\\\\\\\ c", "a \\\\\\\\ b \\\\\\\\ d")
    assert n == 0 and "twice as long" in err


def test_is_already_applied_and_hints():
    from curator.fuzzy_match import find_closest_lines, format_no_match_hint, is_already_applied
    assert is_already_applied("new content here", "old content here", "new content here")
    assert not is_already_applied("x", "old", "new")
    hint = find_closest_lines("    Step 1: Do the thing.", "# T\n\nStep 1: Do the thing.\n")
    assert "Whitespace difference detected" in hint and "file has: Step 1" in hint
    assert format_no_match_hint("Could not find a match", 0, "Step 1: Do the thing.", "Step 1: Do the thing.\n").startswith("\n\nDid you mean")
    assert format_no_match_hint("Found 2 matches", 0, "x", "x x") == ""
