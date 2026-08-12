# Dieselbe Basis wie der Judge-Worker. Der Distributionsname steht dabei, weil
# der kurze -slim-Tag die Debian-Basis von allein wechselt.
FROM python:3.12-slim-trixie
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Voreinstellung :: für den Cluster, der ausschließlich IPv6 vergibt: der
# Service steht auf ipFamilies [IPv6], und ein Listener auf 0.0.0.0 nimmt davon
# nichts an. Dual bindet der Socket dabei nicht, uvicorn baut ihn über
# asyncio.create_server und CPython setzt dort IPV6_V6ONLY. docker-compose.yml
# leitet über IPv4 weiter und setzt BIND_HOST deshalb auf 0.0.0.0.
# exec, damit uvicorn PID 1 wird und SIGTERM erreicht.
CMD ["sh", "-c", "exec uvicorn main:app --host ${BIND_HOST:-::} --port 8000"]