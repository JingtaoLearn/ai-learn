FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 TZ=Asia/Shanghai
WORKDIR /app
COPY pyproject.toml ./
COPY quant ./quant
RUN pip install --no-cache-dir . && useradd -u 10001 -m app && mkdir /data && chown app /data
USER app
EXPOSE 8000
CMD ["uvicorn", "quant.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
