FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5021

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "5021", "--log-config", "/dev/null"]
