"""Idempotent demo data seeder.

Run with `python -m app.seed` (see docker-compose.yml `seed` service). Safe
to re-run: existing users/documents are left untouched, and documents are
only (re)ingested into the vector index if they don't already have chunks.

Creates the 5 demo accounts from the brief - Director, HR, Accountant,
Developer, Employee - and a small but deliberately varied document/finance
corpus that exercises every dimension of the policy model: cross-department
denial, same-department-higher-confidentiality denial, and the
Director's full-access path.
"""

import sys

from app.core.database import SessionLocal, engine
from app.core.migrations import bootstrap_schema
from app.core.security import hash_password
from app.models.document import CONFIDENTIALITY_RANK, Document, DocumentChunk
from app.models.finance import FinancialRecord
from app.models.user import User
from app.rag.embeddings import EmbeddingServiceError
from app.rag.ingest import ingest_document

DEMO_PASSWORD = "Demo1234!"

USERS = [
    dict(username="directeur", email="directeur@orbitia.demo", full_name="Claire Dubois", role="DIRECTOR"),
    dict(username="rh", email="rh@orbitia.demo", full_name="Nadia Amrani", role="HR"),
    dict(username="comptable", email="comptable@orbitia.demo", full_name="Marc Lefevre", role="ACCOUNTANT"),
    dict(username="developpeur", email="dev@orbitia.demo", full_name="Yanis Kaci", role="DEVELOPER"),
    dict(username="employe", email="employe@orbitia.demo", full_name="Sophie Martin", role="EMPLOYEE"),
]

DOCUMENTS = [
    dict(
        title="Charte de l'entreprise",
        department="GENERAL",
        confidentiality="PUBLIC",
        content=(
            "Charte Orbitia\n\n"
            "Orbitia est une entreprise de conseil technologique fondée en 2016. Nos valeurs : "
            "intégrité, exigence technique, respect du client et de la confidentialité des données.\n\n"
            "Cette charte est publique et peut être partagée avec des partenaires externes."
        ),
    ),
    dict(
        title="Politique de congés",
        department="GENERAL",
        confidentiality="INTERNAL",
        content=(
            "Politique de congés payés - Orbitia\n\n"
            "Chaque employé à temps plein bénéficie de 25 jours de congés payés par an, plus 2 jours "
            "de RTT par trimestre. Les congés doivent être posés au moins 2 semaines à l'avance via le "
            "portail RH et validés par le manager direct.\n\n"
            "Le report de congés non pris est limité à 5 jours vers l'année suivante, sauf accord "
            "exceptionnel du service RH. Les congés maladie sont à déclarer sous 48h avec justificatif."
        ),
    ),
    dict(
        title="Guide d'onboarding",
        department="GENERAL",
        confidentiality="INTERNAL",
        content=(
            "Guide d'accueil des nouveaux employés\n\n"
            "Semaine 1 : remise du matériel, création des comptes, visite des locaux, présentation "
            "des équipes. Semaine 2 : formation sécurité et confidentialité des données, présentation "
            "des outils internes. Un parrain est assigné à chaque nouvel arrivant pour les 3 premiers mois."
        ),
    ),
    dict(
        title="Procédure de recrutement",
        department="HR",
        confidentiality="INTERNAL",
        content=(
            "Procédure de recrutement interne\n\n"
            "Toute ouverture de poste est validée par la direction avant publication. Le processus "
            "comprend : tri des CV par le service RH, entretien RH, entretien technique avec l'équipe, "
            "entretien final avec la direction pour les postes seniors. Délai cible : 4 semaines."
        ),
    ),
    dict(
        title="Rapport annuel RH - climat social",
        department="HR",
        confidentiality="CONFIDENTIAL",
        content=(
            "Rapport annuel RH 2025 - synthèse confidentielle\n\n"
            "Taux de turnover : 8,2% (stable par rapport à 2024). Taux de satisfaction interne (enquête "
            "anonyme) : 7,4/10. Trois départs sont attribués à des tensions managériales dans l'équipe "
            "commerciale, un plan d'accompagnement managérial a été lancé au T3."
        ),
    ),
    dict(
        title="Architecture technique - plateforme interne",
        department="TECH",
        confidentiality="INTERNAL",
        content=(
            "Architecture de la plateforme Orbitia\n\n"
            "La plateforme est composée d'un frontend React/TypeScript, d'un backend FastAPI, d'une base "
            "PostgreSQL avec l'extension pgvector pour la recherche vectorielle, d'un serveur MCP exposant "
            "les outils internes, et d'un modèle de langage auto-hébergé via Ollama. Tous les composants "
            "sensibles restent dans le réseau privé de l'entreprise."
        ),
    ),
    dict(
        title="Accès infrastructure de production",
        department="TECH",
        confidentiality="CONFIDENTIAL",
        content=(
            "Procédure d'accès à l'infrastructure de production\n\n"
            "L'accès aux serveurs de production nécessite une authentification à deux facteurs et une "
            "validation par le lead technique. Les accès sont audités mensuellement. Toute élévation de "
            "privilège temporaire doit être justifiée par un ticket d'incident et expire sous 24h."
        ),
    ),
    dict(
        title="Budget masse salariale 2025 (synthèse)",
        department="FINANCE",
        confidentiality="CONFIDENTIAL",
        content=(
            "Synthèse budgétaire - masse salariale 2025\n\n"
            "Le budget global de la masse salariale pour 2025 s'élève à 4,1 millions d'euros, en hausse "
            "de 6% par rapport à 2024, principalement porté par le recrutement de l'équipe technique. "
            "Ce document présente une synthèse agrégée par département, sans détail individuel."
        ),
    ),
    dict(
        title="Grille des salaires individuels 2025",
        department="FINANCE",
        confidentiality="SECRET",
        content=(
            "Grille des salaires individuels - 2025 - STRICTEMENT CONFIDENTIEL\n\n"
            "Ce document contient le détail des rémunérations individuelles par employé. Sa diffusion "
            "est restreinte à la direction générale uniquement, conformément à la politique de "
            "confidentialité des données RH."
        ),
    ),
    dict(
        title="Plan stratégique 2026",
        department="EXEC",
        confidentiality="SECRET",
        content=(
            "Plan stratégique 2026 - document de direction\n\n"
            "Objectifs : ouverture d'un bureau à Lyon, lancement d'une offre IA d'entreprise, croissance "
            "du chiffre d'affaires de 20%. Ce document est strictement réservé à la direction générale "
            "avant sa présentation officielle au comité exécutif."
        ),
    ),
]

