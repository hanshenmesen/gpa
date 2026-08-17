FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN useradd --create-home --uid 10001 gpa
WORKDIR /app

COPY pyproject.toml README.md ./
COPY deploy/cloud/requirements.txt ./deploy/cloud/requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r deploy/cloud/requirements.txt

COPY gpa ./gpa
COPY demo_web ./demo_web
RUN python -m pip install --no-deps .

USER gpa
EXPOSE 8080
CMD ["gpa-cloud"]
