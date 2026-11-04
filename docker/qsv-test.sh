#!/bin/sh
# Is Intel QSV working inside the container? Tests AV1 encode, then HEVC 10-bit GPU decode -> AV1.
# Usage: docker exec -it encody qsv-test.sh
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

# Same pipeline Encody uses for HEVC 10-bit sources: GPU decode -> GPU encode, frames stay on the GPU.
SRC=/tmp/qsv-test-hevc10.mkv
echo "== Making a 10-bit HEVC clip (hevc_qsv)"
"$FF" -hide_banner -y -v error \
  -init_hw_device vaapi=va:"$DEV" -init_hw_device qsv=qs@va -filter_hw_device qs \
  -f lavfi -i testsrc2=size=1920x1080:rate=24:duration=20 \
  -vf format=p010le,hwupload=extra_hw_frames=64 -c:v hevc_qsv -global_quality 25 "$SRC" \
  || { echo "FAIL: hevc_qsv 10-bit encode failed"; exit 1; }
echo "== HEVC 10-bit -> AV1 with GPU decode (-hwaccel qsv -c:v hevc_qsv)"
"$FF" -hide_banner -y -v error -stats \
  -init_hw_device vaapi=va:"$DEV" -init_hw_device qsv=qs@va -filter_hw_device qs \
  -hwaccel qsv -hwaccel_device qs -hwaccel_output_format qsv -c:v:0 hevc_qsv -i "$SRC" \
  -c:v:0 av1_qsv -global_quality:v:0 26 -preset:v:0 slow "$OUT" \
  || { rm -f "$SRC"; echo "FAIL: GPU decode failed (Encody will fall back to CPU decode; or turn off 'Decode on the GPU')"; exit 1; }
rm -f "$SRC" "$OUT"
echo "PASS: GPU decode works. (fps= should be well above the software-decode run; CPU stays low.)"
