FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN adduser --disabled-password --gecos "" hestia

COPY pyproject.toml README.md /app/
COPY hestia /app/hestia
COPY scripts /app/scripts

RUN pip install --no-cache-dir -e ".[s3]"

USER hestia
EXPOSE 8500

CMD ["uvicorn", "hestia.main:app", "--host", "0.0.0.0", "--port", "8500", "--proxy-headers"]
