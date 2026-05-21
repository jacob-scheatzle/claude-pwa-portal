FROM python:3.12-slim

WORKDIR /build
COPY pyproject.toml ./
COPY portal/ ./portal/
RUN pip install --no-cache-dir .

WORKDIR /
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "portal.main:app", "--host", "0.0.0.0", "--port", "8000"]
