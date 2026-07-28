import os
import json
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

def parse_json(data):
    data["id"] = str(data["_id"])
    del data["_id"]
    return data

@app.get("/tasks")
def get_tasks(user=Depends(verify_jwt)):
    tasks = list(db.tasks.find({}, {"test_cases": 0})) # Testcases verbergen
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
    
    # 1. Submission in MongoDB erstellen
    submission = {
        "user_id": user.get("sub"),
        "username": user.get("preferred_username", "Unknown"),
        "task_id": task_id,
        "code": code,
        "status": "PENDING",
        "result": None
    }
    sub_id = db.submissions.insert_one(submission).inserted_id

    # 2. In Redis Queue schreiben
    job_data = {
        "submission_id": str(sub_id),
        "task_id": task_id,
        "code": code
    }
    redis_client.rpush("code_queue", json.dumps(job_data))

    return {"submission_id": str(sub_id), "status": "PENDING"}

@app.get("/submission/{sub_id}")
def get_submission_status(sub_id: str, user=Depends(verify_jwt)):
    sub = db.submissions.find_one({"_id": ObjectId(sub_id)})
    if not sub:
        raise HTTPException(status_code=404, detail="Submission nicht gefunden")
    return parse_json(sub)