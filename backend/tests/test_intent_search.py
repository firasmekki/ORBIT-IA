"""detect_search_intent: requests to FIND a term must be recognised
deterministically (never left to the vector search + LLM), and nothing
else may be mistaken for one."""

import pytest

from app.agent.intent import SearchIntent, detect_keyword_intent, detect_search_intent


@pytest.mark.parametrize(
    "message,term,scope",
    [
        # the exact requests from the bug report
        ("rechercher tarak au niveau de workspace", "tarak", "workspace"),
        ("rechercher tarek", "tarek", "all"),
        ("rechercher firas", "firas", "all"),
        # the reproduction list
        ("Tarek", "Tarek", "all"),
        ("Tarak", "Tarak", "all"),
        ("Firas", "Firas", "all"),
        ("recherche Tarek", "Tarek", "all"),
        ("trouve Tarek dans le workspace", "Tarek", "workspace"),
        ("où apparaît Tarek ?", "Tarek", "all"),
        ("donne-moi les documents qui mentionnent Tarek", "Tarek", "all"),
        # section 6 phrasings
        ("cherche Tarek", "Tarek", "all"),
        ("trouve Tarek", "Tarek", "all"),
        ("où est Tarek", "Tarek", "all"),
        ("qui est Tarek ?", "Tarek", "all"),
        ("mention de Tarek", "Tarek", "all"),
        ("documents contenant Tarek", "Tarek", "all"),
        ("recherche Tarek dans workspace", "Tarek", "workspace"),
        # names, identifiers, emails, quoted phrases
        ("Firas Mekki", "Firas Mekki", "all"),
        ("peux-tu chercher Firas Mekki dans mes documents ?", "Firas Mekki", "documents"),
        ("INV-2025-001", "INV-2025-001", "all"),
        ("contact.fictif@example.test", "contact.fictif@example.test", "all"),
        ("recherche « Tarek Ben Ali »", "Tarek Ben Ali", "all"),
        ("recherche Tarek Ben Ali dans les documents", "Tarek Ben Ali", "documents"),
        ("la politique de congés", "politique de congés", "all"),
    ],
)
def test_search_requests_are_detected_with_the_exact_term(message, term, scope):
    intent = detect_search_intent(message)
    assert intent is not None, message
    assert (intent.term, intent.scope) == (term, scope)


def test_tarak_is_never_corrected_to_tarek():
    assert detect_search_intent("recherche Tarak").term == "Tarak"


@pytest.mark.parametrize(
    "message",
    [
        "Bonjour",
        "merci !",
        "ok",
        "Quelle est la politique de congés ?",
        "liste les documents",
        "recherche les documents",
        "Résume le plan stratégique 2026.",
        "Donne-moi une courbe",
        "qui est le directeur ?",  # a question for the documents, not a name lookup
        "lis rapport.pdf",
        "Compare le budget IT et Marketing",
    ],
)
def test_other_messages_are_not_searches(message):
    assert detect_search_intent(message) is None


def test_bare_term_can_be_disabled_while_another_dialog_waits_for_an_answer():
    assert detect_search_intent("Infrastructure").term == "Infrastructure"
    assert detect_search_intent("Infrastructure", allow_bare=False) is None
    # an explicit search still wins
    assert detect_search_intent("recherche Infrastructure", allow_bare=False).term == "Infrastructure"


@pytest.mark.parametrize(
    "message",
    [
        "cherche combien de mot Mekki",
        "cherche combien de fois le mot Mekki",
        "cherche combien de fois Mekki apparaît",
        "recherche le nombre d'occurrences de Mekki",
        "compte le mot Mekki",
        "nombre d'occurrences de Mekki",
    ],
)
def test_counting_words_are_not_part_of_the_term(message):
    assert detect_search_intent(message) == SearchIntent(term="Mekki")


def test_count_question_with_the_verb_first():
    assert detect_keyword_intent("combien de fois apparaît le mot Mekki ?").keyword == "Mekki"
