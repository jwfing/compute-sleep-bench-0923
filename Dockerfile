FROM python:3.11-slim-bookworm
ARG HERMES_COMMIT=1cec910b6a064d4e4821930be5cfaaf6145a2afd
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates tini && rm -rf /var/lib/apt/lists/*
RUN git init /opt/hermes && cd /opt/hermes && git remote add origin https://github.com/NousResearch/hermes-agent.git && git fetch --depth=1 origin ${HERMES_COMMIT} && git checkout --detach FETCH_HEAD && pip install --no-cache-dir . && pip freeze > /opt/hermes-build-requirements.txt
ENV PYTHONUNBUFFERED=1 HERMES_HOME=/tmp/hermes PORT=8080 HERMES_COMMIT=${HERMES_COMMIT}
WORKDIR /app
COPY bench /app/bench
COPY prompts /app/prompts
EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "bench/bootstrap.py"]
