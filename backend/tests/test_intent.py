from app.agent.intent import detect_keyword_intent


def test_quoted_keyword_wins_regardless_of_trigger_language():
    result = detect_keyword_intent('cherche le mot "Firas Mekki"')
    assert result is not None
    assert result.keyword == "Firas Mekki"


def test_cherche_le_mot_unquoted():
    result = detect_keyword_intent("cherche le mot budget")
    assert result is not None
    assert result.keyword == "budget"


def test_recherche_le_terme():
    result = detect_keyword_intent("recherche le terme confidentiel")
    assert result.keyword == "confidentiel"


def test_trouve_le_nom():
    result = detect_keyword_intent("trouve le nom Firas")
    assert result.keyword == "Firas"


def test_combien_de_fois_quoted():
    result = detect_keyword_intent('combien de fois "firas" apparaît dans les documents ?')
    assert result.keyword == "firas"


def test_combien_de_fois_unquoted():
    result = detect_keyword_intent("combien de fois firas apparaît dans les rapports")
    assert result.keyword == "firas"


def test_ou_apparait():
    result = detect_keyword_intent("où apparaît firas dans les documents")
    assert result.keyword == "firas"


def test_derja_qadeh_mawjouda_quoted():
    result = detect_keyword_intent('9adeh marra "firas" mawjouda fel dossier')
    assert result.keyword == "firas"


def test_derja_word_before_mawjouda():
    result = detect_keyword_intent("qadech marra firas mawjouda")
    assert result is not None
    assert result.keyword == "firas"


def test_derja_win_mawjouda():
    result = detect_keyword_intent('win mawjouda "firas"')
    assert result.keyword == "firas"


def test_arabic_kam_marra_quoted():
    result = detect_keyword_intent('كم مرة تظهر كلمة "فراس" في المستندات')
    assert result.keyword == "فراس"


def test_no_trigger_returns_none():
    assert detect_keyword_intent("quelle est la politique de congés ?") is None
    assert detect_keyword_intent("bonjour, comment ça va ?") is None


def test_trigger_but_empty_keyword_returns_none():
    assert detect_keyword_intent('cherche le mot ""') is None


def test_natural_phrasing_reported_bug():
    # Real user phrasing that used to fall through to the LLM entirely:
    # "chercher" (infinitive, not "cherche"), lots of words between the
    # verb and "mot", and a two-word unquoted name.
    result = detect_keyword_intent(
        "chercher ds les documents il ya un documnts contient le mot firas mekki "
        "dis moi lla paragraphe avec la page et le nom du documnts"
    )
    assert result is not None
    assert result.keyword == "firas mekki"


def test_contient_le_mot_trigger():
    result = detect_keyword_intent("il y a un document qui contient le mot budget dans le rapport")
    assert result is not None
    assert result.keyword == "budget"


def test_chercher_infinitive_form():
    result = detect_keyword_intent("peux-tu chercher le mot congés")
    assert result is not None
    assert result.keyword == "congés"


def test_two_word_name_unquoted_still_trimmed_at_trailing_clause():
    result = detect_keyword_intent("cherche le mot firas mekki dans les documents")
    assert result.keyword == "firas mekki"
