FROM node:20-bullseye

# git y Python — necesarios para clonar repo-victima y correr agent_server.py
RUN apt-get update && \
    apt-get install -y python3 python3-pip git && \
    rm -rf /var/lib/apt/lists/*

# OpenCode se instala ACÁ, en el contenedor de Railway — nunca en la PC
# del estudiante.
RUN npm install -g opencode-ai

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt --break-system-packages

COPY . .

# Identidad de git para los commits automáticos del agente.
RUN git config --global user.email "agente@detective-de-bugs.local" && \
    git config --global user.name "El Detective de Bugs"

EXPOSE 5000

CMD ["python3", "agent_server.py"]
