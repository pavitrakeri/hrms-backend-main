# Base image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy project files
COPY . /app

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Expose port
EXPOSE 8000

# Default command (overridden by compose services)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
