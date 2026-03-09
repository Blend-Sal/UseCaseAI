import os
import re
import uuid
import json
import csv
import io
import time
from threading import Thread
from typing import Any

from fastapi import FastAPI, HTTPException, BackgroundTasks, Header
from nicegui import ui, app as ng_app

from database import SessionLocal, engine
from models import Base, SessionModel, UserModel
from schemas import InputRequest, AuthRequest
from workflow import (
    analyze_node,
    story_node,
    acceptance_node,
    priority_node,
    generate_open_questions,
    run_full_workflow,  # optional
)
from auth import hash_password, verify_password

STORAGE_SECRET = os.getenv("NICEGUI_STORAGE_SECRET", "token")

Base.metadata.create_all(bind=engine)
app = FastAPI()

STATUS_MAP = {
    "idle": "Bereit",
    "running": "Läuft",
    "completed": "Abgeschlossen",
    "error": "Fehler",
    "waiting_for_approval": "Warten auf Freigabe",
    "waiting_for_answers": "Offene Fragen (optional)",
}

PHASE_MAP = {
    "idle": "-",
    "analyze": "Analyse läuft",
    "analyze_done": "Analyse abgeschlossen",
    "analyze_questions": "Offene Fragen (Analyse)",
    "story": "User Stories werden generiert",
    "story_done": "User Stories abgeschlossen",
    "story_questions": "Offene Fragen (Stories)",
    "acceptance": "Akzeptanzkriterien werden generiert",
    "acceptance_done": "Akzeptanzkriterien abgeschlossen",
    "acceptance_questions": "Offene Fragen (Akzeptanzkriterien)",
    "priority": "Priorisierung läuft",
    "priority_done": "Priorisierung abgeschlossen",
    "priority_questions": "Offene Fragen (Priorisierung)",
    "done": "Workflow abgeschlossen",
}

PASSWORD_RULES = [
    ("len", "Mindestens 8 Zeichen", lambda s: len(s) >= 8),
    ("digit", "Mindestens 1 Zahl", lambda s: bool(re.search(r"\d", s))),
    ("special", "Mindestens 1 Sonderzeichen", lambda s: bool(re.search(r"[^\w\s]", s))),
]

HELP_MARKDOWN = """
### 1) Start
1. In **„Anforderung“** deinen Use Case / deine Idee kurz beschreiben (1–10 Sätze reichen).
2. **„Start Analyse“** klicken.

### 2) Ablauf (Phasen)
Das System läuft **schrittweise** und stoppt nach jeder Phase:
1. **Analyse**  
   Ergebnis: Ziel, Akteure, funktionale / nicht-funktionale Anforderungen, offene Punkte.
2. **User Stories**  
   Ergebnis: Stories im Format „Als … möchte ich … damit …“.
3. **Akzeptanzkriterien**  
   Ergebnis: testbare Kriterien (Gherkin).
4. **Priorisierung**  
   Ergebnis: Priorität je Story (High/Medium/Low) + kurze Begründung.

Nach jeder Phase: Status **„Warten auf Freigabe“**.  
→ Mit **„Freigeben & nächster Schritt“** startest du die nächste Phase.

### 3) Offene Fragen (optional, nur auf Klick)
Pro Abschnitt gibt es **„Fragen“**:
- Klick auf **„Fragen“** erzeugt Klärungsfragen passend zur Phase.
- Danach erscheint der Block **„Offene Fragen (optional)“**.

Du kannst dann:
- **Antworten geben** → **„Antworten übernehmen & neu generieren“**  
  → Der aktuelle Abschnitt wird mit deinen Antworten verbessert/neu erzeugt.
- **Überspringen** → **„Überspringen & weiter“** oder **„Freigeben & nächster Schritt“**  
  → Es geht ohne Zusatzinfos weiter („best effort“).

### 4) Manuelle Änderungen (wichtig)
- Pro Abschnitt: **„Bearbeiten“** → Text ändern → **„Speichern“**
- **Wichtig:** Wenn du z.B. in der **Analyse** etwas änderst, reagieren die nächsten Phasen erst darauf,
  **wenn du den Workflow weiterlaufen lässt** (also die nächste Phase neu generieren lässt).  
  Bereits erzeugte spätere Phasen aktualisieren sich **nicht automatisch**.

### 5) Export
- **Export JSON**: maschinenlesbar
- **Export CSV**: für Excel/Sheets

### 6) Sessions / Login
- **Gast**: keine Speicherung
- **Login**: Sessions werden gespeichert und links angezeigt
- **WICHTIG**: Es können nur 10 Sessions gleichzeitig Pro Account existieren
"""


def password_checks(pw: str) -> dict[str, bool]:
    pw = pw or ""
    return {key: fn(pw) for key, _, fn in PASSWORD_RULES}


def validate_password(pw: str) -> tuple[bool, str]:
    checks = password_checks(pw)
    if all(checks.values()):
        return True, ""
    missing = [label for (key, label, _) in PASSWORD_RULES if not checks[key]]
    return False, "Passwort erfüllt nicht: " + ", ".join(missing)


@app.get("/health/live")
def live():
    return {"status": "alive"}


@app.get("/health/ready")
def ready():
    return {"status": "ready"}


def _order_by_created_or_id(query, model):
    if hasattr(model, "created_at"):
        return query.order_by(model.created_at.asc())
    return query.order_by(model.id.asc())


def _order_by_created_or_id_desc(query, model):
    if hasattr(model, "created_at"):
        return query.order_by(model.created_at.desc())
    return query.order_by(model.id.desc())


def _enforce_session_limit(db, user_id: str):
    q = db.query(SessionModel).filter(SessionModel.user_id == user_id)
    sessions = _order_by_created_or_id(q, SessionModel).all()
    while len(sessions) > 10:
        db.delete(sessions[0])
        db.commit()
        sessions.pop(0)


def _strip_open_questions(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"(?is)\n*\s*offene\s+fragen\s*:.*$", "", text).strip()


def _extract_open_questions(text: str) -> list[str]:
    if not text:
        return []
    m = re.search(r"(?im)^\s*offene\s+fragen\s*:\s*$", text)
    if m:
        tail = text[m.end() :]
        qs = []
        for ln in tail.splitlines():
            if not ln.strip():
                if qs:
                    break
                continue
            if re.match(r"^\s*[\-\*\u2022]\s+", ln) or re.match(r"^\s*\d+\.\s+", ln):
                q = re.sub(r"^\s*[\-\*\u2022]\s*", "", ln).strip()
                q = re.sub(r"^\s*\d+\.\s*", "", q).strip()
                if q:
                    qs.append(q)
            elif "?" in ln and len(ln.strip()) <= 180:
                qs.append(ln.strip())

        out, seen = [], set()
        for q in qs:
            qn = q.strip()
            if not qn:
                continue
            k = qn.lower()
            if k in seen:
                continue
            seen.add(k)
            out.append(qn)
            if len(out) >= 6:
                break
        return out

    qs = []
    for ln in text.splitlines():
        ln = ln.strip()
        if "?" in ln and len(ln) <= 160:
            qs.append(ln)

    out, seen = [], set()
    for q in qs:
        k = q.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(q)
        if len(out) >= 6:
            break
    return out


