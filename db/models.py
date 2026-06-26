from datetime import datetime
from enum import Enum as PyEnum
from typing import List, Optional
from decimal import Decimal
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, Numeric, Enum, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from bot.utils.timezone import now_moscow

class Base(DeclarativeBase):
    pass

# --- ENUMS ---
class AdminRole(str, PyEnum):
    ADMIN = "admin"
    DEVELOPER = "developer"

class AdminStatus(str, PyEnum):
    ACTIVE = "active"
    BLOCKED = "blocked"

class TournamentStatus(str, PyEnum):
    DRAFT = "draft"
    REGISTRATION_OPEN = "registration_open"
    REGISTRATION_CLOSED = "registration_closed"
    SELECTION_DONE = "selection_done"
    CONFIRMATION_PENDING = "confirmation_pending"
    GROUPS_FORMED = "groups_formed"
    STAGE_IN_PROGRESS = "stage_in_progress"
    RATING_CALCULATED = "rating_calculated"
    FINALISTS_SELECTED = "finalists_selected"
    FINAL_IN_PROGRESS = "final_in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"

class RegistrationStatus(str, PyEnum):
    REGISTERED = "registered"
    SELECTED_MAIN = "selected_main"
    SELECTED_RESERVE = "selected_reserve"
    NOT_SELECTED = "not_selected"
    WITHDRAWN = "withdrawn"
    EXCLUDED = "excluded"

class SubscriptionStatus(str, PyEnum):
    SUBSCRIBED = "subscribed"
    UNSUBSCRIBED = "unsubscribed"
    UNKNOWN = "unknown"

class SubscriptionEventType(str, PyEnum):
    SUBSCRIBED = "subscribed"
    UNSUBSCRIBED = "unsubscribed"

class SubscriptionEventSource(str, PyEnum):
    TELEGRAM_EVENT = "telegram_event"
    SCHEDULED_CHECK = "scheduled_check"

class StageStatus(str, PyEnum):
    PENDING = "pending"
    TEAMS_FORMED = "teams_formed"
    CODE_SENT = "code_sent"
    RESULT_ENTERED = "result_entered"
    COMPLETED = "completed"

class TeamLabel(str, PyEnum):
    A = "A"
    B = "B"

class FinalistSource(str, PyEnum):
    RATING = "rating"
    MANUAL_ADMIN = "manual_admin"


# --- MODELS ---

class Admin(Base):
    __tablename__ = "admins"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    display_name: Mapped[Optional[str]] = mapped_column(String(255))
    role: Mapped[AdminRole] = mapped_column(Enum(AdminRole), default=AdminRole.ADMIN, nullable=False)
    admin_status: Mapped[AdminStatus] = mapped_column(Enum(AdminStatus), default=AdminStatus.ACTIVE, nullable=False)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=now_moscow)

    tournaments_created = relationship("Tournament", back_populates="creator")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    telegram_username: Mapped[Optional[str]] = mapped_column(String(64))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=now_moscow)


