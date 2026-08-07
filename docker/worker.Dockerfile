FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir hatchling && \
    pip install --no-cache-dir .
COPY src ./src
CMD ["python", "-m", "orchestration.workers.orchestrator_worker"]