def _answers_text(questions: list[str], answers: dict[str, str]) -> str:
    lines = []
    for i, q in enumerate(questions):
        a = (answers.get(str(i), "") or "").strip()
        if not a:
            continue
        lines.append(f"- {q}\n  Antwort: {a}")
    return "\n".join(lines).strip()


def _field_for_stage(stage: str) -> str:
    return {"analyze": "analysis", "story": "stories", "acceptance": "acceptance", "priority": "priority"}[stage]


def _stage_for_field(field: str) -> str:
    return {"analysis": "analyze", "stories": "story", "acceptance": "acceptance", "priority": "priority"}[field]


def _next_stage_from_done(done_stage: str) -> str | None:
    return {
        "analyze_done": "story",
        "story_done": "acceptance",
        "acceptance_done": "priority",
        "priority_done": None,
    }.get(done_stage)


def _normalize_stage_for_approve(stage: str) -> str:
    s = (stage or "").strip()
    if s.endswith("_questions"):
        return s.replace("_questions", "_done")
    return s


def _context_for_question_generation(stage: str, state: dict) -> str:
    if stage == "analyze":
        return state.get("input_text", "")
    if stage == "story":
        return state.get("analysis", "")
    if stage == "acceptance":
        return state.get("stories", "")
    if stage == "priority":
        return state.get("stories", "")
    return ""


STAGE_ORDER = ["analyze", "story", "acceptance", "priority"]


def _stage_idx(stage: str) -> int:
    try:
        return STAGE_ORDER.index(stage)
    except ValueError:
        return 0


def _furthest_generated(obj: Any) -> str:
    if (getattr(obj, "priority", "") or "").strip():
        return "priority"
    if (getattr(obj, "acceptance", "") or "").strip():
        return "acceptance"
    if (getattr(obj, "stories", "") or "").strip():
        return "story"
    if (getattr(obj, "analysis", "") or "").strip():
        return "analyze"
    return "analyze"


def _regen_span(start_stage: str, cap_stage: str) -> list[str]:
    si = _stage_idx(start_stage)
    ci = _stage_idx(cap_stage)
    if ci < si:
        ci = si
    return STAGE_ORDER[si : ci + 1]


def _clear_only(obj: Any, stages: list[str]) -> None:
    for st in stages:
        if st == "story":
            obj.stories = ""
        elif st == "acceptance":
            obj.acceptance = ""
        elif st == "priority":
            obj.priority = ""


def _regen_start_for_answers(stage: str) -> str:
    return {
        "analyze": "analyze",
        "story": "story",
        "acceptance": "acceptance",
        "priority": "acceptance",
    }.get(stage, stage)


def _has_output_for_stage(obj: Any, stage: str) -> bool:
    if stage == "story":
        return bool((getattr(obj, "stories", "") or "").strip())
    if stage == "acceptance":
        return bool((getattr(obj, "acceptance", "") or "").strip())
    if stage == "priority":
        return bool((getattr(obj, "priority", "") or "").strip())
    return False


def run_stage(
    session_id: str,
    stage: str,
    answers_text_block: str | None = None,
    regen_cap: str | None = None,
):
    db = SessionLocal()
    session = db.query(SessionModel).filter_by(id=session_id).first()
    if not session:
        db.close()
        return

    analysis_clean = _strip_open_questions(session.analysis or "")
    stories_clean = _strip_open_questions(session.stories or "")
    acceptance_clean = _strip_open_questions(session.acceptance or "")
    priority_clean = _strip_open_questions(session.priority or "")

    state = {
        "input_text": session.original_input or "",
        "analysis": analysis_clean,
        "stories": stories_clean,
        "acceptance": acceptance_clean,
        "priority": priority_clean,
    }

    def _apply_answers_into_state(base_stage: str, answers_block: str):
        if not answers_block:
            return
        if base_stage == "analyze":
            state["input_text"] = state["input_text"].strip() + "\n\nAntworten auf offene Fragen:\n" + answers_block
        elif base_stage == "story":
            state["analysis"] = state["analysis"].strip() + "\n\nAntworten auf offene Fragen:\n" + answers_block
        elif base_stage == "acceptance":
            state["stories"] = state["stories"].strip() + "\n\nAntworten auf offene Fragen:\n" + answers_block
        elif base_stage == "priority":
            state["stories"] = state["stories"].strip() + "\n\nAntworten auf offene Fragen:\n" + answers_block

    def _run_one(st: str):
        nonlocal state
        if st == "analyze":
            out = analyze_node(state)
            state["analysis"] = (out.get("analysis", "") or "").strip()
            session.analysis = state["analysis"]
        elif st == "story":
            if not state.get("analysis", "").strip():
                outa = analyze_node(state)
                state["analysis"] = (outa.get("analysis", "") or "").strip()
                session.analysis = state["analysis"]
            outs = story_node(state)
            state["stories"] = (outs.get("stories", "") or "").strip()
            session.stories = state["stories"]
        elif st == "acceptance":
            if not state.get("analysis", "").strip():
                outa = analyze_node(state)
                state["analysis"] = (outa.get("analysis", "") or "").strip()
                session.analysis = state["analysis"]
            if not state.get("stories", "").strip():
                outs = story_node(state)
                state["stories"] = (outs.get("stories", "") or "").strip()
                session.stories = state["stories"]
            outc = acceptance_node(state)
            state["acceptance"] = (outc.get("acceptance", "") or "").strip()
            session.acceptance = state["acceptance"]
        elif st == "priority":
            if not state.get("analysis", "").strip():
                outa = analyze_node(state)
                state["analysis"] = (outa.get("analysis", "") or "").strip()
                session.analysis = state["analysis"]
            if not state.get("stories", "").strip():
                outs = story_node(state)
                state["stories"] = (outs.get("stories", "") or "").strip()
                session.stories = state["stories"]
            outp = priority_node(state)
            state["priority"] = (outp.get("priority", "") or "").strip()
            session.priority = state["priority"]

    try:
        session.status = "running"
        session.current_stage = stage
        db.commit()

        if answers_text_block:
            _apply_answers_into_state(stage, answers_text_block)

        if answers_text_block:
            cap = regen_cap or _furthest_generated(session)
            regen_stages = _regen_span(stage, cap)
            _clear_only(session, regen_stages)
            db.commit()

            for st in regen_stages:
                _run_one(st)

            if stage == "analyze":
                session.current_stage = "analyze_done"
                session.status = "waiting_for_approval"
            elif stage == "story":
                session.current_stage = "story_done"
                session.status = "waiting_for_approval"
            elif stage == "acceptance":
                session.current_stage = "acceptance_done"
                session.status = "waiting_for_approval"
            elif stage == "priority":
                session.current_stage = "done"
                session.status = "completed"

            db.commit()
            return

        _run_one(stage)

        if stage == "analyze":
            session.current_stage = "analyze_done"
            session.status = "waiting_for_approval"
        elif stage == "story":
            session.current_stage = "story_done"
            session.status = "waiting_for_approval"
        elif stage == "acceptance":
            session.current_stage = "acceptance_done"
            session.status = "waiting_for_approval"
        elif stage == "priority":
            session.current_stage = "done"
            session.status = "completed"

        db.commit()

    except Exception:
        session.status = "error"
        db.commit()
    finally:
        db.close()


