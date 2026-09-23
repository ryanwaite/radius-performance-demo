#!/usr/bin/env bash
#
# Verifies that the catalog-api build is hermetic: the compile step runs with
# GOPROXY=off and therefore cannot reach proxy.golang.org, so every module must
# already be present in the cache primed by the earlier `go mod download` layer.
#
# This is a negative test, and negative tests are the easiest kind to pass for
# the wrong reason. An earlier version of this check "confirmed" fail-closed
# behaviour while the eviction step was writing to the wrong path
# (GOMODCACHE is /go/pkg/mod in the MCR image, not /root/go/pkg/mod), so the
# build outcome had nothing to do with the mechanism under test.
#
# Two controls now make that impossible:
#
#   1. Positive control -- the eviction step asserts, inside the container,
#      that a populated module cache existed at GOMODCACHE *before* it was
#      removed, and that the directory was empty afterwards. The 'before'
#      half is the one that matters: checking only that the directory ends
#      up empty passes trivially on a wrong path, because `mkdir -p` makes
#      an empty directory anywhere. That is the exact bug this guards.
#   2. Attribution -- the build failure must carry the specific GOPROXY=off
#      module-lookup error. Any other failure is treated as inconclusive, not
#      as success.
#
# One further wrinkle: `docker build --progress=plain` echoes each RUN
# command before running it, so grepping the log for a marker would match the
# recipe rather than its output and report a control as armed even when the
# step never ran. The marker literals below are therefore split across a string
# concatenation in the echo, so the exact string the script greps for can only
# appear in genuine command output.
#
# Usage: benchmark/scripts/verify-hermetic-build.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

pass() { printf '  PASS  %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; exit 1; }

echo "== Control 1: the real build succeeds with GOPROXY=off =="
if docker build --target build -t radius-eval-hermetic-check:baseline -f Dockerfile . >"$WORK/baseline.log" 2>&1; then
  pass "primed module cache satisfies the build without network access"
else
  tail -5 "$WORK/baseline.log"
  fail "baseline build failed; the cache priming layer is broken (see $WORK/baseline.log)"
fi

echo
echo "== Control 2: evicting the module cache makes the same build fail =="

# Derive a variant of the real Dockerfile rather than hand-writing one, so the
# thing under test stays the thing that ships.
python3 - "$WORK/Dockerfile.evicted" <<'PY'
import sys, pathlib

src = pathlib.Path("Dockerfile").read_text()
marker = "RUN GOPROXY=off"
assert marker in src, "Dockerfile no longer contains the GOPROXY=off build step"

eviction = (
    "# Injected by verify-hermetic-build.sh.\n"
    "# Positive control, in two halves. Asserting only that the directory is\n"
    "# empty AFTER the rm is not enough: `mkdir -p` on a wrong path produces an\n"
    "# empty directory too, so that assertion passes trivially and proves\n"
    "# nothing. The load-bearing half is the BEFORE check -- it fails unless\n"
    "# there was a populated module cache at this exact path to destroy.\n"
    'RUN test -n "$(ls -A "$(go env GOMODCACHE)" 2>/dev/null)" \\\n'
    '    && echo "POSITIVE-CONTROL""-ARMED: cache populated at $(go env GOMODCACHE)" \\\n'
    '    && rm -rf "$(go env GOMODCACHE)" \\\n'
    '    && mkdir -p "$(go env GOMODCACHE)" \\\n'
    '    && test -z "$(ls -A "$(go env GOMODCACHE)")" \\\n'
    '    && echo "POSITIVE-CONTROL""-OK: cache emptied at $(go env GOMODCACHE)"\n\n'
)
pathlib.Path(sys.argv[1]).write_text(src.replace(marker, eviction + marker, 1))
PY

set +e
docker build --no-cache --progress=plain --target build \
  -t radius-eval-hermetic-check:evicted \
  -f "$WORK/Dockerfile.evicted" . >"$WORK/evicted.log" 2>&1
status=$?
set -e

if ! grep -q "POSITIVE-CONTROL-ARMED: cache populated" "$WORK/evicted.log"; then
  fail "positive control not armed: no populated module cache was found at GOMODCACHE, so nothing was actually evicted and this run proves nothing (see $WORK/evicted.log)"
fi
if ! grep -q "POSITIVE-CONTROL-OK: cache emptied" "$WORK/evicted.log"; then
  fail "positive control did not complete: the cache was not emptied (see $WORK/evicted.log)"
fi
pass "positive control: cache confirmed populated, then confirmed emptied"

if [ "$status" -eq 0 ]; then
  fail "build SUCCEEDED with an empty module cache -- it is reaching the network"
fi

if grep -q "module lookup disabled by GOPROXY=off" "$WORK/evicted.log"; then
  pass "build failed with the expected GOPROXY=off module lookup error"
else
  echo "--- last 20 lines ---"
  tail -20 "$WORK/evicted.log"
  fail "build failed, but not with the GOPROXY=off error; result is inconclusive"
fi

docker image rm -f radius-eval-hermetic-check:baseline radius-eval-hermetic-check:evicted >/dev/null 2>&1 || true

echo
echo "Hermetic build verified: fail-closed, and the mechanism was proven engaged."
