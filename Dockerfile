# Cloud Run image. pdftoppm (poppler-utils) is a hard runtime dependency:
# Azure OpenAI accepts images, not PDFs, so quotes are rasterised before
# extraction. See docs/DESIGN.md section 8.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir . && pip install --no-cache-dir "uvicorn[standard]>=0.32"

# Non-root: the container handles customer data and holds API credentials.
RUN useradd --create-home --uid 1001 winch
USER winch

ENV PYTHONUNBUFFERED=1 PORT=8080
EXPOSE 8080
CMD exec uvicorn winch.app:create_app --factory --host 0.0.0.0 --port ${PORT}
