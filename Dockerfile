FROM python:3.9-slim

RUN apt-get update && apt-get install -y git && apt-get clean

ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

WORKDIR /app

COPY requirements.txt /app/

RUN pip install --no-cache-dir -r requirements.txt

COPY . /app/

CMD ["python3", "-m", "WebStreamer"]
