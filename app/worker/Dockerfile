FROM python:3.11-slim
# Ohne das puffert Python die Ausgabe blockweise und der Worker schweigt, bis
# der Puffer voll ist. Im Cluster bliebe kubectl logs genauso leer.
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "worker.py"]