FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Yerevan
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY partyhub ./partyhub
COPY config ./config
COPY tools ./tools

RUN useradd --create-home app && chown -R app /app
USER app

EXPOSE 8000
CMD ["python", "-m", "partyhub", "serve", "--port", "8000"]
