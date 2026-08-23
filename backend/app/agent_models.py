from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class AgentRun(Base):
    """One auditable Bali curation run.

    The run stores outcomes and tool observations only. It intentionally never
    stores private chain-of-thought or provider response bodies.
    """

    __tablename__ = "agent_runs"
    __table_args__ = (UniqueConstraint("active_slot", name="uq_agent_run_active_slot"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    region_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("regions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    active_slot: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    mode: Mapped[str] = mapped_column(String(30), default="full", index=True)
    trigger: Mapped[str] = mapped_column(String(30), default="manual", index=True)
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    objective: Mapped[str] = mapped_column(Text, default="")
    score: Mapped[float] = mapped_column(Float, default=0.0)
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    summary: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    steps: Mapped[list["AgentRunStep"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="AgentRunStep.sequence"
    )


class AgentRunStep(Base):
    __tablename__ = "agent_run_steps"
    __table_args__ = (UniqueConstraint("run_id", "sequence", name="uq_agent_run_step_sequence"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    phase: Mapped[str] = mapped_column(String(40), default="act", index=True)
    tool: Mapped[str] = mapped_column(String(100), default="", index=True)
    outcome: Mapped[str] = mapped_column(String(30), default="ok", index=True)
    score_delta: Mapped[float] = mapped_column(Float, default=0.0)
    detail: Mapped[str] = mapped_column(Text, default="")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    run: Mapped[AgentRun] = relationship(back_populates="steps")


class AgentTask(Base):
    """Durable quality/research backlog shared by scheduled and manual runs."""

    __tablename__ = "agent_tasks"
    __table_args__ = (
        UniqueConstraint("region_id", "kind", "target_key", name="uq_agent_task_region_kind_target"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    region_id: Mapped[int] = mapped_column(ForeignKey("regions.id", ondelete="CASCADE"), index=True)
    place_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("places.id", ondelete="SET NULL"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(40), default="research", index=True)
    target_key: Mapped[str] = mapped_column(String(180))
    title: Mapped[str] = mapped_column(String(240))
    detail: Mapped[str] = mapped_column(Text, default="")
    success_metric: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[int] = mapped_column(Integer, default=50, index=True)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[str] = mapped_column(Text, default="")
    retry_after: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentMission(Base):
    """A resumable objective that can span several Fargate executions."""

    __tablename__ = "agent_missions"

    id: Mapped[int] = mapped_column(primary_key=True)
    region_id: Mapped[int] = mapped_column(ForeignKey("regions.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("agent_tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(40), default="research", index=True)
    title: Mapped[str] = mapped_column(String(240))
    objective: Mapped[str] = mapped_column(Text, default="")
    success_metric: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="active", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=50, index=True)
    strategy_json: Mapped[str] = mapped_column(Text, default="{}")
    progress_json: Mapped[str] = mapped_column(Text, default="{}")
    last_run_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentWorkItem(Base):
    __tablename__ = "agent_work_items"
    __table_args__ = (UniqueConstraint("mission_id", "target_key", name="uq_agent_work_item_target"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    mission_id: Mapped[int] = mapped_column(ForeignKey("agent_missions.id", ondelete="CASCADE"), index=True)
    region_id: Mapped[int] = mapped_column(ForeignKey("regions.id", ondelete="CASCADE"), index=True)
    place_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("places.id", ondelete="SET NULL"), nullable=True, index=True
    )
    target_type: Mapped[str] = mapped_column(String(40), default="task", index=True)
    target_key: Mapped[str] = mapped_column(String(180))
    title: Mapped[str] = mapped_column(String(240))
    goal: Mapped[str] = mapped_column(Text, default="")
    definition_of_done: Mapped[str] = mapped_column(Text, default="")
    stage: Mapped[str] = mapped_column(String(40), default="observe", index=True)
    status: Mapped[str] = mapped_column(String(30), default="ready", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=50, index=True)
    state_summary: Mapped[str] = mapped_column(Text, default="")
    current_hypothesis: Mapped[str] = mapped_column(Text, default="")
    next_action_json: Mapped[str] = mapped_column(Text, default="{}")
    failed_approaches_json: Mapped[str] = mapped_column(Text, default="[]")
    blocked_reason: Mapped[str] = mapped_column(Text, default="")
    retry_condition: Mapped[str] = mapped_column(Text, default="")
    evidence_summary: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_run_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentCheckpoint(Base):
    """Compact persisted state, never hidden model reasoning."""

    __tablename__ = "agent_checkpoints"

    id: Mapped[int] = mapped_column(primary_key=True)
    mission_id: Mapped[int] = mapped_column(ForeignKey("agent_missions.id", ondelete="CASCADE"), index=True)
    work_item_id: Mapped[int] = mapped_column(ForeignKey("agent_work_items.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    state_summary: Mapped[str] = mapped_column(Text, default="")
    decision: Mapped[str] = mapped_column(Text, default="")
    new_facts_json: Mapped[str] = mapped_column(Text, default="[]")
    rejected_claims_json: Mapped[str] = mapped_column(Text, default="[]")
    failed_approaches_json: Mapped[str] = mapped_column(Text, default="[]")
    next_action_json: Mapped[str] = mapped_column(Text, default="{}")
    outcome: Mapped[str] = mapped_column(String(30), default="observed", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class AgentEvidence(Base):
    """Claim-level source observation, including rejected/blocked sources."""

    __tablename__ = "agent_evidence"
    __table_args__ = (UniqueConstraint("fingerprint", name="uq_agent_evidence_fingerprint"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    region_id: Mapped[int] = mapped_column(ForeignKey("regions.id", ondelete="CASCADE"), index=True)
    mission_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("agent_missions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    work_item_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("agent_work_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    place_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("places.id", ondelete="SET NULL"), nullable=True, index=True
    )
    run_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_type: Mapped[str] = mapped_column(String(40), default="web", index=True)
    url: Mapped[str] = mapped_column(String(1000), default="", index=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    claim: Mapped[str] = mapped_column(Text, default="")
    excerpt: Mapped[str] = mapped_column(Text, default="")
    source_status: Mapped[str] = mapped_column(String(30), default="discovered", index=True)
    rejection_reason: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class AgentKnowledge(Base):
    __tablename__ = "agent_knowledge"
    __table_args__ = (UniqueConstraint("topic", name="uq_agent_knowledge_topic"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    topic: Mapped[str] = mapped_column(String(180), index=True)
    title: Mapped[str] = mapped_column(String(240))
    content: Mapped[str] = mapped_column(Text, default="")
    scope: Mapped[str] = mapped_column(String(30), default="global", index=True)
    region_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("regions.id", ondelete="CASCADE"), nullable=True, index=True
    )
    place_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("places.id", ondelete="SET NULL"), nullable=True, index=True
    )
    category: Mapped[str] = mapped_column(String(40), default="playbook", index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    principles_json: Mapped[str] = mapped_column(Text, default="[]")
    next_actions_json: Mapped[str] = mapped_column(Text, default="[]")
    keywords_json: Mapped[str] = mapped_column(Text, default="[]")
    source_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    evidence_count: Mapped[int] = mapped_column(Integer, default=0)
    quality_score: Mapped[float] = mapped_column(Float, default=0.7)
    status: Mapped[str] = mapped_column(String(30), default="active", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )


class AgentProposal(Base):
    """High-risk create/update/merge/image suggestions requiring admin review."""

    __tablename__ = "agent_proposals"
    __table_args__ = (UniqueConstraint("proposal_key", name="uq_agent_proposal_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    region_id: Mapped[int] = mapped_column(ForeignKey("regions.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    place_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("places.id", ondelete="SET NULL"), nullable=True, index=True
    )
    secondary_place_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("places.id", ondelete="SET NULL"), nullable=True, index=True
    )
    result_place_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("places.id", ondelete="SET NULL"), nullable=True, index=True
    )
    discovery_candidate_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("discovery_candidates.id", ondelete="SET NULL"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(String(240), default="")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    evidence: Mapped[str] = mapped_column(Text, default="")
    source_urls_json: Mapped[str] = mapped_column(Text, default="[]")
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    proposal_key: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    decision_note: Mapped[str] = mapped_column(Text, default="")
    decided_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentQualityGap(Base):
    """Auditable cooldown/waiver for an exact live place quality gap."""

    __tablename__ = "agent_quality_gaps"
    __table_args__ = (UniqueConstraint("place_id", "gap_kind", name="uq_agent_quality_gap_place_kind"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    region_id: Mapped[int] = mapped_column(ForeignKey("regions.id", ondelete="CASCADE"), index=True)
    place_id: Mapped[int] = mapped_column(ForeignKey("places.id", ondelete="CASCADE"), index=True)
    gap_kind: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(30), default="blocked", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    evidence_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    condition_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=1)
    retry_after: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )


class AgentLesson(Base):
    __tablename__ = "agent_lessons"
    __table_args__ = (UniqueConstraint("lesson_key", name="uq_agent_lesson_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    lesson_key: Mapped[str] = mapped_column(String(180), index=True)
    scope: Mapped[str] = mapped_column(String(30), default="global", index=True)
    region_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("regions.id", ondelete="CASCADE"), nullable=True, index=True
    )
    place_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("places.id", ondelete="SET NULL"), nullable=True, index=True
    )
    category: Mapped[str] = mapped_column(String(40), default="workflow", index=True)
    trigger: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    expected_effect: Mapped[str] = mapped_column(Text, default="")
    evidence_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(30), default="candidate", index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    observation_count: Mapped[int] = mapped_column(Integer, default=1)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )
