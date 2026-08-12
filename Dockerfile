FROM python:3.13-slim

ARG BUILD_SHA=dev
ARG BUILD_DATE=unknown
ENV BUILD_SHA=$BUILD_SHA BUILD_DATE=$BUILD_DATE

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app app
COPY static static
COPY sample sample

EXPOSE 8000
USER 65534
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
