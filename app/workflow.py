import os
import re
from typing import TypedDict

from toolbox_client import generate_text
from langgraph.graph import StateGraph, END

SYSTEM_PROMPT = "Du bist ein erfahrener Requirements Engineer."



class WorkflowState(TypedDict):
    input_text: str
    analysis: str
    stories: str
    acceptance: str
    priority: str


def analyze_node(state: WorkflowState):
    result = generate_text(
        SYSTEM_PROMPT,
        f"""
Analysiere folgende Anforderung.
Extrahiere:
- Ziel
- Akteure
- funktionale Anforderungen
- nicht-funktionale Anforderungen

Anforderung:
{state['input_text']}
""",
    )
    state["analysis"] = result.strip()
    return state


def story_node(state: WorkflowState):
    result = generate_text(
        SYSTEM_PROMPT,
        f"""
Keine Emojis.
Alles auf Deutsch.

Erstelle strukturierte User Stories im Format:

Als <Rolle>
möchte ich <Ziel>
damit <Nutzen>

Basierend auf:
{state['analysis']}
""",
    )
    state["stories"] = result.strip()
    return state


def acceptance_node(state: WorkflowState):
    result = generate_text(
        SYSTEM_PROMPT,
        f"""
Erstelle ausschließlich saubere Gherkin-Akzeptanzkriterien.

Nur Deutsch.
Kein Markdown.
Keine Erklärungen.

User Stories:
{state['stories']}
""",
    )
    state["acceptance"] = result.strip()
    return state


def priority_node(state: WorkflowState):
    result = generate_text(
        SYSTEM_PROMPT,
        f"""
        
Keine Emojis
Ordne jeder User Story eine Priorität (High/Medium/Low) zu
und begründe kurz:

{state['stories']}
""",
    )
    state["priority"] = result.strip()
    return state


def build_graph():
    workflow = StateGraph(WorkflowState)

    workflow.add_node("analyze", analyze_node)
    workflow.add_node("story", story_node)
    workflow.add_node("acceptance", acceptance_node)
    workflow.add_node("priority", priority_node)

    workflow.set_entry_point("analyze")

    workflow.add_edge("analyze", "story")
    workflow.add_edge("story", "acceptance")
    workflow.add_edge("acceptance", "priority")
    workflow.add_edge("priority", END)

    return workflow.compile()


graph = build_graph()


def run_full_workflow(input_text: str) -> WorkflowState:
    initial_state: WorkflowState = {
        "input_text": input_text,
        "analysis": "",
        "stories": "",
        "acceptance": "",
        "priority": "",
    }

    result = graph.invoke(initial_state)
    return result

def _normalize_questions(qraw: str) -> list[str]:
    lines = [ln.strip() for ln in (qraw or "").splitlines() if ln.strip()]
    if not lines:
        return []

    if len(lines) == 1 and re.search(r"(?i)^keine\s+offenen\s+fragen\.?$", lines[0]):
        return []

    out = []
    seen = set()
    max_q = int(os.getenv("MAX_OPEN_QUESTIONS", "5"))

    for ln in lines:
        ln = re.sub(r"^\s*[\-\*\u2022]\s*", "", ln)
        ln = re.sub(r"^\s*\d+\.\s*", "", ln)
        ln = ln.strip()
        if not ln:
            continue

        k = ln.lower()
        if k in seen:
            continue

        seen.add(k)
        out.append(ln)

        if len(out) >= max_q:
            break

    return out


def generate_open_questions(stage: str, context: str, current_output: str) -> list[str]:
    if not current_output.strip():
        return []

    prompts = {
        "analyze": """
Erstelle bis zu 5 offene Fragen, die zur Klärung der Anforderung fehlen.
Wenn keine Fragen offen sind, antworte exakt: Keine offenen Fragen.
Nur Fragen, keine Erklärungen.
""",
        "story": """
Erstelle bis zu 5 offene Fragen, die fehlen, damit User Stories eindeutig und umsetzbar sind.
Wenn keine Fragen offen sind, antworte exakt: Keine offenen Fragen.
Nur Fragen, keine Erklärungen.
""",
        "acceptance": """
Erstelle bis zu 5 offene Fragen, die fehlen, damit Akzeptanzkriterien testbar und eindeutig sind.
Wenn keine Fragen offen sind, antworte exakt: Keine offenen Fragen.
Nur Fragen, keine Erklärungen.
""",
        "priority": """
Erstelle bis zu 5 offene Fragen, falls Priorisierung nicht eindeutig möglich ist.
Wenn keine Fragen offen sind, antworte exakt: Keine offenen Fragen.
Nur Fragen, keine Erklärungen.
""",
    }

    p = prompts.get(stage, prompts["analyze"]).strip()

    qraw = generate_text(
        SYSTEM_PROMPT,
        f"""
{p}

Kontext:
{context}

Aktueller Stand:
{current_output}
""".strip(),
    )

    return _normalize_questions(qraw)