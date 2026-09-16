# Dieselbe Basis wie der Judge-Worker. Der Distributionsname steht dabei, weil
# der kurze -slim-Tag die Debian-Basis von allein wechselt.
FROM python:3.12-slim-trixie
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# start.py bindet einen Socket für IPv4 und IPv6 und übergibt ihn an uvicorn
# (#123). Der Service im Cluster steht auf ipFamilies [IPv6], Compose leitet
# über IPv4 weiter, und kubectl port-forward spricht 127.0.0.1 im Pod an.
# Exec-Form, damit python PID 1 wird und SIGTERM erreicht.
CMD ["python", "start.py"]