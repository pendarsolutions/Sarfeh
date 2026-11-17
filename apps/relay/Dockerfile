# The Bale relay on its own: it needs only FastAPI and httpx, at the versions the app locks in constraints.txt.
#   docker build -f apps/relay/Dockerfile -t finops-relay .
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app
COPY constraints.txt ./
RUN pip install --no-cache-dir -c constraints.txt fastapi uvicorn httpx
COPY apps/relay/finops_relay ./finops_relay
RUN useradd --system --uid 10001 relay && mkdir /data && chown relay /data
USER relay
VOLUME /data
EXPOSE 8090
CMD ["uvicorn", "finops_relay.main:app", "--host", "0.0.0.0", "--port", "8090", "--proxy-headers", "--forwarded-allow-ips=*", "--no-access-log"]
