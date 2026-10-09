FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    "openai>=1.40.0" \
    "requests>=2.31.0" \
    "python-dotenv>=1.0.0"

COPY agent /app/agent
COPY models.py /app/models.py
COPY inference.py /app/inference.py
COPY __init__.py /app/__init__.py

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "agent.runner"]
