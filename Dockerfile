FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py geometry.py store.py ./
COPY templates/ templates/
COPY static/ static/

# 登记数据目录，docker-compose 挂载命名卷以便重启后仍可查
ENV DATA_DIR=/app/data
VOLUME ["/app/data"]

EXPOSE 8000
CMD ["python", "app.py"]
