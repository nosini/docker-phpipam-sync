FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY docker_phpipam_sync.py .

CMD ["python", "-u", "docker_phpipam_sync.py"]
