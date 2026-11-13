FROM python:3.12-slim

WORKDIR /app

# Pretrained forecaster (Amazon Chronos-Bolt) runs on CPU: install the CPU-only torch
# wheel first so chronos-forecasting does not pull the much larger CUDA build.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the model weights into the image: no download when the backend starts.
ENV HF_HOME=/opt/hf
RUN python -c "from chronos import BaseChronosPipeline; BaseChronosPipeline.from_pretrained('amazon/chronos-bolt-small', device_map='cpu')"

COPY . .

# Production CMD: no --reload (dev-only; watchfiles churn + reload semantics).
# For live-editing in dev, docker-compose bind-mounts ./backend:/app — restart
# the container (docker compose restart backend) to pick up code changes.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
