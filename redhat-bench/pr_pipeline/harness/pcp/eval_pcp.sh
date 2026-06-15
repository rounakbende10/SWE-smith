#!/bin/bash
# SWE-bench harness eval script for PCP C-fix instances.
#
# Called by the harness after the agent has applied their fix to /testbed.
# Expects:
#   - /testbed: PCP source repo at base_commit with test_patch + agent's fix applied
#   - PCP already built at base_commit (setup phase did this)
#   - This script rebuilds changed components, installs, and runs qa tests.
#
# Usage: eval_pcp.sh <test_nums>
#   e.g. eval_pcp.sh 1397
#        eval_pcp.sh "987 1671"

set -uo pipefail

TESTBED="/testbed"
QA_DIR="/var/lib/pcp/testsuite"
TEST_NUMS="$*"
TIMEOUT=180

if [ -z "$TEST_NUMS" ]; then
    echo "Usage: eval_pcp.sh <test_num> [test_num ...]"
    exit 1
fi

# --- Step 1: Detect what changed and rebuild ---
cd "$TESTBED"

CHANGED_C=$(git diff --name-only 2>/dev/null | grep -E '\.(c|h)$' | grep '^src/' || true)

if [ -n "$CHANGED_C" ]; then
    echo "[eval] Rebuilding changed C files..."

    # Touch changed files to force recompilation
    echo "$CHANGED_C" | xargs -r touch
    sleep 1

    # Find unique build directories
    BUILD_DIRS=$(echo "$CHANGED_C" | while read f; do
        parts=(${f//\// })
        if [[ "${parts[1]}" == "libpcp" || "${parts[1]}" == "libpcp3" ]]; then
            echo "src/libpcp/src"
        elif [[ "${parts[1]}" == "libpcp_web" ]]; then
            echo "src/libpcp_web/src"
        elif [[ "${parts[1]}" == "libpcp_pmda" ]]; then
            echo "src/libpcp_pmda/src"
        elif [[ "${parts[1]}" == "libpcp_archive" ]]; then
            echo "src/libpcp_archive/src"
        elif [[ "${parts[1]}" == "pmdas" ]]; then
            echo "src/${parts[1]}/${parts[2]}"
        elif [[ "${parts[1]}" == "pmcd" || "${parts[1]}" == "pmproxy" || "${parts[1]}" == "pmfind" ]]; then
            if [ -d "src/${parts[1]}/src" ]; then
                echo "src/${parts[1]}/src"
            else
                echo "src/${parts[1]}"
            fi
        else
            dirname "$f"
        fi
    done | sort -u)

    for d in $BUILD_DIRS; do
        if [ -d "$TESTBED/$d" ]; then
            echo "[eval] Building $d..."
            cd "$TESTBED/$d"
            make -j4 CHECK_STATICS= 2>&1 | tail -3
            make install CHECK_STATICS= 2>&1 | tail -3

            # Force-copy shared libraries
            find . -name '*.so.*' -newer GNUmakefile -exec cp -f {} /usr/lib64/ \; 2>/dev/null
            find . -name '*.so' -newer GNUmakefile -exec cp -f {} /usr/lib64/ \; 2>/dev/null

            # Force-copy PMDA binaries
            if [[ "$d" == *pmdas* ]]; then
                pmda_name=$(basename "$d")
                find . -name "pmda_*.so" -exec cp -f {} /usr/libexec/pcp/pmdas/$pmda_name/ \; 2>/dev/null
                find . -name "pmda${pmda_name}" -perm /111 -exec cp -f {} /usr/libexec/pcp/pmdas/$pmda_name/ \; 2>/dev/null
            fi
        fi
    done

    ldconfig
    /usr/libexec/pcp/lib/pmcd restart 2>/dev/null
    sleep 3
fi

# --- Step 2: Sync qa tests to testsuite ---
cd "$TESTBED"
rsync -a qa/ "$QA_DIR/" --exclude='.git'

# --- Step 3: Ensure pmcd is running ---
mkdir -p /run/pcp
if ! pminfo -f sample.milliseconds >/dev/null 2>&1; then
    /usr/libexec/pcp/lib/pmcd restart 2>/dev/null || /usr/libexec/pcp/lib/pmcd start 2>/dev/null
    sleep 3
    cd /var/lib/pcp/pmdas/sample && echo pipe | ./Install >/dev/null 2>&1
    cd /var/lib/pcp/pmdas/simple && echo pipe | ./Install >/dev/null 2>&1
fi

# --- Step 4: Run qa tests ---
cd "$QA_DIR"

export PCPQA_SYSTEMD=yes
export PCP_SYSTEMDUNIT_DIR=/usr/lib/systemd/system

PASS=0
FAIL=0
TOTAL=0

for tn in $TEST_NUMS; do
    TOTAL=$((TOTAL + 1))
    echo "[eval] Running qa/$tn..."
    output=$(timeout $TIMEOUT ./check $tn 2>&1)
    if echo "$output" | grep -q "Passed"; then
        echo "[eval] qa/$tn: PASSED"
        PASS=$((PASS + 1))
    else
        echo "[eval] qa/$tn: FAILED"
        echo "$output" | tail -10
        FAIL=$((FAIL + 1))
    fi
done

echo ""
echo "[eval] Results: $PASS passed, $FAIL failed, $TOTAL total"

# Exit 0 if all tests passed, 1 otherwise
if [ "$FAIL" -eq 0 ] && [ "$PASS" -gt 0 ]; then
    exit 0
else
    exit 1
fi
