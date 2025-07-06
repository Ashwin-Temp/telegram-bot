FROM python:3.11-slim

# Install dependencies
RUN apt update && apt install -y ffmpeg gcc

# Set working directory
WORKDIR /app

# Copy code
COPY . .

# Install Python packages
RUN pip install --no-cache-dir -r requirements.txt

# Start the bot
CMD ["python", "bot.py"]