def request_questions(session_id: str, stage: str):
    db = SessionLocal()
    session = db.query(SessionModel).filter_by(id=session_id).first()
    if not session:
        db.close()
        return

    field = _field_for_stage(stage)

    analysis_clean = _strip_open_questions(session.analysis or "")
    stories_clean = _strip_open_questions(session.stories or "")
    acceptance_clean = _strip_open_questions(session.acceptance or "")
    priority_clean = _strip_open_questions(session.priority or "")

    state = {
        "input_text": session.original_input or "",
        "analysis": analysis_clean,
        "stories": stories_clean,
        "acceptance": acceptance_clean,
        "priority": priority_clean,
    }

    try:
        session.status = "running"
        session.current_stage = stage
        db.commit()

        context = _context_for_question_generation(stage, state)
        current_output = (state.get(field, "") or "").strip()
        qs = generate_open_questions(stage=stage, context=context, current_output=current_output)

        cleaned_output = _strip_open_questions(current_output)
        if qs:
            new_out = cleaned_output + "\n\nOffene Fragen:\n" + "\n".join([f"- {q}" for q in qs])
            setattr(session, field, new_out)
            session.status = "waiting_for_answers"
            session.current_stage = f"{stage}_questions"
        else:
            setattr(session, field, cleaned_output)
            session.status = "waiting_for_approval"
            session.current_stage = f"{stage}_done"

        db.commit()

    except Exception:
        session.status = "error"
        db.commit()
    finally:
        db.close()


@app.post("/auth/register")
def api_register(req: AuthRequest):
    u = (req.username or "").strip()
    p = req.password or ""
    if not u or not p:
        raise HTTPException(status_code=400, detail="missing username/password")

    ok, msg = validate_password(p)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)

    db = SessionLocal()
    try:
        if db.query(UserModel).filter(UserModel.username == u).first():
            raise HTTPException(status_code=409, detail="username exists")

        user = UserModel(username=u, password_hash=hash_password(p))
        db.add(user)
        db.commit()
        db.refresh(user)
        return {"user_id": user.id, "username": user.username}
    finally:
        db.close()


@app.post("/auth/login")
def api_login(req: AuthRequest):
    u = (req.username or "").strip()
    p = req.password or ""

    db = SessionLocal()
    try:
        user = db.query(UserModel).filter(UserModel.username == u).first()
        if not user:
            raise HTTPException(status_code=401, detail="user not found")
        if not verify_password(p, user.password_hash):
            raise HTTPException(status_code=401, detail="wrong password")
        return {"user_id": user.id, "username": user.username}
    finally:
        db.close()


def _get_user_id_from_api(x_user_id: str | None, body_user_id: str | None) -> str:
    uid = (x_user_id or body_user_id or "").strip()
    if not uid:
        raise HTTPException(status_code=401, detail="missing user_id (X-User-Id header or body.user_id)")
    return uid


@app.post("/sessions")
def create_session(req: InputRequest, background_tasks: BackgroundTasks, x_user_id: str | None = Header(default=None)):
    uid = _get_user_id_from_api(x_user_id, getattr(req, "user_id", None))

    db = SessionLocal()
    try:
        if not db.query(UserModel).filter(UserModel.id == uid).first():
            raise HTTPException(status_code=401, detail="unknown user")

        session = SessionModel(
            id=str(uuid.uuid4()),
            user_id=uid,
            original_input=req.input_text,
            current_stage="analyze",
            status="running",
        )
        db.add(session)
        db.commit()
        _enforce_session_limit(db, uid)
        db.refresh(session)
    finally:
        db.close()

    background_tasks.add_task(run_stage, session.id, "analyze", None, None)
    return {"session_id": session.id}


@app.post("/sessions/{session_id}/approve")
def approve_stage(session_id: str, background_tasks: BackgroundTasks, x_user_id: str | None = Header(default=None)):
    uid = _get_user_id_from_api(x_user_id, None)

    db = SessionLocal()
    try:
        session = db.query(SessionModel).filter_by(id=session_id, user_id=uid).first()
        if not session:
            raise HTTPException(status_code=404, detail="session not found")

        stage_norm = _normalize_stage_for_approve(session.current_stage)
        ns = _next_stage_from_done(stage_norm)

        if ns is None:
            session.status = "completed"
            session.current_stage = "done"
            db.commit()
            return {"status": "completed"}

        background_tasks.add_task(run_stage, session_id, ns, None, None)
        return {"status": "next stage started"}
    finally:
        db.close()


