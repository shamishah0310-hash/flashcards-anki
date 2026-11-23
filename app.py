import os
import re
import io
import csv
import json
import socket
from pathlib import Path
from typing import List, Dict, Any, Optional
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field
import yaml
import httpx
from openai import AsyncOpenAI
import pypdf
import uvicorn
from dotenv import load_dotenv

load_dotenv()

def load_config():
    cfg_path = Path(__file__).parent / "config.yaml"
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    for p_name, p_data in cfg.get("providers", {}).items():
        env_v = p_data.get("api_key_env")
        if env_v and not p_data.get("api_key"):
            p_data["api_key"] = os.getenv(env_v, "")
    return cfg

config = load_config()
app = FastAPI(title="Flashcards & Quiz with Anki Export", version="1.0.0")

static_dir = Path(__file__).parent / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

@app.get("/", response_class=HTMLResponse)
async def index():
    idx_path = static_dir / "index.html"
    with open(idx_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

def deduplicate_cards(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    unique = []
    for c in cards:
        q = c.get("front") or c.get("question", "")
        norm = re.sub(r"[^\w\s]", "", q.lower()).strip()
        if len(norm.split()) < 3 or norm in seen:
            continue
        words = set(norm.split())
        is_dup = any(len(words & set(s.split())) / max(len(words | set(s.split())), 1) > 0.82 for s in seen)
        if not is_dup:
            seen.add(norm)
            unique.append(c)
    return unique

async def generate_cards_from_llm(text: str, difficulty: str = "medium", card_count: int = 8, mcq_count: int = 4, provider: str = "groq"):
    prov = config.get("providers", {}).get(provider, {})
    api_key = prov.get("api_key")

    if provider == "mock" or not api_key:
        return {
            "deck_title": "Study Deck: Core Principles",
            "flashcards": [
                {"id": 1, "front": f"What is the core premise of {text[:40].strip()}...?", "back": "It defines foundational concepts and structured relationships.", "tag": "Core Theory"},
                {"id": 2, "front": "Why is active recall superior to passive re-reading?", "back": "Active recall triggers cognitive retrieval cues and strengthens synaptic plasticity.", "tag": "Cognitive Science"},
                {"id": 3, "front": f"How does {difficulty.lower()} difficulty alter retention?", "back": f"It calibrates conceptual depth based on target learning goals.", "tag": "Methodology"}
            ],
            "quizzes": [
                {
                    "id": 1,
                    "question": "Which cognitive mechanism best promotes long-term retention?",
                    "options": ["A) Rote repetition", "B) Spaced active retrieval", "C) Passive highlighting", "D) Single-pass cramming"],
                    "correct_answer": "B",
                    "explanation": "Spaced active retrieval forces neural reconstruction of memory traces."
                }
            ]
        }

    client = AsyncOpenAI(base_url=prov.get("base_url"), api_key=api_key, http_client=httpx.AsyncClient(timeout=45.0))
    prompt = f"""
Analyze this study text and produce:
1. {card_count} active-recall Flashcards (Front/Back/Tag).
2. {mcq_count} challenging Multiple Choice Questions with 4 options ('A', 'B', 'C', 'D'), correct letter, and explanation.

Difficulty: {difficulty}
Study Material:
\"\"\"{text[:10000]}\"\"\"

Return ONLY valid JSON:
{{
  "deck_title": "Descriptive Title",
  "flashcards": [{{"id": 1, "front": "Question?", "back": "Answer.", "tag": "Category"}}],
  "quizzes": [{{"id": 1, "question": "Question?", "options": ["A) ...", "B) ...", "C) ...", "D) ..."], "correct_answer": "A", "explanation": "Why correct."}}]
}}
"""
    res = await client.chat.completions.create(
        model=prov.get("models", [{}])[0].get("id", "llama-3.3-70b-versatile"),
        messages=[{"role": "user", "content": prompt}],
        temperature=0.6,
        response_format={"type": "json_object"}
    )
    raw = res.choices[0].message.content
    data = json.loads(raw)
    data["flashcards"] = deduplicate_cards(data.get("flashcards", []))
    return data

class GenerateReq(BaseModel):
    text: str
    difficulty: Optional[str] = "medium"
    card_count: Optional[int] = 8
    mcq_count: Optional[int] = 4
    provider: Optional[str] = "groq"

@app.post("/api/generate")
async def generate(req: GenerateReq):
    try:
        return await generate_cards_from_llm(req.text, req.difficulty, req.card_count, req.mcq_count, req.provider)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...), difficulty: str = Form("medium"), card_count: int = Form(8), mcq_count: int = Form(4), provider: str = Form("groq")):
    content = await file.read()
    if file.filename.lower().endswith(".pdf"):
        reader = pypdf.PdfReader(io.BytesIO(content))
        text = "\n".join([p.extract_text() or "" for p in reader.pages])
    else:
        text = content.decode("utf-8", errors="ignore")
    return await generate_cards_from_llm(text, difficulty, card_count, mcq_count, provider)

class ExportReq(BaseModel):
    deck_title: Optional[str] = "AI_Study_Deck"
    flashcards: List[dict]

@app.post("/api/export/anki")
async def export_anki(req: ExportReq):
    out = io.StringIO()
    out.write("#separator:Comma\n#html:true\n#tags column:3\n")
    writer = csv.writer(out, quoting=csv.QUOTE_ALL)
    for c in req.flashcards:
        f = c.get("front", "").replace("\n", "<br>")
        b = c.get("back", "").replace("\n", "<br>")
        tag = c.get("tag", "Study")
        writer.writerow([f, b, tag])
    return Response(
        content=out.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{req.deck_title}_anki.csv"'}
    )

def find_open_port(host: str, preferred: int) -> int:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, preferred))
            return preferred
    except OSError:
        for p in [preferred + 1, 8088, 8888, 5000, 7860]:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    s.bind((host, p))
                    return p
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]

if __name__ == "__main__":
    host = config.get("app", {}).get("host", "127.0.0.1")
    pref_port = config.get("app", {}).get("port", 8082)
    port = find_open_port(host, pref_port)
    print(f"\n⚡ Flashcards & Quiz running at: http://{host}:{port}\n")
    uvicorn.run("app:app", host=host, port=port, reload=False)
