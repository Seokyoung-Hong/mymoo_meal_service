# Use the official Python image from the Docker Hub
FROM tiangolo/uvicorn-gunicorn-fastapi:python3.11

# Set the working directory
WORKDIR /app

RUN apt-get update && apt-get install -y postgresql-client

# Copy the requirements.txt file and install dependencies
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy the service source to the working directory
COPY . /app

# Expose port 80
EXPOSE 80

# Command to run the FastAPI application
ENTRYPOINT ["bash", "-c", "\
    alembic upgrade head && \
    exec uvicorn main:app --host 0.0.0.0 --port 80 \
    "]
