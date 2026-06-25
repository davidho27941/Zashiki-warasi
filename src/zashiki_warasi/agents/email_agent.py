"""LangGraph email agent: classify + summarize + notify, checkpointed per message."""

from __future__ import annotations

import html
import logging
from typing import TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import sessionmaker

from zashiki_warasi.agents.llm import get_chat_model
from zashiki_warasi.core.models import EmailAnalysis as EmailAnalysisORM
from zashiki_warasi.core.schemas import EmailAnalysis, EmailMessage
from zashiki_warasi.notifications.telegram import TelegramNotifier

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
    checkpoint — completed nodes are skipped, so the LLM is never billed
    twice and a successful notify is never re-sent.

    Graph: START -> analyze -> notify -> END
    """

    def __init__(
        self,
        checkpointer: PostgresSaver,
        session_factory: sessionmaker,
        notifier: TelegramNotifier,
    ) -> None:
        self._session_factory = session_factory
        self._notifier = notifier
        self._model = get_chat_model().with_structured_output(EmailAnalysis)
        self._graph = self._build_graph(checkpointer)

    def _build_graph(self, checkpointer: PostgresSaver):
        builder = StateGraph(AgentState)
        builder.add_node("analyze", self._analyze)
        builder.add_node("notify", self._notify)
        builder.add_edge(START, "analyze")
        builder.add_edge("analyze", "notify")
        builder.add_edge("notify", END)
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

    def _notify(self, state: AgentState) -> dict:
        analysis = state["analysis"]
        if analysis is None:
            logger.warning(
                f"notify: skipping {state['email'].id} — no analysis"
            )
            return {}
        text = _format_telegram_message(state["email"], analysis)
        self._notifier.send_message(text)
        return {}

    def handle_email(self, email: EmailMessage) -> None:
        """Run the agent on one email and persist the analysis.

        Designed to be passed as the `handler` callback to `Poller`.

        Idempotency contract — three cases keyed by `email.id`:
          - No prior state: invoke the graph fresh.
          - Prior state, graph completed (no pending next node): reuse
            the cached analysis; do NOT re-invoke (avoids re-billing the
            LLM and re-sending Telegram).
          - Prior state, graph interrupted: invoke with no input so
            LangGraph resumes from where it stopped.
        """
        config = {"configurable": {"thread_id": email.id}}
        snapshot = self._graph.get_state(config)

        if snapshot.values.get("analysis") is not None and not snapshot.next:
            analysis = snapshot.values["analysis"]
            logger.info(
                f"Reusing cached analysis for {email.id} "
                "(graph already complete)"
            )
        else:
            graph_input = (
                None
                if snapshot.values
                else {"email": email, "analysis": None}
            )
            result = self._graph.invoke(graph_input, config=config)
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


_IMPORTANCE_ICON = {"high": "🔴", "medium": "🟡", "low": "⚪"}


def _format_telegram_message(
    email: EmailMessage, analysis: EmailAnalysis
) -> str:
    """Build the HTML payload for Telegram sendMessage.

    All user-controlled fields are HTML-escaped because Gmail headers
    can legitimately contain `<` `>` `&` (display names, encoded
    subjects).
    """
    icon = _IMPORTANCE_ICON.get(analysis.importance, "")
    return (
        f"{icon} <b>[{analysis.importance.upper()}] "
        f"{html.escape(analysis.category)}</b>\n"
        f"<b>From:</b> {html.escape(email.from_address)}\n"
        f"<b>Subject:</b> {html.escape(email.subject)}\n\n"
        f"{html.escape(analysis.summary)}"
    )
