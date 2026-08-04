FROM python:3.11-slim

# Московское время — все расписания и «вчера/сегодня» считаются по нему
ENV TZ=Europe/Moscow \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Папка для отчётов и снимков. На Railway сюда монтируется постоянный диск,
# иначе снимки для внутридневного сравнения терялись бы при перезапуске.
ENV DATA_DIR=/data
RUN mkdir -p /data/output /data/snapshots

CMD ["python", "worker.py"]