@ui.page("/hilfe")
def help_page():
    dark = ui.dark_mode()

    with ui.column().classes("w-full min-h-screen p-8 gap-6"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("UseCase AI – Hilfe").classes("text-3xl font-semibold")
            with ui.row().classes("gap-2"):
                ui.button("Zurück", on_click=lambda: ui.navigate.to("/")).props("outline")
                ui.button(icon="light_mode", on_click=lambda: setattr(dark, "value", not dark.value)).props("flat")

        with ui.card().classes("w-full max-w-5xl shadow-lg p-6"):
            ui.markdown(HELP_MARKDOWN).classes("text-base leading-relaxed")

        with ui.row().classes("gap-3"):
            ui.button("Zurück zur App", on_click=lambda: ui.navigate.to("/")).props("color=primary")
            ui.button("Start", icon="play_arrow", on_click=lambda: ui.navigate.to("/")).props("outline")


@ui.page("/")
def main_page():
    dark = ui.dark_mode()
    storage = ng_app.storage.user

    guest = {
        "input_text": "",
        "analysis": "",
        "stories": "",
        "acceptance": "",
        "priority": "",
        "status": "idle",
        "current_stage": "idle",
        "qa_stage": None,
        "qa_questions": [],
        "qa_answers": {},
    }

    progress_cache = {
        "status": "idle",
        "stage": "idle",
        "start_ts": None,
        "last_percent": 0,
        "finish_ts": None,
        "finish_from": 0,
    }

    FINISH_ANIM_SEC = float(os.getenv("PROGRESS_FINISH_ANIM_SEC", "0.8"))
    PROGRESS_STEP_SECONDS = {
        "analyze": int(os.getenv("PROGRESS_STEP_ANALYZE_SEC", "25")),
        "story": int(os.getenv("PROGRESS_STEP_STORY_SEC", "25")),
        "acceptance": int(os.getenv("PROGRESS_STEP_ACCEPTANCE_SEC", "25")),
        "priority": int(os.getenv("PROGRESS_STEP_PRIORITY_SEC", "25")),
    }

    def _set_progress_state(status: str, stage: str) -> None:
        prev_status = progress_cache.get("status", "idle")
        prev_stage = progress_cache.get("stage", "idle")

        if status == "running":
            if prev_stage != stage or progress_cache["start_ts"] is None:
                progress_cache["start_ts"] = time.time()
            progress_cache["finish_ts"] = None
        else:
            if prev_status == "running":
                progress_cache["finish_ts"] = time.time()
                progress_cache["finish_from"] = int(progress_cache.get("last_percent", 0))
            progress_cache["start_ts"] = None

        progress_cache["status"] = status
        progress_cache["stage"] = stage

    def _calc_percent() -> int:
        status = progress_cache["status"]
        stage = progress_cache["stage"]

        if stage == "idle":
            return 0

        if status == "running":
            dur = PROGRESS_STEP_SECONDS.get(stage, 25)
            if dur <= 0:
                dur = 25
            elapsed = 0.0
            if progress_cache["start_ts"] is not None:
                elapsed = max(0.0, time.time() - progress_cache["start_ts"])
            linear = int((elapsed / dur) * 100)
            if linear < 0:
                linear = 0
            if linear > 99:
                linear = 99
            return linear

        ft = progress_cache.get("finish_ts")
        if ft is not None:
            dt = time.time() - ft
            if dt < FINISH_ANIM_SEC:
                start = int(progress_cache.get("finish_from", 0))
                p = start + int((dt / FINISH_ANIM_SEC) * (100 - start))
                if p > 100:
                    p = 100
                if p < start:
                    p = start
                return p

        return 100

    def _calc_elapsed() -> int:
        if progress_cache["start_ts"] is None:
            return 0
        return max(0, int(time.time() - progress_cache["start_ts"]))

    def get_user_id() -> str | None:
        return storage.get("user_id")

    def is_logged_in() -> bool:
        return bool(get_user_id())

    def logout():
        storage.clear()
        ui.navigate.to("/")

    current_session = {"id": None}
    edit_state = {"analysis": False, "stories": False, "acceptance": False, "priority": False}
    md_map: dict[str, ui.markdown] = {}
    edit_map: dict[str, ui.textarea] = {}

    qa_boxes: dict[str, ui.column] = {}
    qa_state_key = {"k": None}
    qa_answers_state = {"answers": {}}

    sidebar = None
    toggle_btn = None
    approve_btn = None
    status_label = None
    stage_label = None
    spinner = None
    progress_percent = None
    progress_detail = None
    input_box = None

    guest_lock = {"running": False}

    def toggle_dark():
        dark.value = not dark.value
        if toggle_btn is not None:
            toggle_btn.icon = "light_mode" if dark.value else "dark_mode"

    def load_sessions(limit: int = 10):
        uid = get_user_id()
        if not uid:
            return []
        db = SessionLocal()
        q = db.query(SessionModel).filter(SessionModel.user_id == uid)
        sessions = _order_by_created_or_id_desc(q, SessionModel).limit(limit).all()
        db.close()
        return sessions

    def render_sessions():
        nonlocal sidebar
        if sidebar is None:
            return

        sidebar.clear()
        with sidebar:
            ui.label("Sessions").classes("text-lg font-bold mb-4")

            if not is_logged_in():
                ui.label("Gastmodus: Keine gespeicherten Sessions.").classes("text-sm opacity-70")
                ui.button("Login / Registrieren", on_click=login_dialog.open).props("color=primary").classes("w-full mt-3")
                return

            for s in load_sessions(10):
                title = (s.original_input or "").strip()[:40] or "Leere Session"
                ui.button(title, on_click=lambda e, sid=s.id: select_session(sid)).props("flat").classes(
                    "w-full text-left justify-start"
                )

    def select_session(session_id: str):
        if not is_logged_in():
            ui.notify("Nur mit Login auswählbar", color="negative")
            return

        uid = get_user_id()
        db = SessionLocal()
        s = db.query(SessionModel).filter_by(id=session_id, user_id=uid).first()
        db.close()
        if not s:
            ui.notify("Session nicht gefunden", color="negative")
            return

        current_session["id"] = session_id
        storage["selected_session_id"] = session_id
        refresh()

    def _clear_all_qa():
        for b in qa_boxes.values():
            b.clear()

    def run_stage_guest(
        stage: str,
        answers_text_block: str | None = None,
        regen_cap: str | None = None,
    ):
        try:
            guest["status"] = "running"
            guest["current_stage"] = stage

            state = {
                "input_text": guest["input_text"],
                "analysis": _strip_open_questions(guest["analysis"]),
                "stories": _strip_open_questions(guest["stories"]),
                "acceptance": _strip_open_questions(guest["acceptance"]),
                "priority": _strip_open_questions(guest["priority"]),
            }

            def _apply_answers_into_state(base_stage: str, answers_block: str):
                if not answers_block:
                    return
                if base_stage == "analyze":
                    state["input_text"] = state["input_text"].strip() + "\n\nAntworten auf offene Fragen:\n" + answers_block
                elif base_stage == "story":
                    state["analysis"] = state["analysis"].strip() + "\n\nAntworten auf offene Fragen:\n" + answers_block
                elif base_stage == "acceptance":
                    state["stories"] = state["stories"].strip() + "\n\nAntworten auf offene Fragen:\n" + answers_block
                elif base_stage == "priority":
                    state["stories"] = state["stories"].strip() + "\n\nAntworten auf offene Fragen:\n" + answers_block

            def _run_one(st: str):
                nonlocal state
                if st == "analyze":
                    out = analyze_node(state)
                    state["analysis"] = (out.get("analysis", "") or "").strip()
                    guest["analysis"] = state["analysis"]
                elif st == "story":
                    if not state.get("analysis", "").strip():
                        outa = analyze_node(state)
                        state["analysis"] = (outa.get("analysis", "") or "").strip()
                        guest["analysis"] = state["analysis"]
                    outs = story_node(state)
                    state["stories"] = (outs.get("stories", "") or "").strip()
                    guest["stories"] = state["stories"]
                elif st == "acceptance":
                    if not state.get("analysis", "").strip():
                        outa = analyze_node(state)
                        state["analysis"] = (outa.get("analysis", "") or "").strip()
                        guest["analysis"] = state["analysis"]
                    if not state.get("stories", "").strip():
                        outs = story_node(state)
                        state["stories"] = (outs.get("stories", "") or "").strip()
                        guest["stories"] = state["stories"]
                    outc = acceptance_node(state)
                    state["acceptance"] = (outc.get("acceptance", "") or "").strip()
                    guest["acceptance"] = state["acceptance"]
                elif st == "priority":
                    if not state.get("analysis", "").strip():
                        outa = analyze_node(state)
                        state["analysis"] = (outa.get("analysis", "") or "").strip()
                        guest["analysis"] = state["analysis"]
                    if not state.get("stories", "").strip():
                        outs = story_node(state)
                        state["stories"] = (outs.get("stories", "") or "").strip()
                        guest["stories"] = state["stories"]
                    outp = priority_node(state)
                    state["priority"] = (outp.get("priority", "") or "").strip()
                    guest["priority"] = state["priority"]

            if answers_text_block:
                _apply_answers_into_state(stage, answers_text_block)

            if answers_text_block:
                cap = regen_cap
                if not cap:
                    if (guest.get("priority") or "").strip():
                        cap = "priority"
                    elif (guest.get("acceptance") or "").strip():
                        cap = "acceptance"
                    elif (guest.get("stories") or "").strip():
                        cap = "story"
                    elif (guest.get("analysis") or "").strip():
                        cap = "analyze"
                    else:
                        cap = "analyze"

                regen_stages = _regen_span(stage, cap)

                for st in regen_stages:
                    if st == "story":
                        guest["stories"] = ""
                    elif st == "acceptance":
                        guest["acceptance"] = ""
                    elif st == "priority":
                        guest["priority"] = ""

                for st in regen_stages:
                    _run_one(st)

                if stage == "analyze":
                    guest["current_stage"] = "analyze_done"
                    guest["status"] = "waiting_for_approval"
                elif stage == "story":
                    guest["current_stage"] = "story_done"
                    guest["status"] = "waiting_for_approval"
                elif stage == "acceptance":
                    guest["current_stage"] = "acceptance_done"
                    guest["status"] = "waiting_for_approval"
                elif stage == "priority":
                    guest["current_stage"] = "done"
                    guest["status"] = "completed"
                return

            _run_one(stage)

            if stage == "analyze":
                guest["current_stage"] = "analyze_done"
                guest["status"] = "waiting_for_approval"
            elif stage == "story":
                guest["current_stage"] = "story_done"
                guest["status"] = "waiting_for_approval"
            elif stage == "acceptance":
                guest["current_stage"] = "acceptance_done"
                guest["status"] = "waiting_for_approval"
            elif stage == "priority":
                guest["current_stage"] = "done"
                guest["status"] = "completed"

        except Exception:
            guest["status"] = "error"
        finally:
            guest_lock["running"] = False

    def request_questions_guest(stage: str):
        try:
            guest["status"] = "running"
            guest["current_stage"] = stage

            state = {
                "input_text": guest["input_text"],
                "analysis": _strip_open_questions(guest["analysis"]),
                "stories": _strip_open_questions(guest["stories"]),
                "acceptance": _strip_open_questions(guest["acceptance"]),
                "priority": _strip_open_questions(guest["priority"]),
            }

            field = _field_for_stage(stage)
            context = _context_for_question_generation(stage, state)
            current_output = (state.get(field, "") or "").strip()
            qs = generate_open_questions(stage=stage, context=context, current_output=current_output)

            cleaned_output = _strip_open_questions(current_output)
            if qs:
                out = cleaned_output + "\n\nOffene Fragen:\n" + "\n".join([f"- {q}" for q in qs])
                guest[field] = out
                guest["status"] = "waiting_for_answers"
                guest["current_stage"] = f"{stage}_questions"
                guest["qa_stage"] = stage
                guest["qa_questions"] = qs
                guest["qa_answers"] = {}
            else:
                guest[field] = cleaned_output
                guest["status"] = "waiting_for_approval"
                guest["current_stage"] = f"{stage}_done"

        except Exception:
            guest["status"] = "error"
        finally:
            guest_lock["running"] = False

    login_dialog = ui.dialog()
    with login_dialog:
        with ui.card().classes("w-full max-w-[420px] mx-4 shadow-xl p-6"):
            ui.label("Login / Registrieren").classes("text-2xl font-semibold mb-4")

            with ui.tabs().classes("w-full") as tabs:
                t_login = ui.tab("Login")
                t_reg = ui.tab("Registrieren")

            with ui.tab_panels(tabs, value=t_login).classes("w-full mt-4"):

                with ui.tab_panel(t_login):
                    login_user = ui.input("Username").classes("w-full")
                    login_pw = ui.input("Passwort", password=True, password_toggle_button=True).classes("w-full")

                    def do_login():
                        u = (login_user.value or "").strip()
                        p = (login_pw.value or "")

                        if not u and not p:
                            ui.notify("Bitte Username und Passwort eingeben.", color="negative")
                            return
                        if not u:
                            ui.notify("Bitte Username eingeben.", color="negative")
                            return
                        if not p:
                            ui.notify("Bitte Passwort eingeben.", color="negative")
                            return

                        db = SessionLocal()
                        try:
                            user = db.query(UserModel).filter(UserModel.username == u).first()
                            if not user:
                                ui.notify("Login fehlgeschlagen: Benutzer existiert nicht.", color="negative")
                                return
                            if not verify_password(p, user.password_hash):
                                ui.notify("Login fehlgeschlagen: Passwort ist falsch.", color="negative")
                                return

                            storage.update({"user_id": user.id, "username": user.username})
                            ui.notify("Login erfolgreich.", color="positive")
                            login_dialog.close()
                            ui.navigate.to("/")
                        finally:
                            db.close()

                    ui.button("Login", on_click=do_login).props("color=primary").classes("w-full mt-4")

                with ui.tab_panel(t_reg):
                    reg_user = ui.input("Username").classes("w-full")
                    reg_pw = ui.input("Passwort", password=True, password_toggle_button=True).classes("w-full")
                    reg_pw.props("outlined")

                    pw_status = ui.label("Passwort-Anforderungen:").classes("text-xs mt-2 text-gray-500")
                    rules_col = ui.column().classes("gap-1 mt-2")

                    rule_rows: dict[str, tuple[ui.icon, ui.label]] = {}
                    with rules_col:
                        for key, label, _ in PASSWORD_RULES:
                            with ui.row().classes("items-center gap-2"):
                                ic = ui.icon("cancel").classes("text-red-600")
                                lb = ui.label(label).classes("text-xs text-red-600")
                                rule_rows[key] = (ic, lb)

                    def set_rule(key: str, ok: bool):
                        ic, lb = rule_rows[key]
                        if ok:
                            ic.name = "check_circle"
                            ic.classes(remove="text-red-600", add="text-green-600")
                            lb.classes(remove="text-red-600", add="text-green-600")
                        else:
                            ic.name = "cancel"
                            ic.classes(remove="text-green-600", add="text-red-600")
                            lb.classes(remove="text-green-600", add="text-red-600")

                    def update_pw_feedback() -> None:
                        pw = reg_pw.value or ""
                        checks = password_checks(pw)

                        if not pw:
                            pw_status.text = "Passwort-Anforderungen:"
                            pw_status.classes(remove="text-red-600 text-green-600", add="text-gray-500")
                            for k in checks:
                                set_rule(k, False)
                            return

                        for k in checks:
                            set_rule(k, checks[k])

                        all_ok = all(checks.values())
                        if all_ok:
                            pw_status.text = "Passwort erfüllt alle Anforderungen."
                            pw_status.classes(remove="text-red-600 text-gray-500", add="text-green-600")
                        else:
                            missing = [label for (k, label, _) in PASSWORD_RULES if not checks[k]]
                            pw_status.text = "Fehlt: " + ", ".join(missing)
                            pw_status.classes(remove="text-green-600 text-gray-500", add="text-red-600")

                    reg_pw.on("update:model-value", lambda e: update_pw_feedback())
                    reg_pw.on("input", lambda e: update_pw_feedback())
                    reg_pw.on("keyup", lambda e: update_pw_feedback())

                    update_pw_feedback()

                    def do_register():
                        u = (reg_user.value or "").strip()
                        p = (reg_pw.value or "")

                        if not u and not p:
                            ui.notify("Bitte Username und Passwort eingeben.", color="negative")
                            return
                        if not u:
                            ui.notify("Bitte Username eingeben.", color="negative")
                            return
                        if not p:
                            ui.notify("Bitte Passwort eingeben.", color="negative")
                            return

                        ok, msg = validate_password(p)
                        if not ok:
                            ui.notify(msg, color="negative")
                            update_pw_feedback()
                            return

                        db = SessionLocal()
                        try:
                            if db.query(UserModel).filter(UserModel.username == u).first():
                                ui.notify("Registrierung fehlgeschlagen: Username existiert schon.", color="negative")
                                return

                            user = UserModel(username=u, password_hash=hash_password(p))
                            db.add(user)
                            db.commit()
                            db.refresh(user)

                            storage.update({"user_id": user.id, "username": user.username})
                            ui.notify("Registrierung erfolgreich. Du bist jetzt eingeloggt.", color="positive")
                            login_dialog.close()
                            ui.navigate.to("/")
                        finally:
                            db.close()

                    ui.button("Registrieren", on_click=do_register).props("color=secondary").classes("w-full mt-4")

            ui.button("Schließen", on_click=login_dialog.close).props("flat").classes("w-full mt-4")

    def start():
        text = (input_box.value or "").strip()
        if not text:
            ui.notify("Bitte Anforderung eingeben", color="negative")
            return

        if is_logged_in():
            uid = get_user_id()
            db = SessionLocal()
            session = SessionModel(
                id=str(uuid.uuid4()),
                user_id=uid,
                original_input=text,
                current_stage="analyze",
                status="running",
            )
            db.add(session)
            db.commit()
            _enforce_session_limit(db, uid)
            db.refresh(session)
            db.close()

            current_session["id"] = session.id
            storage["selected_session_id"] = session.id

            _set_progress_state("running", "analyze")
            Thread(target=run_stage, args=(session.id, "analyze", None, None), daemon=True).start()
            render_sessions()
            refresh()
            return

        guest["input_text"] = text
        guest["analysis"] = ""
        guest["stories"] = ""
        guest["acceptance"] = ""
        guest["priority"] = ""
        guest["status"] = "running"
        guest["current_stage"] = "analyze"
        guest["qa_stage"] = None
        guest["qa_questions"] = []
        guest["qa_answers"] = {}
        guest_lock["running"] = True
        _set_progress_state("running", "analyze")
        Thread(target=run_stage_guest, args=("analyze", None, None), daemon=True).start()
        refresh()

    def approve():
        if is_logged_in():
            if not current_session["id"]:
                return

            uid = get_user_id()
            db = SessionLocal()
            session = db.query(SessionModel).filter_by(id=current_session["id"], user_id=uid).first()
            db.close()
            if not session:
                return

            stage_norm = _normalize_stage_for_approve(session.current_stage)
            ns = _next_stage_from_done(stage_norm)

            if ns is None:
                dbx = SessionLocal()
                try:
                    s2 = dbx.query(SessionModel).filter_by(id=session.id, user_id=uid).first()
                    if s2:
                        s2.status = "completed"
                        s2.current_stage = "done"
                        dbx.commit()
                finally:
                    dbx.close()
                refresh()
                return

            db2 = SessionLocal()
            try:
                s2 = db2.query(SessionModel).filter_by(id=session.id, user_id=uid).first()
                if s2 and _has_output_for_stage(s2, ns):
                    if ns == "story":
                        s2.current_stage = "story_done"
                        s2.status = "waiting_for_approval"
                    elif ns == "acceptance":
                        s2.current_stage = "acceptance_done"
                        s2.status = "waiting_for_approval"
                    elif ns == "priority":
                        s2.current_stage = "done"
                        s2.status = "completed"
                    db2.commit()
                    refresh()
                    return
            finally:
                db2.close()

            _set_progress_state("running", ns)
            Thread(target=run_stage, args=(session.id, ns, None, None), daemon=True).start()
            refresh()
            return

        if guest_lock["running"]:
            return

        stage_norm = _normalize_stage_for_approve(guest["current_stage"])
        ns = _next_stage_from_done(stage_norm)
        if ns is None:
            guest["status"] = "completed"
            guest["current_stage"] = "done"
            refresh()
            return

        if ns == "story" and (guest.get("stories") or "").strip():
            guest["current_stage"] = "story_done"
            guest["status"] = "waiting_for_approval"
            refresh()
            return
        if ns == "acceptance" and (guest.get("acceptance") or "").strip():
            guest["current_stage"] = "acceptance_done"
            guest["status"] = "waiting_for_approval"
            refresh()
            return
        if ns == "priority" and (guest.get("priority") or "").strip():
            guest["current_stage"] = "done"
            guest["status"] = "completed"
            refresh()
            return

        guest_lock["running"] = True
        _set_progress_state("running", ns)
        Thread(target=run_stage_guest, args=(ns, None, None), daemon=True).start()
        refresh()

    def ask_questions_for(stage: str):
        if stage not in ("analyze", "story", "acceptance", "priority"):
            return

        if is_logged_in():
            if not current_session["id"]:
                return
            uid = get_user_id()
            db = SessionLocal()
            session = db.query(SessionModel).filter_by(id=current_session["id"], user_id=uid).first()
            db.close()
            if not session:
                return
            _set_progress_state("running", stage)
            Thread(target=request_questions, args=(session.id, stage), daemon=True).start()
            refresh()
            return

        if guest_lock["running"]:
            return
        guest_lock["running"] = True
        _set_progress_state("running", stage)
        Thread(target=request_questions_guest, args=(stage,), daemon=True).start()
        refresh()

    def submit_answers_ui():
        if is_logged_in():
            if not current_session["id"]:
                return
            uid = get_user_id()
            db = SessionLocal()
            session = db.query(SessionModel).filter_by(id=current_session["id"], user_id=uid).first()
            db.close()
            if not session:
                return

            stage = None
            if str(session.current_stage).endswith("_questions"):
                stage = str(session.current_stage).replace("_questions", "")
            if stage not in ("analyze", "story", "acceptance", "priority"):
                return

            field = _field_for_stage(stage)
            db2 = SessionLocal()
            try:
                s2 = db2.query(SessionModel).filter_by(id=session.id, user_id=uid).first()
                txt = getattr(s2, field) or ""
            finally:
                db2.close()

            qs = _extract_open_questions(txt)
            answers_block = _answers_text(qs, qa_answers_state.get("answers", {}))

            eff = _regen_start_for_answers(stage)

            db3 = SessionLocal()
            try:
                s3 = db3.query(SessionModel).filter_by(id=session.id, user_id=uid).first()
                cap = _furthest_generated(s3) if s3 else eff
            finally:
                db3.close()

            _set_progress_state("running", eff)
            Thread(
                target=run_stage,
                args=(session.id, eff, answers_block if answers_block else None, cap),
                daemon=True,
            ).start()
            refresh()
            return

        stage = guest.get("qa_stage")
        if stage not in ("analyze", "story", "acceptance", "priority"):
            return
        qs = guest.get("qa_questions") or []
        answers_block = _answers_text(qs, guest.get("qa_answers") or {})

        eff = _regen_start_for_answers(stage)

        if (guest.get("priority") or "").strip():
            cap = "priority"
        elif (guest.get("acceptance") or "").strip():
            cap = "acceptance"
        elif (guest.get("stories") or "").strip():
            cap = "story"
        elif (guest.get("analysis") or "").strip():
            cap = "analyze"
        else:
            cap = "analyze"

        guest_lock["running"] = True
        _set_progress_state("running", eff)
        Thread(
            target=run_stage_guest,
            args=(eff, answers_block if answers_block else None, cap),
            daemon=True,
        ).start()
        refresh()

    def toggle_edit(field: str):
        edit_state[field] = not edit_state[field]
        refresh()

    def save_field(field: str):
        if edit_map.get(field) is None:
            return

        if is_logged_in():
            if not current_session["id"]:
                return
            uid = get_user_id()
            db = SessionLocal()
            session = db.query(SessionModel).filter_by(id=current_session["id"], user_id=uid).first()
            if session:
                setattr(session, field, edit_map[field].value)
                db.commit()
            db.close()
        else:
            guest[field] = edit_map[field].value

        edit_state[field] = False
        refresh()

    def export_json():
        if is_logged_in():
            if not current_session["id"]:
                return
            uid = get_user_id()
            db = SessionLocal()
            session = db.query(SessionModel).filter_by(id=current_session["id"], user_id=uid).first()
            db.close()
            if not session:
                return
            data = {
                "analysis": session.analysis or "",
                "stories": session.stories or "",
                "acceptance": session.acceptance or "",
                "priority": session.priority or "",
            }
        else:
            data = {
                "analysis": guest["analysis"] or "",
                "stories": guest["stories"] or "",
                "acceptance": guest["acceptance"] or "",
                "priority": guest["priority"] or "",
            }

        ui.download(json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8"), "session.json")

    def export_csv():
        if is_logged_in():
            if not current_session["id"]:
                return
            uid = get_user_id()
            db = SessionLocal()
            session = db.query(SessionModel).filter_by(id=current_session["id"], user_id=uid).first()
            db.close()
            if not session:
                return
            rows = [
                ("Analyse", session.analysis or ""),
                ("User Stories", session.stories or ""),
                ("Akzeptanzkriterien", session.acceptance or ""),
                ("Priorisierung", session.priority or ""),
            ]
        else:
            rows = [
                ("Analyse", guest["analysis"] or ""),
                ("User Stories", guest["stories"] or ""),
                ("Akzeptanzkriterien", guest["acceptance"] or ""),
                ("Priorisierung", guest["priority"] or ""),
            ]

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Bereich", "Inhalt"])
        for a, b in rows:
            writer.writerow([a, b])
        ui.download(output.getvalue().encode("utf-8"), "session.csv")

    def refresh():
        if is_logged_in():
            if not current_session["id"]:
                saved_id = storage.get("selected_session_id")
                if saved_id:
                    uid = get_user_id()
                    db = SessionLocal()
                    s = db.query(SessionModel).filter_by(id=saved_id, user_id=uid).first()
                    db.close()
                    if s:
                        current_session["id"] = saved_id

            if not current_session["id"]:
                _set_progress_state("idle", "idle")
                if status_label is not None:
                    status_label.text = f"Status: {STATUS_MAP.get('idle')}"
                if stage_label is not None:
                    stage_label.text = f"Phase: {PHASE_MAP.get('idle')}"
                if approve_btn is not None:
                    approve_btn.visible = False
                _clear_all_qa()
                qa_state_key["k"] = None
                for f in edit_state.keys():
                    if f in md_map:
                        md_map[f].content = ""
                return

            uid = get_user_id()
            db = SessionLocal()
            session = db.query(SessionModel).filter_by(id=current_session["id"], user_id=uid).first()
            db.close()
            if not session:
                current_session["id"] = None
                storage.pop("selected_session_id", None)
                _set_progress_state("idle", "idle")
                _clear_all_qa()
                qa_state_key["k"] = None
                return

            if session.status == "running" and session.current_stage in PROGRESS_STEP_SECONDS:
                _set_progress_state("running", session.current_stage)
            else:
                _set_progress_state(session.status, "idle")

            if status_label is not None:
                status_label.text = f"Status: {STATUS_MAP.get(session.status, session.status)}"
            if stage_label is not None:
                stage_label.text = f"Phase: {PHASE_MAP.get(session.current_stage, session.current_stage)}"

            for f in edit_state.keys():
                if f in md_map:
                    md_map[f].content = getattr(session, f) or ""
                if f in edit_map:
                    if not edit_state[f]:
                        edit_map[f].value = getattr(session, f) or ""
                    md_map[f].visible = not edit_state[f]
                    edit_map[f].visible = edit_state[f]

            if approve_btn is not None:
                approve_btn.visible = session.status in ("waiting_for_approval", "waiting_for_answers") and session.current_stage != "done"

            if session.status == "waiting_for_answers" and str(session.current_stage).endswith("_questions"):
                st = str(session.current_stage).replace("_questions", "")
                if st in ("analyze", "story", "acceptance", "priority"):
                    field = _field_for_stage(st)
                    txt = getattr(session, field) or ""
                    qs = _extract_open_questions(txt)

                    k = f"{session.id}:{st}:{hash(tuple(qs))}"
                    if qa_state_key["k"] != k:
                        qa_state_key["k"] = k
                        qa_answers_state["answers"] = {}
                        _clear_all_qa()

                        target = qa_boxes.get(field)
                        if target is not None:
                            with target:
                                with ui.card().classes("w-full shadow-lg p-4"):
                                    ui.label("Offene Fragen (optional)").classes("text-lg font-bold")
                                    ui.label("Antworten sind optional.").classes("text-sm opacity-70")
                                    for i, q in enumerate(qs):
                                        inp = ui.input(label=q).classes("w-full")

                                        def _mk(ix: int, el: ui.input):
                                            def _on(_e=None):
                                                qa_answers_state["answers"][str(ix)] = el.value or ""
                                            return _on

                                        inp.on("update:model-value", _mk(i, inp))
                                        inp.on("input", _mk(i, inp))

                                    with ui.row().classes("w-full gap-2 mt-3"):
                                        ui.button("Antworten übernehmen & neu generieren", on_click=submit_answers_ui).props("color=primary").classes("flex-1")
                                        ui.button("Überspringen & weiter", on_click=approve).props("outline").classes("flex-1")
                return

            qa_state_key["k"] = None
            _clear_all_qa()
            return

        if guest["status"] == "running" and guest["current_stage"] in PROGRESS_STEP_SECONDS:
            _set_progress_state("running", guest["current_stage"])
        else:
            _set_progress_state(guest["status"], "idle")

        if status_label is not None:
            status_label.text = f"Status: {STATUS_MAP.get(guest['status'], guest['status'])}"
        if stage_label is not None:
            stage_label.text = f"Phase: {PHASE_MAP.get(guest['current_stage'], guest['current_stage'])}"

        for f in edit_state.keys():
            if f in md_map:
                md_map[f].content = guest.get(f, "") or ""
            if f in edit_map:
                if not edit_state[f]:
                    edit_map[f].value = guest.get(f, "") or ""
                md_map[f].visible = not edit_state[f]
                edit_map[f].visible = edit_state[f]

        if approve_btn is not None:
            approve_btn.visible = guest["status"] in ("waiting_for_approval", "waiting_for_answers") and guest["current_stage"] != "done"

        if guest["status"] == "waiting_for_answers" and str(guest["current_stage"]).endswith("_questions"):
            st = str(guest["current_stage"]).replace("_questions", "")
            if st in ("analyze", "story", "acceptance", "priority"):
                field = _field_for_stage(st)
                qs = guest.get("qa_questions") or []
                k = f"guest:{st}:{hash(tuple(qs))}"
                if qa_state_key["k"] != k:
                    qa_state_key["k"] = k
                    _clear_all_qa()

                    target = qa_boxes.get(field)
                    if target is not None:
                        with target:
                            with ui.card().classes("w-full shadow-lg p-4"):
                                ui.label("Offene Fragen (optional)").classes("text-lg font-bold")
                                ui.label("Antworten sind optional.").classes("text-sm opacity-70")
                                for i, q in enumerate(qs):
                                    inp = ui.input(label=q).classes("w-full")

                                    def _mk(ix: int, el: ui.input):
                                        def _on(_e=None):
                                            a = guest.get("qa_answers") or {}
                                            a[str(ix)] = el.value or ""
                                            guest["qa_answers"] = a
                                        return _on

                                    inp.on("update:model-value", _mk(i, inp))
                                    inp.on("input", _mk(i, inp))

                                with ui.row().classes("w-full gap-2 mt-3"):
                                    ui.button("Antworten übernehmen & neu generieren", on_click=submit_answers_ui).props("color=primary").classes("flex-1")
                                    ui.button("Überspringen & weiter", on_click=approve).props("outline").classes("flex-1")
            return

        qa_state_key["k"] = None
        _clear_all_qa()

    def animate_progress_ui():
        if spinner is None or progress_percent is None or progress_detail is None:
            return

        percent = _calc_percent()
        elapsed = _calc_elapsed()
        stage = progress_cache["stage"]
        status = progress_cache["status"]

        show = stage != "idle"
        spinner.visible = status == "running"
        progress_percent.visible = show
        progress_detail.visible = show

        progress_percent.text = f"{percent}%"
        progress_detail.text = f"{PHASE_MAP.get(stage, stage)} • {elapsed}s"

        progress_cache["last_percent"] = percent

    def section(title: str, field: str):
        stage = _stage_for_field(field)
        with ui.card().classes("w-full mb-6 shadow-lg"):
            with ui.row().classes("justify-between items-center w-full"):
                ui.label(title).classes("text-xl font-bold")
                with ui.row().classes("gap-2"):
                    ui.button("Fragen", on_click=lambda e, st=stage: ask_questions_for(st)).props("flat")
                    ui.button("Bearbeiten", on_click=lambda e, f=field: toggle_edit(f)).props("flat")
                    ui.button("Speichern", on_click=lambda e, f=field: save_field(f)).props("flat color=positive")
            md_map[field] = ui.markdown("")
            edit_map[field] = ui.textarea().classes("w-full")
            edit_map[field].visible = False
            with ui.column().classes("w-full mt-3") as qac:
                qa_boxes[field] = qac

    with ui.row().classes("w-full h-screen"):
        with ui.column().classes("w-72 bg-gray-100 dark:bg-gray-900 p-4 overflow-y-auto") as _sidebar:
            sidebar = _sidebar
            render_sessions()

        with ui.column().classes("flex-1 p-8 overflow-y-auto"):
            with ui.row().classes("justify-between items-center w-full"):
                with ui.row().classes("items-center gap-4"):
                    ui.label("UseCase AI").classes("text-3xl font-semibold")
                    if is_logged_in():
                        ui.label(f"@{storage.get('username', '')}").classes("text-sm opacity-70")
                    else:
                        ui.label("Gast").classes("text-sm opacity-70")

                with ui.row().classes("items-center gap-2"):
                    ui.button("Hilfe", icon="menu_book", on_click=lambda: ui.navigate.to("/hilfe")).props("flat")
                    toggle_btn = ui.button(icon="dark_mode", on_click=toggle_dark).props("flat")
                    if is_logged_in():
                        ui.button("Logout", on_click=logout).props("outline")
                    else:
                        ui.button("Login", on_click=login_dialog.open).props("color=primary")

            input_box = ui.textarea(label="Anforderung").classes("w-full mt-6")

            with ui.row().classes("gap-4 mt-4 items-center"):
                ui.button("Start Analyse", on_click=start).props("color=primary")
                approve_btn = ui.button("Freigeben & nächster Schritt", on_click=approve).props("color=secondary")
                ui.button("Export JSON", on_click=export_json).props("outline")
                ui.button("Export CSV", on_click=export_csv).props("outline")

            status_label = ui.label("Status: -").classes("mt-6")
            stage_label = ui.label("Phase: -")

            with ui.row().classes("items-center gap-3 mt-3"):
                spinner = ui.spinner(size="md")
                progress_percent = ui.label("0%").classes("font-semibold")
                progress_detail = ui.label("").classes("text-sm opacity-70")

            spinner.visible = False
            progress_percent.visible = False
            progress_detail.visible = False

            ui.separator().classes("my-6")

            section("Analyse", "analysis")
            section("User Stories", "stories")
            section("Akzeptanzkriterien", "acceptance")
            section("Priorisierung", "priority")

    ui.timer(1.0, refresh)
    ui.timer(0.2, animate_progress_ui)
    refresh()


ui.run_with(app, storage_secret=STORAGE_SECRET)