"""Security alerts vs. functional errors, through the real chat persistence
path (routers/chat.py::_persist_assistant_turn) against a real database:
only a confirmed ACCESS_DENIED raises a Director-facing alert, a missing
file / validation error / outage never does, and the same refusal repeated
(a resent message, a retry) raises a single alert - while every attempt
stays in the audit log."""

from app.agent.orchestrator import AgentTurnResult, _record_tool_call
from app.models.alert import Alert
from app.models.audit import AuditLog
from app.models.chat import Conversation
from app.policy.engine import (
    ACCESS_DENIED,
    NOT_FOUND,
    SERVICE_UNAVAILABLE,
    VALIDATION_ERROR,
    WORKSPACE_FILE_NOT_FOUND,
    tool_error,
)
from app.routers.chat import _persist_assistant_turn

QUESTION = (
    "À partir du fichier Orbitia_Test_Entreprise_Complet.xlsx, feuille CA mensuel, génère un graphique en courbe "
    "du chiffre d'affaires de janvier à décembre."
)


def _conversation(db, user):
    conv = Conversation(user_id=user.id, title="test")
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def _turn(tool: str, result: dict) -> AgentTurnResult:
    trace: list[dict] = []
    _record_tool_call(trace, tool, {"document_name": "x"}, result)
    return AgentTurnResult(answer="réponse", tool_trace=trace)


def _chat_alerts(db):
    db.expire_all()
    return db.query(Alert).filter(Alert.alert_type == "CHAT_ACCESS_DENIED").all()


# TEST 2 / CAS A - file absent: functional error, no alert
def test_file_not_found_never_raises_an_alert(db, make_test_user):
    user = make_test_user(username="al_claire", role="DIRECTOR")
    conv = _conversation(db, user)
    result = _turn("read_spreadsheet_data", tool_error(NOT_FOUND, "document introuvable", concealed=True))

    _persist_assistant_turn(db, conv=conv, user=user, original_message=QUESTION, result=result)

    assert _chat_alerts(db) == []
    assert result.tool_trace[0]["decision"] == "ERROR"


def test_workspace_file_not_found_never_raises_an_alert(db, make_test_user):
    user = make_test_user(username="al_ws", role="EMPLOYEE")
    conv = _conversation(db, user)
    result = _turn("read_local_file", tool_error(WORKSPACE_FILE_NOT_FOUND, "fichier absent du Workspace"))

    _persist_assistant_turn(db, conv=conv, user=user, original_message="lis fichier_inexistant.xlsx", result=result)

    assert _chat_alerts(db) == []


# TEST 3 / CAS B - real refusal: alert, no content
def test_confirmed_access_denied_raises_one_alert(db, make_test_user):
    user = make_test_user(username="al_nadia", role="HR")
    conv = _conversation(db, user)
    result = _turn("search_documents", tool_error(ACCESS_DENIED, "Accès refusé : une ressource pertinente existe"))

    _persist_assistant_turn(db, conv=conv, user=user, original_message="salaire directeur en 2023", result=result)

    alerts = _chat_alerts(db)
    assert len(alerts) == 1
    assert "salaire directeur en 2023" in alerts[0].description


def test_concealed_refusal_still_alerts_on_its_code(db, make_test_user):
    # Shown to the user exactly like "not found" (decision ERROR, neutral
    # wording) - but it IS a refusal, and the code says so.
    user = make_test_user(username="al_conceal", role="EMPLOYEE")
    conv = _conversation(db, user)
    result = _turn("get_document", tool_error(ACCESS_DENIED, "document introuvable ou accès non autorisé", concealed=True))

    _persist_assistant_turn(db, conv=conv, user=user, original_message="ouvre le plan stratégique", result=result)

    assert result.tool_trace[0]["decision"] == "ERROR"
    assert len(_chat_alerts(db)) == 1


def test_refusal_already_alerted_by_the_tool_is_not_alerted_twice(db, make_test_user):
    user = make_test_user(username="al_twice", role="EMPLOYEE")
    conv = _conversation(db, user)
    result = _turn(
        "get_document_section",
        tool_error(ACCESS_DENIED, "document introuvable ou accès non autorisé", concealed=True, alert_raised=True),
    )

    _persist_assistant_turn(db, conv=conv, user=user, original_message="section 2 du plan", result=result)

    assert _chat_alerts(db) == []


# TEST 4 - the same refusal 4 times: 4 audit rows, 1 alert
def test_retried_refusal_raises_a_single_alert_but_is_audited_every_time(db, make_test_user):
    user = make_test_user(username="al_retry", role="HR")
    conv = _conversation(db, user)

    for _ in range(4):
        result = _turn("search_documents", tool_error(ACCESS_DENIED, "Accès refusé : ressource hors périmètre"))
        _persist_assistant_turn(db, conv=conv, user=user, original_message="salaire directeur en 2023", result=result)

    assert len(_chat_alerts(db)) == 1
    audited = db.query(AuditLog).filter(AuditLog.action == "CHAT_MESSAGE", AuditLog.user_id == user.id).count()
    assert audited == 4


def test_a_different_question_is_a_different_event(db, make_test_user):
    user = make_test_user(username="al_two", role="HR")
    conv = _conversation(db, user)
    for question in ("salaire directeur en 2023", "grille des salaires"):
        result = _turn("search_documents", tool_error(ACCESS_DENIED, "Accès refusé"))
        _persist_assistant_turn(db, conv=conv, user=user, original_message=question, result=result)

    assert len(_chat_alerts(db)) == 2


# TEST 5 - authorized document: no alert
def test_authorized_read_raises_no_alert(db, make_test_user):
    user = make_test_user(username="al_ok", role="DIRECTOR")
    conv = _conversation(db, user)
    result = _turn("read_spreadsheet_data", {"title": "CA", "sheet": "CA mensuel", "columns": [], "rows": []})

    _persist_assistant_turn(db, conv=conv, user=user, original_message=QUESTION, result=result)

    assert result.tool_trace[0]["decision"] == "ALLOW"
    assert _chat_alerts(db) == []


# TEST 6 - technical / validation errors: no alert, logged separately
def test_technical_and_validation_errors_are_logged_not_alerted(db, make_test_user):
    user = make_test_user(username="al_tech", role="DIRECTOR")
    conv = _conversation(db, user)
    trace: list[dict] = []
    _record_tool_call(trace, "search_documents", {}, tool_error(SERVICE_UNAVAILABLE, "timeout", service_unavailable=True))
    _record_tool_call(trace, "generate_chart", {}, tool_error(VALIDATION_ERROR, "données invalides"))
    result = AgentTurnResult(answer="erreur", tool_trace=trace, degraded=True)

    _persist_assistant_turn(db, conv=conv, user=user, original_message="graphique", result=result)

    assert _chat_alerts(db) == []
    db.expire_all()
    errors = db.query(AuditLog).filter(AuditLog.action == "CHAT_TOOL_ERROR", AuditLog.user_id == user.id).all()
    assert len(errors) == 1
    assert "SERVICE_UNAVAILABLE" in errors[0].reason and "VALIDATION_ERROR" in errors[0].reason


def test_error_without_a_code_fails_closed_as_a_refusal(db, make_test_user):
    # An unclassified error must never silently escape alerting.
    user = make_test_user(username="al_legacy", role="EMPLOYEE")
    conv = _conversation(db, user)
    result = _turn("search_database", {"error": "rôle non autorisé"})

    _persist_assistant_turn(db, conv=conv, user=user, original_message="budget", result=result)

    assert len(_chat_alerts(db)) == 1
