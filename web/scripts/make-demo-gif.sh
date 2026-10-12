#!/usr/bin/env bash
# Turn the recorded demo (e2e/demo.mjs) into the README GIF.
#   scripts/make-demo-gif.sh /tmp/redcliff-demo ../docs/images/demo.gif
set -euo pipefail
dir="${1:-/tmp/redcliff-demo}"
out="${2:-../docs/images/demo.gif}"
read -r start end < <(node -e "const t=require('$dir/trim.json'); console.log(t.start, t.end)")
duration=$(node -e "console.log(($end - $start).toFixed(2))")
filters="fps=10,scale=960:-1:flags=lanczos"
ffmpeg -v error -y -ss "$start" -t "$duration" -i "$dir/demo.webm" \
  -vf "$filters,palettegen=max_colors=80:stats_mode=diff" "$dir/palette.png"
ffmpeg -v error -y -ss "$start" -t "$duration" -i "$dir/demo.webm" -i "$dir/palette.png" \
  -lavfi "$filters [x]; [x][1:v] paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle" "$out"
echo "wrote $out ($(du -h "$out" | cut -f1), ${duration}s)"
