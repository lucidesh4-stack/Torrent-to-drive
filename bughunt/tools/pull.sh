#!/usr/bin/env bash
# pull.sh — sync live CloudFlow/Streamly repo into bughunt/live/ and record HEAD.
# RUN FIRST EVERY SESSION. The live repo changes between turns (line numbers drift).
#
# HF raw needs -L (resolve/main returns a 307 redirect to a CDN).
set -euo pipefail

SPACE="lucidesh4/cloudflow"
BASE="https://huggingface.co/spaces/${SPACE}/resolve/main"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # bughunt/
DEST="${HERE}/live"
mkdir -p "$DEST"

FILES="
streamly_optimized/app.py
streamly_optimized/search_service.py
streamly_optimized/cloud_service.py
streamly_optimized/redis_store.py
streamly_optimized/store.py
streamly_optimized/security.py
streamly_optimized/config.py
streamly_optimized/auth_utils.py
streamly_optimized/extensions.py
streamly_optimized/routes/__init__.py
streamly_optimized/routes/search.py
streamly_optimized/routes/cloud.py
streamly_optimized/routes/queue.py
streamly_optimized/routes/telegram.py
streamly_optimized/routes/auth.py
streamly_optimized/routes/history.py
streamly_optimized/static/js/src/1-core.js
streamly_optimized/static/js/src/2-cloud.js
streamly_optimized/static/js/src/3-search-sort.js
streamly_optimized/static/js/src/3b-series.js
streamly_optimized/static/js/src/4-history.js
streamly_optimized/static/js/src/4b-telegram-transfers.js
streamly_optimized/static/js/src/5-search.js
streamly_optimized/static/js/src/6-main.js
streamly_optimized/static/js/src/_wrap_open.txt
streamly_optimized/static/js/src/_wrap_close.txt
streamly_optimized/static/js/app.js
streamly_optimized/static/css/base.css
streamly_optimized/static/css/responsive.css
streamly_optimized/templates/index.html
AUDIT_suggestions.md
README.md
DEPLOY.md
Dockerfile
render.yaml
"

echo "Pulling ${SPACE} -> ${DEST}"
for f in $FILES; do
  mkdir -p "$DEST/$(dirname "$f")"
  code=$(curl -sL -o "$DEST/$f" -w "%{http_code}" --max-time 30 "$BASE/$f" || echo "ERR")
  sz=$(wc -c < "$DEST/$f" 2>/dev/null || echo 0)
  printf "%s  %7sB  %s\n" "$code" "$sz" "$f"
done

HEAD=$(curl -s --max-time 20 "https://huggingface.co/api/spaces/${SPACE}/refs" \
  | grep -o '"targetCommit":"[a-f0-9]*"' | head -1 | cut -d'"' -f4)
echo "$HEAD" > "$DEST/.HEAD"
echo "HEAD = $HEAD  (written to live/.HEAD)"
