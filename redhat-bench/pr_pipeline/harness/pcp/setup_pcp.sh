#!/bin/bash
# SWE-bench harness setup script for PCP C-fix instances.
#
# Called by the harness BEFORE the agent starts. Sets up the repo at
# base_commit with a full PCP source build, then applies test_patch.
#
# Usage: setup_pcp.sh <base_commit> <test_patch_file>

set -euo pipefail

TESTBED="/testbed"
QA_DIR="/var/lib/pcp/testsuite"
BASE_COMMIT="$1"
TEST_PATCH="$2"

echo "[setup] Checking out $BASE_COMMIT..."
cd "$TESTBED"
git checkout -- . 2>/dev/null
git clean -fd 2>/dev/null
git checkout "$BASE_COMMIT"

echo "[setup] Building PCP from source at $BASE_COMMIT..."
./configure --prefix=/usr --sysconfdir=/etc --localstatedir=/var \
    --disable-qt --disable-qt3d 2>&1 | tail -3
make -j8 2>&1 | tail -5
make install 2>&1 | tail -5

echo "[setup] Syncing qa directory..."
rsync -a "$TESTBED/qa/" "$QA_DIR/" --exclude='.git'

echo "[setup] Rebuilding testsuite binaries..."
cd "$QA_DIR" && make -C src -j$(nproc) 2>&1 | tail -3 || true

echo "[setup] Configuring networking for pmcd..."
mkdir -p /run/pcp
# Ensure hostname resolves to IPv4 loopback (pmcd binds 127.0.0.1)
cp /etc/hosts /tmp/hosts.bak 2>/dev/null
(echo "127.0.0.1 $(hostname) localhost"; cat /tmp/hosts.bak) > /etc/hosts 2>/dev/null || true

echo "[setup] Starting PCP services..."
/usr/libexec/pcp/lib/pmcd restart 2>/dev/null || /usr/libexec/pcp/lib/pmcd start 2>/dev/null
sleep 3
cd /var/lib/pcp/pmdas/sample && echo pipe | ./Install 2>/dev/null | tail -1
cd /var/lib/pcp/pmdas/simple && echo pipe | ./Install 2>/dev/null | tail -1
/usr/libexec/pcp/lib/pmlogger restart 2>/dev/null || /usr/libexec/pcp/lib/pmlogger start 2>/dev/null || true
sleep 2

echo "[setup] Applying test_patch..."
cd "$TESTBED"
if [ -f "$TEST_PATCH" ]; then
    # Strip binary diffs that can't be applied
    grep -v "^Binary files\|^GIT binary patch" "$TEST_PATCH" > /tmp/test_patch_clean.diff || true

    # Ensure trailing newline
    [ -n "$(tail -c1 /tmp/test_patch_clean.diff)" ] && echo >> /tmp/test_patch_clean.diff

    git apply /tmp/test_patch_clean.diff 2>/dev/null \
        || git apply --3way /tmp/test_patch_clean.diff 2>/dev/null \
        || patch --batch --fuzz=5 -p1 -i /tmp/test_patch_clean.diff 2>/dev/null \
        || echo "[setup] WARNING: test_patch apply failed"
fi

# Sync updated qa tests
rsync -a "$TESTBED/qa/" "$QA_DIR/" --exclude='.git'

echo "[setup] Done. Agent can now modify files in /testbed and run eval_pcp.sh"
