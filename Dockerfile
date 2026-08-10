FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p state logs

ENV PYTHONUNBUFFERED=1
ENV TZ=UTC

# ENTRYPOINT is just the interpreter so `docker-compose run algo <script>`
# actually runs the script. The old `ENTRYPOINT ["python", "main.py"]` made
# every `run … tools/force_liquidate.py` silently start the live algo instead
# (args were appended to main.py) — which is why orphans never flattened and
# every "liquidate" attempt reprinted RECONCILIATION MISMATCH on Telegram.
ENTRYPOINT ["python"]
CMD ["main.py"]
