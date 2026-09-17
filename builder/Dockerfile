# Godot, headless, with the web export templates. CPU only: no GPU needed to export.
FROM python:3.12-slim

ARG GODOT=4.5.1
RUN apt-get update && apt-get install -y --no-install-recommends curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN curl -sSL -o /tmp/godot.zip \
      "https://github.com/godotengine/godot/releases/download/${GODOT}-stable/Godot_v${GODOT}-stable_linux.x86_64.zip" \
    && unzip -q /tmp/godot.zip -d /usr/local/bin && rm /tmp/godot.zip \
    && mv /usr/local/bin/Godot_v${GODOT}-stable_linux.x86_64 /usr/local/bin/godot \
    && chmod +x /usr/local/bin/godot
RUN curl -sSL -o /tmp/tpl.tpz \
      "https://github.com/godotengine/godot/releases/download/${GODOT}-stable/Godot_v${GODOT}-stable_export_templates.tpz" \
    && mkdir -p "/root/.local/share/godot/export_templates/${GODOT}.stable" \
    && unzip -q -j /tmp/tpl.tpz 'templates/web*' -d "/root/.local/share/godot/export_templates/${GODOT}.stable" \
    && rm /tmp/tpl.tpz

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
ENV PORT=8080 GODOT_VERSION=${GODOT}
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
