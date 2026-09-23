from app.agent.intent import detect_list_documents_intent


def test_no_trigger_returns_none():
    assert detect_list_documents_intent("quelle est la politique de congés ?") is None
    assert detect_list_documents_intent("cherche le mot budget") is None


def test_bare_list_trigger():
    result = detect_list_documents_intent("liste les documents")
    assert result is not None
    assert result.department is None
    assert result.author is None


def test_department_synonym_hr():
    result = detect_list_documents_intent("montre-moi les documents RH")
    assert result.department == "HR"


def test_department_synonym_finance_accented():
    result = detect_list_documents_intent("affiche les documents financiers")
    assert result.department == "FINANCE"


def test_department_synonym_general_no_accent_needed():
    result = detect_list_documents_intent("quels sont les documents généraux")
    assert result.department == "GENERAL"


def test_author_extraction():
    result = detect_list_documents_intent("liste les documents de Sophie Martin")
    assert result.author == "Sophie Martin"


def test_date_min_iso():
    result = detect_list_documents_intent("liste les documents depuis 2026-01-01")
    assert result.date_min == "2026-01-01"


def test_date_min_french_format_converted_to_iso():
    result = detect_list_documents_intent("liste les documents depuis le 01/03/2026")
    assert result.date_min == "2026-03-01"


def test_date_max():
    result = detect_list_documents_intent("liste les documents avant 31/12/2025")
    assert result.date_max == "2025-12-31"


def test_combined_department_and_date():
    result = detect_list_documents_intent("liste les documents finance depuis le 01/01/2026")
    assert result.department == "FINANCE"
    assert result.date_min == "2026-01-01"
