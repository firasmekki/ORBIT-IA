"""Multi-turn chart dialog through run_agent_turn, threading
reference_context between turns exactly like routers/chat.py does.
call_tool / chat_completion / complete_json are monkeypatched - the
search_database fake returns the demo's real financial records."""

from contextlib import asynccontextmanager

import app.agent.orchestrator as orchestrator
from app.agent.llm_client import LLMServiceError
from app.policy.rules import Grant

RECORDS = [
    {"label": "Budget IT / Infrastructure 2025", "amount": 310000.0, "year": 2025, "confidentiality": "CONFIDENTIAL"},
    {"label": "Budget Marketing 2025", "amount": 95000.0, "year": 2025, "confidentiality": "CONFIDENTIAL"},
    {"label": "Budget RH 2025", "amount": 180000.0, "year": 2025, "confidentiality": "CONFIDENTIAL"},
    {"label": "Résultat net 2025 (prévisionnel)", "amount": 620000.0, "year": 2025, "confidentiality": "SECRET"},
]


def _director(extra=()):
    return Grant(
        role="DIRECTOR",
        departments=frozenset({"FINANCE", "GENERAL"}),
        max_level="SECRET",
        max_level_rank=3,
        tools=frozenset({"generate_chart", "search_database", "search_documents", *extra}),
    )


class Conversation:
    """Plays turns like the router: the previous assistant turn's
    reference_context is handed to the next one."""

    def __init__(self, monkeypatch, *, records=RECORDS, llm_pick=None, extra_tools=None):
        self.calls: list[tuple[str, dict]] = []
        self.context = None
        self.grant = _director()

        @asynccontextmanager
        async def session(token):
            yield object()

        async def call_tool(_session, name, arguments):
            self.calls.append((name, arguments))
            if name == "search_database":
                return {"results": records}
            if name == "generate_chart":
                return {"chart": {**arguments}}
            if extra_tools and name in extra_tools:
                return extra_tools[name](arguments)
            raise AssertionError(f"unexpected tool {name}")

        async def no_llm(*a, **k):
            raise AssertionError("the chart dialog must never ask the LLM for an answer")

        async def complete_json(messages, schema, **_):
            if llm_pick is None:
                raise LLMServiceError("indisponible")
            return {"metric_ids": llm_pick}

        monkeypatch.setattr(orchestrator, "open_mcp_session", session)
        monkeypatch.setattr(orchestrator, "mint_internal_token", lambda **kwargs: "fake-token")
        monkeypatch.setattr(orchestrator, "call_tool", call_tool)
        monkeypatch.setattr(orchestrator, "chat_completion", no_llm)
        monkeypatch.setattr(orchestrator, "complete_json", complete_json)

    async def say(self, message, history=None):
        result = await orchestrator.run_agent_turn(
            user_id="u1", grant=self.grant, message=message, history=history or [], last_reference_context=self.context
        )
        self.context = result.reference_context
        return result


# TEST 1 + spec section 3 / 6: "Donne-moi une courbe" -> questions -> chart, with memory
async def test_incomplete_request_becomes_a_dialog_that_ends_in_a_chart(monkeypatch):
    chat = Conversation(monkeypatch)

    first = await chat.say("Donne-moi une courbe")
    assert first.chart is None
    # Nothing in the conversation: a short question, never the generic
    # list of every budget in the database.
    assert first.answer == "Quelle donnée souhaitez-vous représenter, et sur quelle période ?"
    assert first.clarification["status"] == "need_more_information"
    assert first.clarification["options"] == []

    second = await chat.say("Le budget")
    assert second.chart is None
    assert second.answer.startswith("Quel budget souhaitez-vous analyser")
    assert [o["value"] for o in second.clarification["options"]] == [
        "Budget IT / Infrastructure", "Budget Marketing", "Budget RH",
    ]

    third = await chat.say("Infrastructure")
    assert third.clarification is None
    # A single period: no curve, no one-bar chart - a value card, and why.
    assert third.chart["chart_type"] == "kpi"
    assert [(i["label"], i["period"], i["value"]) for i in third.chart["data"]] == [
        ("Budget IT / Infrastructure", "2025", 310000.0)
    ]
    assert third.answer.startswith("Je dispose uniquement de la valeur de « Budget IT / Infrastructure » en 2025 (310 000 €)")
    assert "au moins une autre période comparable" in third.answer
    assert not any(name == "generate_chart" for name, _ in chat.calls)
    assert third.reference_context is None  # dialog over


