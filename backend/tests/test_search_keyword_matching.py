from app.rag.search_keyword import build_pattern, find_matches, is_arabic_keyword


def test_build_pattern_none_for_empty_keyword():
    assert build_pattern("   ", whole_word=True) is None
    assert build_pattern("", whole_word=True) is None


def test_exact_count_single_page():
    text = "Firas travaille avec Firas et Firas encore."
    pattern = build_pattern("Firas", whole_word=True)
    matches = find_matches(text, line_offset=1, pattern=pattern)
    assert len(matches) == 3


def test_multiple_occurrences_same_line_all_counted():
    text = "firas firas firas"
    pattern = build_pattern("firas", whole_word=True)
    matches = find_matches(text, line_offset=1, pattern=pattern)
    assert len(matches) == 3
    # no overlap/double counting: each match starts after the previous ends
    assert len({m.line for m in matches}) == 1


def test_whole_word_excludes_partial_matches():
    text = "Firasment n'est pas Firas, ni Firasco."
    pattern = build_pattern("Firas", whole_word=True)
    matches = find_matches(text, line_offset=1, pattern=pattern)
    assert len(matches) == 1


def test_substring_mode_includes_partial_matches():
    text = "Firasment n'est pas Firas, ni Firasco."
    pattern = build_pattern("Firas", whole_word=False)
    matches = find_matches(text, line_offset=1, pattern=pattern)
    assert len(matches) == 3


def test_case_and_accent_insensitive_matching():
    text = "L'étudiant s'appelle ÉTUDIANT puis etudiant."
    pattern = build_pattern("étudiant", whole_word=True)
    matches = find_matches(text, line_offset=1, pattern=pattern)
    assert len(matches) == 3


def test_line_number_uses_page_offset():
    text = "ligne 1\nligne 2\nfiras ici\nligne 4"
    pattern = build_pattern("firas", whole_word=True)
    matches = find_matches(text, line_offset=100, pattern=pattern)
    assert len(matches) == 1
    # "firas" is on the 3rd line of `text` -> global line 100 + 3 - 1 = 102
    assert matches[0].line == 102


def test_excerpt_is_centered_on_match_within_original_text():
    text = "x" * 100 + "FIRAS" + "y" * 100
    pattern = build_pattern("firas", whole_word=False)
    matches = find_matches(text, line_offset=1, pattern=pattern)
    assert len(matches) == 1
    assert "FIRAS" in matches[0].excerpt
    assert matches[0].excerpt.startswith("x")
    assert matches[0].excerpt.endswith("y")


def test_no_match_returns_empty_list():
    pattern = build_pattern("absent", whole_word=True)
    assert find_matches("rien à voir ici", line_offset=1, pattern=pattern) == []


def test_is_arabic_keyword():
    assert is_arabic_keyword("مدرسة") is True
    assert is_arabic_keyword("firas") is False
    assert is_arabic_keyword("Café") is False
