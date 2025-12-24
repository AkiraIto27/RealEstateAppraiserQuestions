#!/usr/bin/env bash
set -euo pipefail

python scripts/rag_local.py explain --bundle r07.jsonl.gz --only-ids r07-012,r07-022,r07-048 --timeout 300 --force