FINANCIAL_RECORDS = [
    dict(label="Budget RH 2025", department="FINANCE", confidentiality="CONFIDENTIAL", amount=180000, year=2025),
    dict(label="Budget Marketing 2025", department="FINANCE", confidentiality="CONFIDENTIAL", amount=95000, year=2025),
    dict(label="Budget IT / Infrastructure 2025", department="FINANCE", confidentiality="CONFIDENTIAL", amount=310000, year=2025),
    dict(label="Masse salariale totale 2025", department="FINANCE", confidentiality="CONFIDENTIAL", amount=4100000, year=2025),
    dict(label="Salaire individuel - Direction Générale", department="FINANCE", confidentiality="SECRET", amount=145000, year=2025),
    dict(label="Résultat net 2025 (prévisionnel)", department="FINANCE", confidentiality="SECRET", amount=620000, year=2025),
]


def ensure_schema() -> None:
    with engine.begin() as conn:
        bootstrap_schema(conn)


def seed_users(db) -> None:
    for u in USERS:
        existing = db.query(User).filter(User.username == u["username"]).first()
        if existing:
            continue
        db.add(User(hashed_password=hash_password(DEMO_PASSWORD), **u))
    db.commit()


def seed_documents(db) -> list[Document]:
    created: list[Document] = []
    for d in DOCUMENTS:
        existing = db.query(Document).filter(Document.title == d["title"]).first()
        if existing:
            created.append(existing)
            continue
        doc = Document(
            title=d["title"],
            department=d["department"],
            confidentiality=d["confidentiality"],
            confidentiality_rank=CONFIDENTIALITY_RANK[d["confidentiality"]],
            content=d["content"],
        )
        db.add(doc)
        db.commit()
        db.refresh(doc)
        created.append(doc)
    return created


def seed_financial_records(db) -> None:
    for r in FINANCIAL_RECORDS:
        existing = db.query(FinancialRecord).filter(FinancialRecord.label == r["label"]).first()
        if existing:
            continue
        db.add(
            FinancialRecord(
                label=r["label"],
                department=r["department"],
                confidentiality=r["confidentiality"],
                confidentiality_rank=CONFIDENTIALITY_RANK[r["confidentiality"]],
                amount=r["amount"],
                year=r["year"],
            )
        )
    db.commit()


def ingest_documents(db, documents: list[Document]) -> None:
    for doc in documents:
        has_chunks = db.query(DocumentChunk).filter(DocumentChunk.document_id == doc.id).first() is not None
        if has_chunks:
            continue
        try:
            count = ingest_document(db, doc)
            print(f"  indexed '{doc.title}' -> {count} chunks")
        except EmbeddingServiceError as exc:
            print(f"  WARNING: could not index '{doc.title}' ({exc})", file=sys.stderr)


def main() -> None:
    print("Ensuring schema...")
    ensure_schema()

    db = SessionLocal()
    try:
        print("Seeding demo users...")
        seed_users(db)

        print("Seeding demo documents...")
        documents = seed_documents(db)

        print("Seeding demo financial records...")
        seed_financial_records(db)

        print("Ingesting documents into the vector index (requires Ollama)...")
        ingest_documents(db, documents)
    finally:
        db.close()

    print("\nDone. Demo accounts (password for all: " + DEMO_PASSWORD + "):")
    for u in USERS:
        print(f"  {u['username']:<14} -> {u['role']}")


if __name__ == "__main__":
    main()
