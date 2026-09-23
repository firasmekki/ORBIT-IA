"""Test fixtures.

Runs against a real Postgres (orbitia_test database, not the demo
`orbitia` one) rather than mocking the ORM - the whole point of the
ACL/silent-leak tests is that filtering happens inside real SQL, so a mock
session would test nothing meaningful. Set TEST_DATABASE_URL to point
elsewhere (e.g. in CI); defaults to the same Postgres container the app
uses in docker-compose, just a different database name.

DocumentPage rows are inserted directly rather than through
app.rag.ingest.ingest_document, since that also embeds DocumentChunk rows
via Ollama - unnecessary and slow for tests that only exercise
search_keyword/get_document_section, which never touch embeddings at all.
"""

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://orbitia:orbitia@localhost:5432/orbitia_test"),
)

from app.core.migrations import bootstrap_schema  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.models.document import CONFIDENTIALITY_RANK, Document, DocumentPage  # noqa: E402
from app.models.user import User  # noqa: E402

TEST_DATABASE_URL = os.environ["DATABASE_URL"]


@pytest.fixture(scope="session")
def engine():
    eng = create_engine(TEST_DATABASE_URL, future=True)
    with eng.begin() as conn:
        bootstrap_schema(conn)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    Session = sessionmaker(bind=engine, future=True)
    session = Session()
    yield session
    session.rollback()
    session.execute(
        text(
            "TRUNCATE documents, document_pages, document_chunks, users, "
            "audit_logs, alerts RESTART IDENTITY CASCADE"
        )
    )
    session.commit()
    session.close()


def make_document(db, *, title, department, confidentiality, pages, owner_id=None):
    """`pages`: list of (page_no, section, line_offset, text) tuples."""
    content = "\n\n".join(p[3] for p in pages)
    doc = Document(
        title=title,
        department=department,
        confidentiality=confidentiality,
        confidentiality_rank=CONFIDENTIALITY_RANK[confidentiality],
        content=content,
        owner_id=owner_id,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    for i, (page_no, section, line_offset, text_) in enumerate(pages):
        db.add(
            DocumentPage(
                document_id=doc.id,
                page_no=page_no,
                section=section,
                order_index=i,
                line_offset=line_offset,
                text=text_,
            )
        )
    db.commit()
    return doc


def make_user(db, *, username, role, extra_departments=None, confidentiality_override=None, extra_tools=None):
    user = User(
        username=username,
        email=f"{username}@orbitia.test",
        full_name=username,
        hashed_password=hash_password("Test1234!"),
        role=role,
        extra_departments=extra_departments or [],
        confidentiality_override=confidentiality_override,
        extra_tools=extra_tools or [],
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def make_doc(db):
    return lambda **kwargs: make_document(db, **kwargs)


@pytest.fixture
def make_test_user(db):
    return lambda **kwargs: make_user(db, **kwargs)
