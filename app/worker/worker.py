import os
import json
import time
import docker
from pymongo import MongoClient
from bson import ObjectId
import redis

mongo_client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = mongo_client["coding_platform"]
redis_client = redis.Redis.from_url(os.getenv("REDIS_URI", "redis://localhost:6379"))
docker_client = docker.from_env()


def run_code_in_sandbox(code: str, test_input: str) -> str:
    """Führt Code sicher in einem isolierten Docker-Container aus."""
    # Wrapper-Skript, das Input übergibt
    wrapped_code = f"""
import sys
input_data = {repr(test_input)}
sys.stdin = open('/tmp/input.txt', 'w')
sys.stdin.write(input_data)
sys.stdin.seek(0)

{code}
"""
    try:
        # Container mit strengen Limits starten
        container = docker_client.containers.run(
            image="python:3.11-slim",
            command=["python", "-c", wrapped_code],
            network_mode="none",  # Keinen Netzwerkzugriff erlauben!
            mem_limit="128m",  # RAM begrenzen
            nano_cpus=500000000,  # Max 0.5 CPU Cores
            detach=False,
            stdout=True,
            stderr=True,
            timeout=5,  # Max 5 Sekunden Laufzeit
        )
        return container.decode("utf-8").strip()
    except docker.errors.ContainerError as e:
        return f"EXECUTION_ERROR: {e.stderr.decode('utf-8')}"
    except Exception as e:
        return f"TIMEOUT_OR_SYSTEM_ERROR: {str(e)}"


def process_queue():
    print("Worker gestartet, warte auf Tasks...")
    while True:
        # Blockierendes Pop aus Redis Queue
        _, item = redis_client.blpop("code_queue")
        job = json.loads(item)

        sub_id = job["submission_id"]
        task = db.tasks.find_one({"_id": ObjectId(job["task_id"])})

        if not task:
            continue

        test_cases = task.get("test_cases", [])
        all_passed = True
        error_message = ""

        for case in test_cases:
            output = run_code_in_sandbox(job["code"], case["input"])
            expected = str(case["expected_output"]).strip()

            if output != expected:
                all_passed = False
                error_message = f"Fehler bei Input '{case['input']}': Erwartet '{expected}', Bekommen '{output}'"
                break

        # Ergebnis in MongoDB aktualisieren
        final_status = "SUCCESS" if all_passed else "FAILED"
        result_text = "Alle Tests bestanden!" if all_passed else error_message

        db.submissions.update_one(
            {"_id": ObjectId(sub_id)},
            {
                "$set": {
                    "status": final_status,
                    "result": result_text,
                    "updated_at": time.time(),
                }
            },
        )
        print(f"Submission {sub_id} verarbeitet: {final_status}")


if __name__ == "__main__":
    process_queue()
