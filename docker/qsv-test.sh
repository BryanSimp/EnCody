#!/bin/sh
# Step 1 check: is Intel QSV AV1 encoding working inside the container?
# Usage: docker exec -it reencoder qsv-test.sh
DEV=${1:-/dev/dri/renderD128}
FF=${FFMPEG:-/usr/lib/jellyfin-ffmpeg/ffmpeg}
OUT=/tmp/qsv-test.mkv
echo "== Devices"; ls -l /dev/dri || { echo "FAIL: /dev/dri not passed through"; exit 1; }
echo "== Running as: $(id)"
echo "== vainfo"; vainfo --display drm --device "$DEV" 2>&1 | grep -E "Driver version|VAProfileAV1" || echo "WARN: vainfo shows no AV1 profiles"
echo "== Encoders"; "$FF" -hide_banner -encoders 2>/dev/null | grep -E "av1_qsv|hevc_qsv|h264_qsv"
echo "== Encoding 20s 1080p test clip with av1_qsv"
"$FF" -hide_banner -y -v error -stats \
  -init_hw_device vaapi=va:"$DEV" -init_hw_device qsv=qs@va -filter_hw_device qs \
  -f lavfi -i testsrc2=size=1920x1080:rate=24:duration=20 \
  -vf format=nv12,hwupload=extra_hw_frames=64 -c:v av1_qsv -global_quality 26 -preset slow "$OUT" \
  || { echo "FAIL: av1_qsv encode failed"; exit 1; }
"${FFPROBE:-/usr/lib/jellyfin-ffmpeg/ffprobe}" -v error -select_streams v:0 -show_entries stream=codec_name,width,height -of csv=p=0 "$OUT"
rm -f "$OUT"
echo "PASS: av1_qsv works. (Look at the fps= value above; 1080p should be roughly 100+ fps.)"
