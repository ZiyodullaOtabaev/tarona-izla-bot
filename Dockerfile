FROM python:3.12-slim

# FFmpeg va tizim vositalarini o'rnatish
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Bog'liqliklarni o'rnatish
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Loyiha fayllarini nusxalash
COPY . .

# Downloads papkasini tayyorlash
RUN mkdir -p downloads

# Botni ishga tushirish
CMD ["python", "-u", "bot.py"]
