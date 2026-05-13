FROM python:3.12-slim

WORKDIR /app
COPY app.py /app/app.py
COPY static /app/static

ENV HOST=0.0.0.0
ENV PYTHONUNBUFFERED=1

EXPOSE 8765
CMD ["python", "app.py"]
