import os
from datetime import datetime, timezone

from fastapi import FastAPI, Depends, HTTPException
from pymongo import MongoClient
from bson import ObjectId
import redis

from auth import verify_jwt

app = FastAPI()

# DB Connections
mongo_client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = mongo_client["coding_platform"]
redis_client = redis.Redis.from_url(os.getenv("REDIS_URI", "redis://localhost:6379"))

# Trägt sowohl den Durchlauf aus #82 (RUNNING mit abgelaufener Frist finden)
# als auch dessen Requeue-Vergleich. Ohne ihn liefe die Suche über alle
# Einreichungen, nicht nur über die wartenden.
db.submissions.create_index([("status", 1), ("frist", 1)])

# Nur Python ist bis #6 tatsächlich wählbar, der Worker führt keine andere
# Sprache aus. Weitere Einträge kommen mit den jeweiligen Worker-Images.
SPRACHEN = ("python",)
STANDARD_SPRACHE = "python"


def parse_json(data):
    data["id"] = str(data["_id"])
    del data["_id"]
    return data


@app.get("/tasks")
def get_tasks(user=Depends(verify_jwt)):
    tasks = list(db.tasks.find({}, {"test_cases": 0}))  # Testcases verbergen
    return [parse_json(t) for t in tasks]


@app.get("/tasks/{task_id}")
def get_task(task_id: str, user=Depends(verify_jwt)):
    task = db.tasks.find_one({"_id": ObjectId(task_id)}, {"test_cases": 0})
    if not task:
        raise HTTPException(status_code=404, detail="Aufgabe nicht gefunden")
    return parse_json(task)


@app.post("/submit")
def submit_code(payload: dict, user=Depends(verify_jwt)):
    task_id = payload.get("task_id")
    code = payload.get("code")
    sprache = payload.get("sprache", STANDARD_SPRACHE)
    if sprache not in SPRACHEN:
        raise HTTPException(status_code=400, detail=f"Unbekannte Sprache: {sprache}")

    # 1. Submission in MongoDB erstellen, mit den Feldern aus #82: sprache für
    # die Queue-Auswahl des Durchlaufs, versuche/run_token/frist für die
    # bedingte Übernahme im Worker. Die API selbst besetzt nur versuche mit 0
    # und die anderen beiden mit None, der Worker füllt sie beim Claim.
    submission = {
        "user_id": user.get("sub"),
        "username": user.get("preferred_username", "Unknown"),
        "task_id": task_id,
        "code": code,
        "sprache": sprache,
        "status": "PENDING",
        "result": None,
        "test_results": None,
        "versuche": 0,
        "run_token": None,
        "frist": None,
        "worker_id": None,
        # Einzige Stelle, die die Anlage selbst festhält; der Worker schreibt
        # danach nur noch updated_at. Ohne created_at ließe sich nicht sagen,
        # wie lange eine Einreichung schon in der Warteschlange steht.
        "created_at": datetime.now(timezone.utc),
    }
    sub_id = db.submissions.insert_one(submission).inserted_id

    # 2. Nur die ID in die Queue der Sprache schreiben. Code und Aufgabe liest
    # der Worker erst nach der Übernahme aus MongoDB, siehe worker.py.
    redis_client.rpush(f"judge:{sprache}", str(sub_id))

    return {"submission_id": str(sub_id), "status": "PENDING"}


@app.get("/submission/{sub_id}")
def get_submission_status(sub_id: str, user=Depends(verify_jwt)):
    sub = db.submissions.find_one({"_id": ObjectId(sub_id)})
    if not sub:
        raise HTTPException(status_code=404, detail="Submission nicht gefunden")
    return parse_json(sub)
