"""Which chat messages are about the user's own mailbox, and what they ask
(app/agent/email_intent.py) - deterministic, no model involved."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.agent.email_intent import is_email_request, parse_email_request

NOW = datetime(2026, 10, 28, 9, 30, tzinfo=UTC)  # a Wednesday
TZ = ZoneInfo("Africa/Tunis")


@pytest.mark.parametrize("message", [
    "Donne-moi le dernier email reçu.",
    "Résume les cinq derniers emails.",
    "Quels sont mes emails non lus cette semaine ?",
    "Recherche les messages provenant de ce fournisseur.",
    "Quels emails nécessitent une action de ma part ?",
    "Prépare un résumé des emails reçus aujourd'hui avec les échéances et les actions à effectuer.",
    "Donne-moi le dernier email reçu sur mon compte Gmail.",
    "Résume les cinq derniers emails de mon Outlook.",
    "Affiche mes emails non lus de Yahoo cette semaine.",
    "Résume les emails de Sonelgaz",
    "Affiche les emails reçus de mon fournisseur Sonelgaz",
    "show my latest emails",
    "Envoie un email à Paul",  # recognized, then refused (read-only)
])
def test_mailbox_requests_are_recognized(message):
    assert is_email_request(message), message


@pytest.mark.parametrize("message", [
    "Bonjour",
    "Quelle est la politique de congés ?",
    "cherche l'adresse email de Tarek dans les documents",
    "Rédige un email de relance pour un client",
    "Combien de fois Tarek apparaît dans le contrat ?",
    "Résume le document rapport.pdf",
])
def test_other_requests_are_not_taken_for_mailbox_questions(message):
    assert not is_email_request(message), message


def test_counts_periods_and_filters():
    r = parse_email_request("Résume les cinq derniers emails non lus de cette semaine", now=NOW, tz=TZ)
    assert r.limit == 5 and not r.limit_is_default and r.unread_only and r.summarize
    assert r.period_label == "cette semaine" and r.received_after.isoformat() == "2026-10-26T00:00:00+01:00"
    today = parse_email_request("les emails reçus aujourd'hui", now=NOW, tz=TZ)
    assert today.received_after.isoformat() == "2026-10-28T00:00:00+01:00" and today.limit is None
    yesterday = parse_email_request("mes emails d'hier", now=NOW, tz=TZ)
    assert (yesterday.received_before - yesterday.received_after).days == 1
    assert parse_email_request("le dernier email reçu", now=NOW, tz=TZ).limit == 1


def test_senders_and_ambiguous_references():
    by_address = parse_email_request("les emails envoyés par Factures@Exemple.tn", now=NOW, tz=TZ)
    assert by_address.from_address == "factures@exemple.tn"
    by_name = parse_email_request("Affiche les emails reçus de Sonelgaz cette semaine", now=NOW, tz=TZ)
    assert by_name.from_name == "sonelgaz" and by_name.period_label == "cette semaine"
    assert parse_email_request("les messages provenant de ce fournisseur", now=NOW, tz=TZ).ambiguous_sender == "fournisseur"
    assert parse_email_request("le dernier email de mon Outlook", now=NOW, tz=TZ).from_name is None


def test_action_filter_versus_actions_in_a_summary():
    filter_request = parse_email_request("Quels emails demandent une action de ma part ?", now=NOW, tz=TZ)
    assert filter_request.needs_action and filter_request.summarize
    report = parse_email_request("Prépare un résumé des emails reçus aujourd'hui avec les échéances et les actions à effectuer",
                                 now=NOW, tz=TZ)
    assert report.summarize and report.deadlines and not report.needs_action


def test_write_verbs_are_flagged_but_not_read_participles():
    assert parse_email_request("Supprime mes emails non lus", now=NOW, tz=TZ).write_request
    assert parse_email_request("marque comme lus mes emails", now=NOW, tz=TZ).write_request
    assert not parse_email_request("les emails envoyés par Paul", now=NOW, tz=TZ).write_request
    assert not parse_email_request("mes emails de la marque Renault", now=NOW, tz=TZ).write_request
