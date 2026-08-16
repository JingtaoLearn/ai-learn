FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git=1:2.39.5-0+deb12u3 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 --shell /bin/bash research

COPY requirements.lock /tmp/project/requirements.lock
RUN pip install --upgrade pip==25.2 \
    && pip install --no-cache-dir --require-hashes -r /tmp/project/requirements.lock

COPY pyproject.toml README.md /tmp/project/
COPY src /tmp/project/src
RUN pip install --no-cache-dir --no-deps --no-build-isolation /tmp/project

WORKDIR /workspace
EXPOSE 4200 5000 8888
