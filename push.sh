#!/bin/sh
# commit new data and push; 3 attempts with rebase (own repository only)
git config user.name "github-actions[bot]"; git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add -A data; [ -d results ] && git add -A results; git diff --cached --quiet && exit 0
git commit -q -m "N7 data $(date -u +%Y-%m-%dT%H:%MZ)"
for i in 1 2 3; do git pull -q --rebase && git push -q && exit 0; sleep 3; done; exit 1
