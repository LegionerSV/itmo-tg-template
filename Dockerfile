FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt \
    && useradd --uid 10001 --create-home student
COPY --chown=student:student app ./app
USER student
CMD ["python", "-m", "app"]
