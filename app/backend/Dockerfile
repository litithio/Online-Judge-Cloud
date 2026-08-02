# Dieselbe Basis wie der Judge-Worker. Der Distributionsname steht dabei, weil
# der kurze -slim-Tag die Debian-Basis von allein wechselt.
FROM python:3.12-slim-trixie
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]