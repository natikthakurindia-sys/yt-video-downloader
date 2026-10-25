FROM python:3.11-slim

# FFmpeg aur Node.js install karo (PO Token ke liye zaroori)
RUN apt-get update && apt-get install -y ffmpeg curl && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y nodejs

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# PO Token Provider plugin install karo
RUN pip install bgutil-ytdlp-pot-provider

# PO Token server ka code clone karo
RUN git clone --single-branch --branch 2.0.2 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /bgutil
WORKDIR /bgutil/server
RUN npm ci && npx tsc

WORKDIR /app
COPY . .

# Dono cheezein start karo: Token server aur Flask
CMD node /bgutil/server/build/main.js --port 4416 & \
    gunicorn app:app --bind 0.0.0.0:$PORT --timeout 600