class Tournament(Base):
    __tablename__ = "tournaments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_username: Mapped[Optional[str]] = mapped_column(String(64))
    status: Mapped[TournamentStatus] = mapped_column(Enum(TournamentStatus), default=TournamentStatus.DRAFT, nullable=False)
    main_slots: Mapped[int] = mapped_column(Integer, nullable=False)
    reserve_slots: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    group_size: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    subgroup_size: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    final_size: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    created_by: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("admins.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_moscow)
    registration_opened_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    registration_closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    creator = relationship("Admin", back_populates="tournaments_created")
    settings = relationship("TournamentSetting", back_populates="tournament", uselist=False)


class TournamentSetting(Base):
    __tablename__ = "tournament_settings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tournament_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("tournaments.id"), unique=True, nullable=False)
    rules_text: Mapped[Optional[str]] = mapped_column(Text)
    tiebreaker_order: Mapped[Optional[dict]] = mapped_column(Text)  # Для простоты в MySQL можно хранить как текст/JSON вручную
    points_formula: Mapped[Optional[dict]] = mapped_column(Text)
    points_win: Mapped[int] = mapped_column(Integer, default=4, nullable=False)
    points_mvp: Mapped[int] = mapped_column(Integer, default=2, nullable=False)

    tournament = relationship("Tournament", back_populates="settings")


class ProjectSettings(Base):
    __tablename__ = "project_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    rules_text: Mapped[Optional[str]] = mapped_column(Text)
    rules_url: Mapped[Optional[str]] = mapped_column(String(512))
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class Registration(Base):
    __tablename__ = "registrations"
    __table_args__ = (UniqueConstraint("tournament_id", "user_id", name="uq_tournament_user"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tournament_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("tournaments.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    game_nick: Mapped[str] = mapped_column(String(64), nullable=False)
    game_rank: Mapped[str] = mapped_column(String(32), nullable=False)
    contact_telegram: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[RegistrationStatus] = mapped_column(Enum(RegistrationStatus), default=RegistrationStatus.REGISTERED, nullable=False)
    subscription_status: Mapped[SubscriptionStatus] = mapped_column(Enum(SubscriptionStatus), default=SubscriptionStatus.UNKNOWN, nullable=False)
    rules_accepted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    rules_accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    participation_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    participation_confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    unsubscribed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    excluded_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    exclusion_reason: Mapped[Optional[str]] = mapped_column(String(255))
    registered_at: Mapped[datetime] = mapped_column(DateTime, default=now_moscow)


class SubscriptionEvent(Base):
    __tablename__ = "subscription_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    registration_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("registrations.id"), nullable=False)
    event_type: Mapped[SubscriptionEventType] = mapped_column(Enum(SubscriptionEventType), nullable=False)
    source: Mapped[SubscriptionEventSource] = mapped_column(Enum(SubscriptionEventSource), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=now_moscow)


class TournamentGroup(Base):
    __tablename__ = "tournament_groups"
    __table_args__ = (UniqueConstraint("tournament_id", "group_number", name="uq_tournament_group_number"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tournament_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("tournaments.id"), nullable=False)
    group_number: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_moscow)


class GroupMember(Base):
    __tablename__ = "group_members"
    __table_args__ = (UniqueConstraint("group_id", "registration_id", name="uq_group_registration"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("tournament_groups.id"), nullable=False)
    registration_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("registrations.id"), nullable=False)


class Stage(Base):
    __tablename__ = "stages"
    __table_args__ = (UniqueConstraint("tournament_id", "stage_number", name="uq_tournament_stage_number"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tournament_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("tournaments.id"), nullable=False)
    stage_number: Mapped[int] = mapped_column(Integer, nullable=False)
    group_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("tournament_groups.id"))
    is_final: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[StageStatus] = mapped_column(Enum(StageStatus), default=StageStatus.PENDING, nullable=False)
    match_code: Mapped[Optional[str]] = mapped_column(String(32))
    code_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    played_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class StageTeam(Base):
    __tablename__ = "stage_teams"
    __table_args__ = (UniqueConstraint("stage_id", "team_label", name="uq_stage_team_label"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    stage_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("stages.id"), nullable=False)
    team_label: Mapped[TeamLabel] = mapped_column(Enum(TeamLabel), nullable=False)


class StageTeamMember(Base):
    __tablename__ = "stage_team_members"
    __table_args__ = (UniqueConstraint("stage_team_id", "registration_id", name="uq_team_registration"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    stage_team_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("stage_teams.id"), nullable=False)
    registration_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("registrations.id"), nullable=False)


class StageResult(Base):
    __tablename__ = "stage_results"
    __table_args__ = (UniqueConstraint("stage_id", "registration_id", name="uq_stage_registration"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    stage_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("stages.id"), nullable=False)
    registration_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("registrations.id"), nullable=False)
    team_label: Mapped[TeamLabel] = mapped_column(Enum(TeamLabel), nullable=False)
    points: Mapped[Decimal] = mapped_column(Numeric(8, 2), default=Decimal("0.00"), nullable=False)
    placement: Mapped[Optional[int]] = mapped_column(Integer)
    kills: Mapped[Optional[int]] = mapped_column(Integer)
    deaths: Mapped[Optional[int]] = mapped_column(Integer)
    assists: Mapped[Optional[int]] = mapped_column(Integer)
    acs: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 2))
    econ_rating: Mapped[Optional[int]] = mapped_column(Integer)
    first_bloods: Mapped[Optional[int]] = mapped_column(Integer)
    spikes_planted: Mapped[Optional[int]] = mapped_column(Integer)
    spikes_defused: Mapped[Optional[int]] = mapped_column(Integer)
    is_stage_mvp: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    entered_by: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("admins.id"))
    entered_at: Mapped[datetime] = mapped_column(DateTime, default=now_moscow)


class Finalist(Base):
    __tablename__ = "finalists"
    __table_args__ = (UniqueConstraint("tournament_id", "registration_id", name="uq_tournament_finalist"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tournament_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("tournaments.id"), nullable=False)
    registration_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("registrations.id"), nullable=False)
    source: Mapped[FinalistSource] = mapped_column(Enum(FinalistSource), default=FinalistSource.RATING, nullable=False)
    participation_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    participation_confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=now_moscow)


class MvpAward(Base):
    __tablename__ = "mvp_awards"
    __table_args__ = (UniqueConstraint("tournament_id", "team_label", name="uq_tournament_team_mvp"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tournament_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("tournaments.id"), nullable=False)
    team_label: Mapped[TeamLabel] = mapped_column(Enum(TeamLabel), nullable=False)
    registration_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("registrations.id"), nullable=False)
    is_tournament_winner: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    prize_description: Mapped[Optional[str]] = mapped_column(String(255))
    awarded_at: Mapped[datetime] = mapped_column(DateTime, default=now_moscow)


class ReplacementLog(Base):
    __tablename__ = "replacement_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tournament_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("tournaments.id"), nullable=False)
    old_registration_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("registrations.id"), nullable=False)
    new_registration_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("registrations.id"), nullable=False)
    from_stage_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("stages.id"))
    replaced_by_admin_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("admins.id"))
    replaced_at: Mapped[datetime] = mapped_column(DateTime, default=now_moscow)


class NotificationsLog(Base):
    __tablename__ = "notifications_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    registration_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("registrations.id"))
    admin_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("admins.id"))
    notification_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[Optional[str]] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=now_moscow)