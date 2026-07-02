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
streamly/app.py
streamly/search_service.py
streamly/cloud_service.py
streamly/redis_store.py
streamly/store.py
streamly/security.py
streamly/config.py
streamly/auth_utils.py
streamly/extensions.py
streamly/routes/__init__.py
streamly/routes/search.py
streamly/routes/cloud.py
streamly/routes/queue.py
streamly/routes/telegram.py
streamly/routes/auth.py
streamly/routes/history.py
streamly/static/js/src/1-core.js
streamly/static/js/src/2-cloud.js
streamly/static/js/src/3-search-sort.js
streamly/static/js/src/3b-series.js
streamly/static/js/src/4-history.js
streamly/static/js/src/4b-telegram-transfers.js
streamly/static/js/src/5-search.js
streamly/static/js/src/6-main.js
streamly/static/js/src/_wrap_open.txt
streamly/static/js/src/_wrap_close.txt
streamly/static/js/app.js
streamly/static/css/base.css
streamly/static/css/responsive.css
streamly/templates/index.html
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
