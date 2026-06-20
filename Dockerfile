FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY cart_optimizer/ ./cart_optimizer/
COPY webapp/ ./webapp/
COPY knapsack.py ./

# Create data dir so it works standalone (no volume mount required).
# With a volume mount (e.g. -v flavourscout-data:/data) sessions + coupon
# ledger persist across container restarts.
RUN mkdir -p /data

ENV COUPON_DB=/data/coupons.db
ENV SESSION_FILE=/data/sessions.json
ENV SESSION_SECRET=docker-local-dev
EXPOSE 8000

CMD ["sh", "-c", "uvicorn webapp.server:app --host 0.0.0.0 --port ${PORT:-8000}"]
