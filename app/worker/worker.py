import os
import json
import time
import docker
import requests
from pymongo import MongoClient
from bson import ObjectId
import redis

mongo_client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = mongo_client["coding_platform"]
redis_client = redis.Redis.from_url(os.getenv("REDIS_URI", "redis://localhost:6379"))
# Im Cluster gibt es diesen Socket nicht, dort läuft containerd statt Docker.
# Der Worker startet die Ausführung dann entweder als eigenen Pod über die
# Kubernetes-API oder er läuft selbst unter der gVisor-RuntimeClass und führt
# den Code im eigenen Pod aus. Die Wahl steht noch aus.
docker_client = docker.from_env()

# Leer im lokalen Lauf, dann nimmt Docker seine Standardlaufzeit runc. Im
# Cluster steht hier runsc, damit gVisor den eingereichten Code ausführt.
SANDBOX_RUNTIME = os.getenv("SANDBOX_RUNTIME") or None
SANDBOX_TIMEOUT = 5


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
    container = None
    try:
        container = docker_client.containers.run(
            image="python:3.11-slim",
            command=["python", "-c", wrapped_code],
            # Die drei Grenzen setzt im Cluster der Pod: eine NetworkPolicy
            # ohne erlaubten Verkehr, resources.limits.memory und
            # resources.limits.cpu.
            network_mode="none",  # Keinen Netzwerkzugriff erlauben!
            mem_limit="128m",  # RAM begrenzen
            nano_cpus=500000000,  # Max 0.5 CPU Cores
            runtime=SANDBOX_RUNTIME,
            detach=True,  # ohne das wartet run selbst, ohne Zeitlimit
        )
        try:
            # Das Zeitlimit begrenzt nur das Warten, nicht den Container. Der
            # läuft weiter und muss selbst beendet werden.
            result = container.wait(timeout=SANDBOX_TIMEOUT)
        except requests.exceptions.RequestException:
            # Den abgelaufenen Lesevorgang meldet docker je nach Fall als
            # ReadTimeout oder als ConnectionError. Statt den Typ zu prüfen:
            # läuft der Container noch, dann war es das Zeitlimit. Wenn nicht,
            # ist die Verbindung zum Daemon das Problem und der Fehler gehört
            # nicht der Einreichung angelastet.
            container.reload()
            if container.status != "running":
                raise
            container.kill()
            return f"TIMEOUT: Zeitlimit von {SANDBOX_TIMEOUT} Sekunden überschritten"

        output = container.logs().decode("utf-8").strip()
        if result["StatusCode"] != 0:
            return f"EXECUTION_ERROR: {output}"
        return output
    except Exception as e:
        # Mit dem Typ, sonst sieht ein Fehler im Worker aus wie ein Fehler im
        # eingereichten Code.
        return f"SYSTEM_ERROR: {type(e).__name__}: {e}"
    finally:
        # Ohne das bleibt je Testfall ein beendeter Container liegen.
        if container is not None:
            container.remove(force=True)


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
