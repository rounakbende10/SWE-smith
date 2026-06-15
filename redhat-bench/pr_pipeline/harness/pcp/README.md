# PCP Harness for SWE-bench Evaluation

Evaluates coding agents on PCP (Performance Co-Pilot) C-fix instances.

## Architecture

```
Agent sees:  problem_statement (bug description)
Agent edits: /testbed/src/**/*.c, /testbed/src/**/*.h

Harness:
  1. setup_pcp.sh  → checkout base_commit, build PCP, apply test_patch
  2. Agent works   → modifies C source files in /testbed
  3. eval_pcp.sh   → incremental rebuild, install, run qa tests
```

## Build

```bash
docker build -t pcp-harness:latest .
```

## Run an instance

```bash
# Start container (privileged needed for pmcd)
docker run --privileged -d --name pcp-eval pcp-harness:latest tail -f /dev/null

# Copy test_patch into container
docker cp test_patch.diff pcp-eval:/test_patch.diff

# Setup: checkout commit, build, apply test_patch
docker exec pcp-eval /harness/setup_pcp.sh <base_commit> /test_patch.diff

# Agent applies fix (e.g., copy patched file)
docker cp fixed_file.c pcp-eval:/testbed/src/pmdas/smart/smart_stats.c

# Evaluate
docker exec pcp-eval /harness/eval_pcp.sh 1397
```

## Instance configs

See `instances.json` for per-instance configs with:
- `base_commit` — git SHA to checkout
- `test_nums` — qa test numbers to run
- `setup_cmd` / `eval_cmd` — exact commands for harness

## Key constraints

- **`--network host`** required — pmcd binds TCP port 44321, qa tests use `pminfo -h <hostname>`. No `--privileged` needed.
- **Full source build** per commit (~10-15 min on 4 CPU, ~30 min on emulated x86)
- **Incremental rebuild** after agent fix (~30s for single PMDA)
- **No Qt/GUI** — configure with `--disable-qt` to skip pmchart/pmview (saves ~10 min compile)
- qa tests compare tool output against `.out` files
- Tests need `PCPQA_SYSTEMD=yes` environment variable
- PMDAs (sample, simple) must be installed before running tests
- Tested on NERC OpenShift privileged pods (wrk-2/wrk-3 nodes)

## Files

- `Dockerfile` — base image with all PCP build deps + full repo clone
- `setup_pcp.sh` — per-instance setup (checkout, build, test_patch)
- `eval_pcp.sh` — evaluation (rebuild changed files, run qa tests)
- `instances.json` — per-instance harness configs