async def test_vague_request_offers_only_what_the_conversation_is_about(monkeypatch):
    chat = Conversation(monkeypatch)
    history = [
        {"role": "user", "content": "Quel est le budget marketing 2025 ?"},
        {"role": "assistant", "content": "Le Budget Marketing 2025 est de 95 000 €."},
    ]
    first = await chat.say("Fais-moi un graphique", history=history)
    assert [o["label"] for o in first.clarification["options"]] == ["Budget Marketing", "Autre"]
    assert "Budget RH" not in first.answer and "Résultat net" not in first.answer

    result = await chat.say(first.clarification["options"][0]["value"])
    assert result.chart["chart_type"] == "kpi"
    assert result.chart["data"][0]["id"] == "budget_marketing"


async def test_curve_request_in_context_explains_a_single_period(monkeypatch):
    chat = Conversation(monkeypatch)
    history = [{"role": "assistant", "content": "Le Budget IT / Infrastructure 2025 est de 310 000 €."}]
    ask = await chat.say("Tu peux donner des courbes ?", history=history)
    assert ask.chart is None
    assert "Je ne dispose que d'une seule période pour « Budget IT / Infrastructure » (2025)" in ask.answer
    assert "au moins une autre période comparable" in ask.answer


# TEST 2
async def test_complete_request_with_a_single_period_is_a_value_card(monkeypatch):
    chat = Conversation(monkeypatch)
    result = await chat.say("Donne-moi une courbe de variation du budget Infrastructure 2025")
    assert result.clarification is None
    assert result.chart["chart_type"] == "kpi"
    assert result.chart["data"][0]["value"] == 310000.0
    contract = result.chart["contract"]
    assert contract["recommended_type"] == "kpi" and contract["unit"] == "€" and contract["periods"] == ["2025"]
    assert contract["sources"] and contract["rationale"]


# TEST 3
async def test_comparison_of_two_budgets(monkeypatch):
    chat = Conversation(monkeypatch)
    result = await chat.say("Compare le budget IT et Marketing")
    assert result.chart["chart_type"] == "bar"
    assert {(r["label"], r["amount"]) for r in result.chart["data"]} == {
        ("Budget IT / Infrastructure", 310000.0),
        ("Budget Marketing", 95000.0),
    }
    assert result.chart["x_axis"]["label"] and result.chart["y_axis"]["label"]


# TEST 5: unknown data -> asked for, then built from the user's own values
async def test_missing_data_is_requested_then_used(monkeypatch):
    chat = Conversation(monkeypatch)

    ask = await chat.say("Fais-moi la courbe du chiffre d'affaires")
    assert ask.chart is None
    assert "je n'ai pas encore les données de « chiffre d'affaires »" in ask.answer
    assert "Aucune donnée autorisée" not in ask.answer

    chart = await chat.say("2024: 120000, 2025: 135000")
    assert chart.chart is not None
    assert [(r["label"], r["amount"]) for r in chart.chart["data"]] == [("2024", 120000.0), ("2025", 135000.0)]
    assert chart.chart["chart_type"] == "line"
    assert chart.chart["title"] == "Chiffre d'affaires"


# The LLM may SUGGEST a catalog metric for loose wording - the user confirms
async def test_llm_suggestion_is_offered_first_and_confirmed_by_the_user(monkeypatch):
    chat = Conversation(monkeypatch, llm_pick=["budget_it_infrastructure"])

    ask = await chat.say("Fais un graphique des dépenses informatiques")
    assert ask.chart is None  # never plotted on the model's word alone
    assert "Vouliez-vous dire « Budget IT / Infrastructure »" in ask.answer
    assert ask.clarification["options"][0]["value"] == "Budget IT / Infrastructure"

    confirmed = await chat.say(ask.clarification["options"][0]["value"])
    assert confirmed.chart["data"][0]["id"] == "budget_it_infrastructure"


async def test_wrong_llm_guess_never_becomes_a_chart(monkeypatch):
    # Real behaviour seen with Mistral: "chiffre d'affaires" -> résultat net.
    chat = Conversation(monkeypatch, llm_pick=["resultat_net_previsionnel"])
    ask = await chat.say("Fais-moi la courbe du chiffre d'affaires")
    assert ask.chart is None
    assert "Je n'ai pas de données nommées « chiffre d'affaires »" in ask.answer

    values = await chat.say("2024: 120000, 2025: 135000")  # the dialog is still open
    assert [(r["label"], r["amount"]) for r in values.chart["data"]] == [("2024", 120000.0), ("2025", 135000.0)]


