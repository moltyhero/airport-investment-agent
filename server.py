import uuid

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from airport_agent.agent import Agent

app = FastAPI(title="Airport Investment Intelligence Agent")
sessions: dict = {}


class Msg(BaseModel):
    message: str
    session_id: str | None = None


@app.post("/api/chat")
def chat(m: Msg):
    sid = m.session_id or str(uuid.uuid4())
    agent = sessions.setdefault(sid, Agent())
    return {"session_id": sid, **agent.chat(m.message)}


@app.get("/")
def index():
    return FileResponse("static/index.html")
