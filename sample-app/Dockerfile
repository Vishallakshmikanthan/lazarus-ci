FROM python:3.11-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Install dependencies first for layer caching
COPY services/api/requirements.txt /app/requirements.txt
RUN uv pip install --system --no-cache -r requirements.txt

# Copy application code and pipeline scripts
COPY . /app/

RUN mkdir -p /app/logs && chmod 777 /app/logs

EXPOSE 5000

CMD ["python", "-m", "services.api.app"]
