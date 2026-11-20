FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends ipmitool \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 h12fan && useradd --uid 10001 --gid h12fan --no-create-home h12fan \
    && mkdir /data && chown h12fan:h12fan /data
WORKDIR /app
COPY h12fan /app/h12fan
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 H12FAN_BIND=0.0.0.0 H12FAN_DATA=/data
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD python -c "import base64,os,urllib.request,pathlib; p=pathlib.Path(os.environ['H12FAN_WEB_PASSWORD_FILE']).read_text().rstrip('\r\n') if os.getenv('H12FAN_WEB_PASSWORD_FILE') else os.getenv('H12FAN_WEB_PASSWORD',''); r=urllib.request.Request('http://127.0.0.1:8080/healthz'); r.add_header('Authorization','Basic '+base64.b64encode(('admin:'+p).encode()).decode()); urllib.request.urlopen(r,timeout=4)"
CMD ["python", "-m", "h12fan.server"]