async def test_llm_cannot_introduce_a_metric_that_does_not_exist(monkeypatch):
    chat = Conversation(monkeypatch, llm_pick=["chiffre_affaires_2026"])
    result = await chat.say("Fais un graphique des dépenses informatiques")
    assert result.chart is None
    assert result.clarification is not None


async def test_llm_down_still_asks_instead_of_failing(monkeypatch):
    chat = Conversation(monkeypatch, llm_pick=None)
    result = await chat.say("Fais un graphique des dépenses informatiques")
    assert result.chart is None
    assert result.clarification["status"] == "need_more_information"


# A new complete request is not mistaken for an answer
async def test_new_full_request_starts_over(monkeypatch):
    chat = Conversation(monkeypatch)
    await chat.say("Donne-moi une courbe")
    result = await chat.say("Compare le budget IT et Marketing")
    assert {r["id"] for r in result.chart["data"]} == {"budget_it_infrastructure", "budget_marketing"}


async def test_unrelated_question_leaves_the_dialog(monkeypatch):
    chat = Conversation(monkeypatch)
    await chat.say("Donne-moi une courbe")

    async def llm_answer(messages, tools=None):
        return {"content": "Les congés sont de 25 jours.", "tool_calls": None}

    monkeypatch.setattr(orchestrator, "chat_completion", llm_answer)
    original = orchestrator.call_tool

    async def with_search(session, name, arguments):
        if name == "search_documents":
            return {"results": []}
        return await original(session, name, arguments)

    monkeypatch.setattr(orchestrator, "call_tool", with_search)
    result = await chat.say("Quelle est la politique de congés pour les nouveaux arrivants dans l'entreprise ?")
    assert result.chart is None and result.clarification is None


# No accessible financial data: Excel files offered as sources, then sheets
async def test_without_records_excel_files_and_sheets_are_offered(monkeypatch):
    sheets_error = {
        "error": "plusieurs feuilles", "code": "AMBIGUOUS", "status": "ambiguous",
        "error_kind": "ambiguous_sheet", "sheets": ["CA mensuel", "Comparaison annuelle"],
    }
    chat = Conversation(
        monkeypatch,
        records=[],
        extra_tools={
            "list_documents": lambda args: {"results": [{"title": "Orbitia_Test_Entreprise_Complet"}]},
            "read_spreadsheet_data": lambda args: sheets_error,
        },
    )
    chat.grant = _director(extra=("list_documents", "read_spreadsheet_data"))

    first = await chat.say("Donne-moi une courbe")
    file_option = next(o for o in first.clarification["options"] if "Orbitia_Test_Entreprise_Complet" in o["label"])
    assert file_option["value"] == "À partir du fichier Orbitia_Test_Entreprise_Complet.xlsx"

    second = await chat.say(file_option["value"])
    assert second.answer == "Quelle feuille de « Orbitia_Test_Entreprise_Complet.xlsx » souhaitez-vous utiliser ?"
    assert [o["value"] for o in second.clarification["options"]] == ["feuille CA mensuel", "feuille Comparaison annuelle"]
    # Not a security event: a question.
    assert not any(t["decision"] == "DENY" for t in second.tool_trace)


async def test_technical_failure_has_a_user_friendly_message(monkeypatch):
    chat = Conversation(monkeypatch)

    async def down(session, name, arguments):
        return {"error": "connection refused", "code": "SERVICE_UNAVAILABLE", "service_unavailable": True}

    monkeypatch.setattr(orchestrator, "call_tool", down)
    result = await chat.say("Fais un graphique du budget RH")
    assert result.answer == orchestrator.CHART_TECHNICAL_ERROR
    assert "refused" not in result.answer


# "Analyse la variation" is computed from the authorized records - the LLM
# (chat_completion raises in this fake) is never asked to estimate it.
async def test_variation_question_is_computed_never_generated(monkeypatch):
    chat = Conversation(monkeypatch)
    result = await chat.say("Analyse la variation du Budget IT / Infrastructure")
    assert result.chart is None
    assert "est de 310 000 € pour 2025" in result.answer
    assert "La variation annuelle ne peut donc pas être calculée" in result.answer
    assert [name for name, _ in chat.calls] == ["search_database"]


async def test_variation_between_two_years_is_calculated(monkeypatch):
    records = RECORDS + [{"label": "Budget IT / Infrastructure 2024", "amount": 240000.0, "year": 2024, "confidentiality": "CONFIDENTIAL"}]
    chat = Conversation(monkeypatch, records=records)
    result = await chat.say("Quelle est la variation du budget IT entre 2024 et 2025 ?")
    assert "augmente de +70 000 € (+29,2 %) entre 2024 et 2025 : 240 000 € → 310 000 €" in result.answer
