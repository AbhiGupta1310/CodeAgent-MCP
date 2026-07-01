FROM python:3.11-slim

# Install git (required for cloning repos)
RUN apt-get update && \
    apt-get install -y git && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy project files
COPY pyproject.toml .
COPY . .

# Install dependencies
RUN pip install -e .

# Create sessions directory
RUN mkdir -p /tmp/codeagent_sessions

EXPOSE 8000

CMD ["python", "-m", "code_server.server"]
