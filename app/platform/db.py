from functools import lru_cache

from sqlalchemy import JSON, Float, ForeignKey, Integer, String, UniqueConstraint, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.core.config import settings


class Base(DeclarativeBase):
    pass


class CompanyRow(Base):
    __tablename__ = "platform_companies"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    complex_id: Mapped[str] = mapped_column(String(100), index=True)
    owner_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    longitude: Mapped[float] = mapped_column(Float)
    latitude: Mapped[float] = mapped_column(Float)
    payload: Mapped[dict] = mapped_column(JSON)


class SessionRow(Base):
    __tablename__ = "platform_sessions"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(100), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[dict] = mapped_column(JSON)


class SourceRow(Base):
    __tablename__ = "platform_sources"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    kind: Mapped[str] = mapped_column(String(30), index=True)
    payload: Mapped[dict] = mapped_column(JSON)


class OrganizationRow(Base):
    __tablename__ = "platform_organizations"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(200), unique=True)
    biz_no: Mapped[str] = mapped_column(String(20), unique=True)
    payload: Mapped[dict] = mapped_column(JSON)


class MemberRow(Base):
    __tablename__ = "platform_members"
    __table_args__ = (UniqueConstraint("company_id", "email"),)
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    company_id: Mapped[str] = mapped_column(ForeignKey("platform_organizations.id"), index=True)
    user_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    email: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20))
    token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    expires_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON)


class RecipientRow(Base):
    __tablename__ = "platform_recipients"
    session_id: Mapped[str] = mapped_column(ForeignKey("platform_sessions.id"), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(100), primary_key=True, index=True)
    bundle_id: Mapped[str] = mapped_column(String(100), primary_key=True)


class StreamRow(Base):
    __tablename__ = "platform_streams"
    session_id: Mapped[str] = mapped_column(ForeignKey("platform_sessions.id"), primary_key=True)
    stream_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)


class DeliveryRow(Base):
    __tablename__ = "platform_deliveries"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("platform_sessions.id"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    lease_until: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[dict] = mapped_column(JSON)


@lru_cache
def engine():
    url = settings.database_url or "sqlite:///./platform.db"
    return create_engine(
        url, connect_args={"check_same_thread": False} if url.startswith("sqlite") else {}
    )


def database():
    with Session(engine()) as db:
        yield db
