import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class FinancialRecord(Base):
    """A small stand-in for a real internal financial system.

    Reachable only through the `search_database` MCP tool, which is itself
    only offered to DIRECTOR/ACCOUNTANT sessions (see app/policy/rules.py) -
    plus a row-level confidentiality filter on top, for defense in depth.
    """

    __tablename__ = "financial_records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    department: Mapped[str] = mapped_column(String(32), nullable=False, default="FINANCE")
    confidentiality: Mapped[str] = mapped_column(String(32), nullable=False)
    confidentiality_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
