import os
import json
import time
import docker
import requests
from pymongo import MongoClient
from bson import ObjectId
import redis

# Leer im lokalen Lauf, dann nimmt Docker seine Standardlaufzeit runc. Im
# Cluster steht hier runsc, damit gVisor den eingereichten Code ausführt.
SANDBOX_RUNTIME = os.getenv("SANDBOX_RUNTIME") or None
SANDBOX_TIMEOUT = 5

# Kommt als ENV aus dem Dockerfile, aus demselben ARG wie FROM. Damit läuft der
# eingereichte Code in der Fassung, die der Judge zusagt, und nicht in einer
# anderen. Ohne Vorgabe, weil eine zweite Zeichenkette an dieser Stelle genau
# die Abweichung wieder möglich machen würde, die das ARG verhindert.
#
# Der leere Wert muss mitgeprüft werden, er kommt durch os.getenv hindurch, und
# der Worker würde dann jede Einreichung mit SYSTEM_ERROR beantworten, statt
# gar nicht zu starten.
SANDBOX_IMAGE = os.getenv("SANDBOX_IMAGE", "").strip()
if not SANDBOX_IMAGE:
    raise SystemExit(
        "SANDBOX_IMAGE ist leer oder nicht gesetzt. Der Wert kommt aus dem "
        "ARG PYTHON_IMAGE in app/worker/Dockerfile, damit der Worker und die "
        "Sandbox dieselbe Fassung von Python verwenden."
    )

# Erst nach der Prüfung der Konfiguration. Andernfalls scheitert der Aufbau der
# Verbindungen zuerst, und eine fehlende Einstellung sieht dann nach einem
# Ausfall von Docker oder der Datenbank aus.
mongo_client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = mongo_client["coding_platform"]
redis_client = redis.Redis.from_url(os.getenv("REDIS_URI", "redis://localhost:6379"))
# Im Cluster gibt es diesen Socket nicht, dort läuft containerd statt Docker.
# Der Worker startet die Ausführung dann entweder als eigenen Pod über die
# Kubernetes-API oder er läuft selbst unter der gVisor-RuntimeClass und führt
# den Code im eigenen Pod aus. Die Wahl steht noch aus.
docker_client = docker.from_env()


def run_code_in_sandbox(code: str, test_input: str) -> str:
    """Führt Code sicher in einem isolierten Docker-Container aus."""
    # Wrapper-Skript, das Input übergibt. Die Eingabe hängt an einem echten
    # Dateideskriptor 0 und nicht nur an sys.stdin, sonst scheitern Lösungen,
    # die sys.stdin.buffer, os.read(0, ...) oder /dev/stdin lesen.
    wrapped_code = f"""
import os
import sys

with open('/tmp/input.txt', 'w') as _f:
    _f.write({repr(test_input)})
_fd = os.open('/tmp/input.txt', os.O_RDONLY)
os.dup2(_fd, 0)
sys.stdin = open(0, closefd=False)

{code}
"""
    container = None
    try:
        container = docker_client.containers.run(
            image=SANDBOX_IMAGE,
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
        # Ohne das bleibt je Testfall ein beendeter Container liegen. Der
        # eigene Fehlerfang muss sein: eine Ausnahme aus dem finally heraus
        # verwirft das Ergebnis der Einreichung und beendet den Worker.
        if container is not None:
            try:
                container.remove(force=True)
            except Exception as e:
                print(f"Container {container.short_id} nicht entfernt: {e}")


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
