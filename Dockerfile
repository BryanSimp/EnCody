FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PUID=1000 PGID=1000 TZ=America/Chicago \
    LIBVA_DRIVER_NAME=iHD \
    FFMPEG=/usr/lib/jellyfin-ffmpeg/ffmpeg \
    FFPROBE=/usr/lib/jellyfin-ffmpeg/ffprobe \
    PATH=/opt/venv/bin:/usr/lib/jellyfin-ffmpeg:$PATH

# jellyfin-ffmpeg (QSV/VA-API build) + Intel media driver and oneVPL runtime for Arc
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg tzdata python3 python3-venv \
 && install -d /etc/apt/keyrings \
 && curl -fsSL https://repo.jellyfin.org/jellyfin_team.gpg.key | gpg --dearmor -o /etc/apt/keyrings/jellyfin.gpg \
 && printf 'Types: deb\nURIs: https://repo.jellyfin.org/ubuntu\nSuites: noble\nComponents: main\nArchitectures: amd64\nSigned-By: /etc/apt/keyrings/jellyfin.gpg\n' \
      > /etc/apt/sources.list.d/jellyfin.sources \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      jellyfin-ffmpeg7 intel-media-va-driver-non-free libvpl2 libmfx-gen1.2 vainfo intel-gpu-tools \
 && apt-get purge -y gnupg && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /app
COPY reencoder /app/reencoder
COPY docker/entrypoint.sh docker/qsv-test.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/qsv-test.sh

VOLUME /config
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python3", "-m", "reencoder"]
