FROM python:3.11-slim
# авто-баст кэша: каждый деплой Railway меняет DEPLOYMENT_ID -> слой pip не кэшируется
ARG RAILWAY_DEPLOYMENT_ID
ARG RAILWAY_GIT_COMMIT_SHA
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg opus-tools libopus0 libffi-dev libsodium-dev curl nodejs && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN echo "cache bust $RAILWAY_DEPLOYMENT_ID $RAILWAY_GIT_COMMIT_SHA $(date +%s)" > /tmp/bust && pip install --no-cache-dir --upgrade pip && pip uninstall -y discord.py discord py-cord pycord 2>/dev/null; pip install --no-cache-dir --force-reinstall -r requirements.txt && python -c "import discord; print(f'discord {discord.__version__} voice_recv={__import__(\"importlib\").util.find_spec(\"discord.ext.voice_recv\") is not None}')"
COPY . .
CMD ["python", "bot.py"]
