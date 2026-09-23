from app.agent.intent import detect_section_request


def test_page_with_document_name():
    r = detect_section_request("ouvre la page 2 de Charte de l'entreprise")
    assert r.page == 2
    assert r.document_name == "Charte de l'entreprise"
    assert r.section is None
    assert not r.use_context_document


def test_section_name_alone():
    r = detect_section_request("montre la section 4.2")
    assert r.section == "4.2"
    assert r.page is None
    assert r.document_name is None


def test_section_name_with_document():
    r = detect_section_request("montre la section 4.2 de Procédure Qualité")
    assert r.section == "4.2"
    assert r.document_name == "Procédure Qualité"


def test_block_reference():
    r = detect_section_request("affiche le bloc 3")
    assert r.section == "Bloc 3"
    assert r.page is None


def test_page_of_context_document():
    r = detect_section_request("montre la page 2 de ce document")
    assert r.page == 2
    assert r.use_context_document is True
    assert r.document_name is None


def test_first_occurrence():
    r = detect_section_request("ouvre la première occurrence")
    assert r.first_occurrence is True


def test_nth_choice_deuxieme():
    r = detect_section_request("le deuxième")
    assert r.nth_choice == 2


def test_nth_choice_la_deuxieme():
    r = detect_section_request("la deuxième")
    assert r.nth_choice == 2


def test_nth_choice_not_confused_with_explicit_page():
    # "la page 2" must win over any accidental ordinal-like match
    r = detect_section_request("ouvre la page 2")
    assert r.page == 2
    assert r.nth_choice is None


def test_continuation_suite():
    r = detect_section_request("suite")
    assert r.continuation is True


def test_continuation_continue():
    r = detect_section_request("continue")
    assert r.continuation is True


def test_no_trigger_returns_none():
    assert detect_section_request("quelle est la politique de congés ?") is None
    assert detect_section_request("bonjour") is None


def test_page_without_document_reference():
    r = detect_section_request("ouvre la page 2")
    assert r.page == 2
    assert r.document_name is None
    assert not r.use_context_document
