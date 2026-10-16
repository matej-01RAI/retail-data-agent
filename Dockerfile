# Retail Data Analyst: the CLI and the HTTP API in one image.
#
#   docker build -t retail-agent .
#   docker run -it --rm --env-file .env retail-agent --user ceo --offline      # chat, synthetic data
#   docker run -it --rm --env-file .env retail-agent --user ceo                # chat, BigQuery
#   docker run --rm -p 8000:8000 --env-file .env retail-agent serve --host 0.0.0.0 --offline
#
# Secrets come from the environment at run time (.env is never copied into the image).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY retail_agent ./retail_agent
COPY config ./config
COPY knowledge ./knowledge

# The offline dataset: synthetic, ending on the day the image is built, opened read-only.
ENV DUCKDB_PATH=/opt/retail-agent/thelook_sample.duckdb
RUN mkdir -p /opt/retail-agent && python -m retail_agent sample-db

# Reports, preferences, metrics and traces go to /data (mount a volume to keep them).
RUN useradd --create-home --uid 10001 app && mkdir /data && chown app /data
USER app
ENV DATA_DIR=/data

EXPOSE 8000
ENTRYPOINT ["python", "-m", "retail_agent"]
CMD ["--user", "ceo"]
