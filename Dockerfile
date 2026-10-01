FROM python:3.12-slim
WORKDIR /app
ENV PORT=10000 PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 10000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '10000') + '/health', timeout=3)"
CMD ["python", "main.py"]