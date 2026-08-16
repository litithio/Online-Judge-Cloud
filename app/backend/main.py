import os
from datetime import datetime, timezone

from fastapi import FastAPI, Depends, HTTPException
from pymongo import MongoClient
from pymongo.errors import PyMongoError
from bson import ObjectId
import redis

from auth import get_current_user

app = FastAPI()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")

# DB Connections
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["coding_platform"]
redis_client = redis.Redis.from_url(os.getenv("REDIS_URI", "redis://localhost:6379"))

# Eigene Verbindung nur für /readyz, mit kurzen Zeitlimits.
#
# Der Client oben behält die Vorgabe von 30 Sekunden. MongoDB läuft als
# ReplicaSet, und während einer Neuwahl des Primary ist für einige Sekunden kein
# Server wählbar. Mit 2 Sekunden würden die fachlichen Routen in dieser Zeit
# scheitern, statt die Wahl abzuwarten.
#
# socketTimeoutMS und connectTimeoutMS zusätzlich zu serverSelectionTimeoutMS:
# Letzteres begrenzt nur die Suche nach einem Server. Antwortet ein gewählter
# Server danach nicht mehr, wartet der Aufruf ohne die beiden anderen Werte
# unbegrenzt, und die Aufrufe von /readyz liefen sich im Threadpool auf.
health_client = MongoClient(
    MONGO_URI,
    serverSelectionTimeoutMS=2000,
    connectTimeoutMS=2000,
    socketTimeoutMS=2000,
)

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


# Die beiden Endpunkte für die Probes tragen bewusst kein Depends(verify_jwt).
# Das kubelet schickt kein Token; eine geschützte Probe schlüge dauerhaft fehl.
@app.get("/healthz")
async def healthz():
    """Läuft der Prozess noch? Hängt an keinem anderen Dienst.

    Die livenessProbe startet den Pod neu, wenn hier nichts mehr kommt. Eine
    Prüfung von MongoDB gehört deshalb nicht hierher: ein Ausfall der Datenbank
    würde sonst reihum alle Pods neu starten, ohne dass ein Neustart hilft.

    async, damit die Antwort im Eventloop entsteht. Die übrigen Endpunkte sind
    synchron und laufen im Threadpool. Wäre /healthz auch dort, könnten
    blockierte Aufrufe es mit ausbremsen und einen Neustart auslösen.
    """
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    """Kann der Pod Anfragen beantworten?

    Geprüft wird MongoDB, weil jede Route sie braucht. Valkey bleibt außen vor:
    ohne die Queue scheitert /submit, während /tasks und /submission weiter
    antworten. Ein Pod ohne Valkey ist also mehr wert als gar kein Pod.
    """
    try:
        health_client.admin.command("ping")
    except PyMongoError as fehler:
        raise HTTPException(
            status_code=503, detail=f"MongoDB nicht erreichbar: {fehler}"
        )
    return {"status": "ok"}


@app.get("/tasks")
def get_tasks(user=Depends(get_current_user)):
    tasks = list(db.tasks.find({}, {"test_cases": 0}))  # Testcases verbergen
    return [parse_json(t) for t in tasks]


@app.get("/tasks/{task_id}")
def get_task(task_id: str, user=Depends(get_current_user)):
    task = db.tasks.find_one({"_id": ObjectId(task_id)}, {"test_cases": 0})
    if not task:
        raise HTTPException(status_code=404, detail="Aufgabe nicht gefunden")
    return parse_json(task)


@app.post("/submit")
def submit_code(payload: dict, user=Depends(get_current_user)):
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
def get_submission_status(sub_id: str, user=Depends(get_current_user)):
    sub = db.submissions.find_one({"_id": ObjectId(sub_id)})
    if not sub:
        raise HTTPException(status_code=404, detail="Submission nicht gefunden")
    return parse_json(sub)
