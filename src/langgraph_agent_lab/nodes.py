"""Node functions for the LangGraph workflow.

Each function receives AgentState and returns a partial state update dict.
Do NOT mutate input state — return new values only.

LLM REQUIREMENT:
- classify_node MUST use a real LLM call (structured output for intent classification)
- answer_node MUST use a real LLM call (grounded response generation)
- evaluate_node SHOULD use LLM-as-judge (bonus points; heuristic acceptable for base score)
"""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field

from .llm import get_llm
from .state import AgentState, Route, make_event


class ClassificationResult(BaseModel):
    route: Literal["simple", "tool", "missing_info", "risky", "error"] = Field(
        description="Routing decision for the support query"
    )
    reasoning: str = Field(description="Brief reason for the classification")


CLASSIFY_PROMPT = """You are a support ticket router.
Classify the user query into exactly one route.

Routes (priority order — pick the highest matching route):
1. risky — side effects: refunds, deletions, emails, cancellations, account changes
2. tool — information lookups: order status, tracking, search, lookup by ID
3. missing_info — vague/incomplete queries (e.g. "Can you fix it?")
4. error — system failures: timeouts, crashes, service unavailable
5. simple — FAQ / how-to without tools or risky actions

User query: {query}

Return the single best route following the priority order above."""


ANSWER_PROMPT = """You are a helpful support agent. Generate a concise, professional response.

User query: {query}
Tool results: {tool_results}
Approval status: {approval}
Proposed action: {proposed_action}

Ground your answer in the tool results and context when available. Be helpful and clear."""


# ─── EXAMPLE: working node (provided for reference) ──────────────────
def intake_node(state: AgentState) -> dict:
    """Normalize raw query. This node is provided as a working example."""
    query = state.get("query", "").strip()
    return {
        "query": query,
        "messages": [f"intake:{query[:40]}"],
        "events": [make_event("intake", "completed", "query normalized")],
    }


def classify_node(state: AgentState) -> dict:
    """Classify the query into a route using an LLM."""
    query = state.get("query", "")
    llm = get_llm()
    structured = llm.with_structured_output(ClassificationResult)
    result: ClassificationResult = structured.invoke(CLASSIFY_PROMPT.format(query=query))
    route = result.route
    risk_level = "high" if route == Route.RISKY.value else "low"
    return {
        "route": route,
        "risk_level": risk_level,
        "events": [
            make_event(
                "classify",
                "completed",
                f"route={route}",
                reasoning=result.reasoning,
            )
        ],
    }


def tool_node(state: AgentState) -> dict:
    """Execute a mock tool call with transient failure simulation."""
    route = state.get("route", "")
    attempt = state.get("attempt", 0)
    query = state.get("query", "")

    if route == Route.ERROR.value and attempt < 2:
        result = f"ERROR: transient failure processing request (attempt={attempt})"
    elif route == Route.RISKY.value:
        result = f"SUCCESS: risky action executed for query: {query[:80]}"
    else:
        result = f"SUCCESS: lookup completed for query: {query[:80]}"

    return {
        "tool_results": [result],
        "events": [make_event("tool", "completed", result[:60])],
    }


def evaluate_node(state: AgentState) -> dict:
    """Evaluate tool results — the retry-loop gate."""
    tool_results = state.get("tool_results", [])
    latest = tool_results[-1] if tool_results else ""
    evaluation_result = "needs_retry" if "ERROR" in latest.upper() else "success"
    return {
        "evaluation_result": evaluation_result,
        "events": [
            make_event(
                "evaluate",
                "completed",
                f"evaluation_result={evaluation_result}",
            )
        ],
    }


def answer_node(state: AgentState) -> dict:
    """Generate a final response using an LLM."""
    query = state.get("query", "")
    tool_results = state.get("tool_results", [])
    approval = state.get("approval")
    proposed_action = state.get("proposed_action")

    llm = get_llm()
    prompt = ANSWER_PROMPT.format(
        query=query,
        tool_results=tool_results or "None",
        approval=approval or "Not required",
        proposed_action=proposed_action or "None",
    )
    response = llm.invoke(prompt)
    final_answer = response.content if hasattr(response, "content") else str(response)

    return {
        "final_answer": final_answer,
        "events": [make_event("answer", "completed", "response generated")],
    }


def ask_clarification_node(state: AgentState) -> dict:
    """Ask for missing information instead of hallucinating."""
    query = state.get("query", "")
    pending_question = (
        f"Could you provide more details about your request? "
        f'Your message "{query}" is too vague for us to help effectively.'
    )
    return {
        "pending_question": pending_question,
        "final_answer": pending_question,
        "events": [make_event("clarify", "completed", "clarification requested")],
    }


def risky_action_node(state: AgentState) -> dict:
    """Prepare a risky action for human approval."""
    query = state.get("query", "")
    proposed_action = f"Execute risky support action: {query}"
    return {
        "proposed_action": proposed_action,
        "events": [
            make_event(
                "risky_action",
                "prepared",
                proposed_action[:80],
            )
        ],
    }


def approval_node(state: AgentState) -> dict:
    """Human-in-the-loop approval step."""
    proposed_action = state.get("proposed_action", "unknown action")
    use_interrupt = os.getenv("LANGGRAPH_INTERRUPT", "").lower() == "true"

    if use_interrupt:
        from langgraph.types import interrupt

        decision = interrupt(
            {
                "action": proposed_action,
                "message": "Approve this risky action?",
            }
        )
        if isinstance(decision, dict):
            approved = bool(decision.get("approved", False))
            reviewer = decision.get("reviewer", "human-reviewer")
            comment = decision.get("comment", "")
        else:
            approved = bool(decision)
            reviewer = "human-reviewer"
            comment = ""
    else:
        approved = True
        reviewer = "mock-reviewer"
        comment = "Auto-approved for lab testing"

    approval = {"approved": approved, "reviewer": reviewer, "comment": comment}
    return {
        "approval": approval,
        "events": [
            make_event(
                "approval",
                "completed",
                f"approved={approved}",
                reviewer=reviewer,
            )
        ],
    }


def retry_or_fallback_node(state: AgentState) -> dict:
    """Record a retry attempt."""
    attempt = state.get("attempt", 0) + 1
    error_msg = f"Retry attempt {attempt}: transient tool failure"
    return {
        "attempt": attempt,
        "errors": [error_msg],
        "events": [make_event("retry", "attempt", error_msg)],
    }


def dead_letter_node(state: AgentState) -> dict:
    """Handle unresolvable failures after max retries exceeded."""
    attempt = state.get("attempt", 0)
    max_attempts = state.get("max_attempts", 3)
    final_answer = (
        f"Unable to complete your request after {attempt} attempt(s) "
        f"(max {max_attempts}). The issue has been escalated for manual review."
    )
    return {
        "final_answer": final_answer,
        "events": [make_event("dead_letter", "failed", "max retries exceeded")],
    }


def finalize_node(state: AgentState) -> dict:
    """Emit a final audit event. All routes must pass through here before END."""
    return {
        "events": [make_event("finalize", "completed", "workflow finished")],
    }
