# Version matches engine/requirements.txt exactly. Registry digest qualification is required before deployment.
FROM mcr.microsoft.com/playwright/python:v1.49.1-noble
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
# public/ exists ONLY after stage_public_context.py verifies an externally accepted manifest.
COPY public/engine/requirements.txt /app/engine/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/engine/requirements.txt \
 && useradd --create-home --uid 10001 --shell /usr/sbin/nologin kac
COPY --chown=10001:10001 public/engine/ /app/engine/
COPY --chown=10001:10001 public/web/ /app/web/
COPY --chown=10001:10001 ["public/supplier-methods.html", "public/Поставщики.md", "public/Поставщики_резерв_Москва.md", "public/ТСН-2001_электро_автоматика.md", "public/индексы.md", "/app/"]
RUN mkdir -p /app/engine/data && chown 10001:10001 /app/engine/data
USER 10001:10001
EXPOSE 8765
# Compose supplies an exact trusted proxy address; there is no public backend port.
CMD ["python", "-m", "uvicorn", "engine.app:app", "--host", "0.0.0.0", "--port", "8765", "--workers", "1", "--no-proxy-headers"]
