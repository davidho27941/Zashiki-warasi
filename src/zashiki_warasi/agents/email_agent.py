"""LangGraph email agent: classify + summarize, checkpointed per message."""

from __future__ import annotations

import logging
from typing import TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import sessionmaker

from zashiki_warasi.agents.llm import get_chat_model
from zashiki_warasi.core.models import EmailAnalysis as EmailAnalysisORM
from zashiki_warasi.core.schemas import EmailAnalysis, EmailMessage

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are an email triage assistant.

For each email you receive, produce a JSON object with:
- category: one of [work, personal, promotional, newsletter, transactional, other]
- importance: one of [high, medium, low]
- summary: one to two sentences capturing the key point

Be concise and accurate. Base your judgement on sender, subject, and body content.
"""


class AgentState(TypedDict):
    email: EmailMessage
    analysis: EmailAnalysis | None


class EmailAgent:
    """LangGraph-based email triage agent.

    Each email is processed in its own thread (thread_id = email.id).
    On crash, restarting with the same email replays from the last
    checkpoint — the analyze node is skipped if its result is already
    saved, so the LLM is never billed twice.
    """

    def __init__(
        self,
        checkpointer: PostgresSaver,
        session_factory: sessionmaker,
    ) -> None:
        self._session_factory = session_factory
        self._model = get_chat_model().with_structured_output(EmailAnalysis)
        self._graph = self._build_graph(checkpointer)

    def _build_graph(self, checkpointer: PostgresSaver):
        builder = StateGraph(AgentState)
        builder.add_node("analyze", self._analyze)
        builder.add_edge(START, "analyze")
        builder.add_edge("analyze", END)
        return builder.compile(checkpointer=checkpointer)

    def _analyze(self, state: AgentState) -> dict:
        email = state["email"]
        user_text = (
            f"From: {email.from_address}\n"
            f"Subject: {email.subject}\n"
            f"Date: {email.received_at.isoformat()}\n"
            f"\n"
            f"{email.body_plain or email.snippet}"
        )
        analysis = self._model.invoke(
            [
                SystemMessage(content=SYSTEM_PROMPT),
                HumanMessage(content=user_text),
            ]
        )
        return {"analysis": analysis}

    def handle_email(self, email: EmailMessage) -> None:
        """Run the agent on one email and persist the analysis.

        Designed to be passed as the `handler` callback to `Poller`.
        Re-invoking with the same email is safe: LangGraph's checkpoint
        keyed by `email.id` makes analyze idempotent, and persistence
        uses an existence check to avoid duplicate rows.
        """
        config = {"configurable": {"thread_id": email.id}}
        result = self._graph.invoke(
            {"email": email, "analysis": None},
            config=config,
        )
        analysis = result.get("analysis")
        if analysis is None:
            logger.warning(f"Agent returned no analysis for {email.id}")
            return
        self._persist(email.id, analysis)
        logger.info(
            f"Analyzed {email.id}: "
            f"category={analysis.category} importance={analysis.importance}"
        )

    def _persist(self, message_id: str, analysis: EmailAnalysis) -> None:
        with self._session_factory() as session:
            if session.get(EmailAnalysisORM, message_id) is not None:
                return
            session.add(
                EmailAnalysisORM(
                    message_id=message_id,
                    category=analysis.category,
                    importance=analysis.importance,
                    summary=analysis.summary,
                )
            )
            session.commit()
