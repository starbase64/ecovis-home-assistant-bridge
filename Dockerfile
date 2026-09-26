FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bridge/ecovis_ha_bridge.py .
COPY tools/ ./tools/
ENV PYTHONUNBUFFERED=1
CMD ["python", "ecovis_ha_bridge.py"]
