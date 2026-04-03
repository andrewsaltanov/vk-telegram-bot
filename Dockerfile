FROM python:3.11-slim

WORKDIR /app

# Устанавливаем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем исходники
COPY src/ ./src/

# Создаём папку для базы данных
RUN mkdir -p /app/data

CMD ["python", "src/main.py"]
