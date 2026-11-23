FROM python:3.14-slim
WORKDIR /service
ENV OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock && useradd --system --uid 10001 --create-home care
COPY app ./app
COPY tests ./tests
COPY deploy ./deploy
USER care
EXPOSE 8080
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
