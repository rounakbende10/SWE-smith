"""
Step 2: Validate collected PRs as SWE-bench instances.

Builds a Docker image at each PR's base_commit, applies test_patch (new tests),
runs tests (should fail = bug present), applies patch (the fix), runs tests again
(should pass). Instances with FAIL_TO_PASS > 0 and PASS_TO_PASS > 0 are valid.

Usage:
    python validate_prs.py input.jsonl --lang go [--output-dir ./results] [--timeout 600]

Requires:
    pip install docker unidiff
    pip install swesmith  (for exec_run_with_timeout and constants)
    Docker daemon running (local or DOCKER_HOST for DinD)
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import docker
from swesmith.constants import TEST_OUTPUT_START, TEST_OUTPUT_END
from swesmith.harness.utils import exec_run_with_timeout

try:
    from unidiff import PatchSet
except ImportError:
    PatchSet = None

# ── Language-specific config ──

LANG_CONFIG = {
    "go": {
        "base_image": "golang:1.22",
        "install_cmd": "go mod download || true",
        "test_cmd": "go test -v -short -count=1",
    },
    "python": {
        "base_image": "python:3.12-bookworm",
        "install_cmd": 'pip install -e ".[test,dev]" 2>/dev/null || pip install -e ".[test]" 2>/dev/null || pip install -e . 2>/dev/null || true',
        "test_cmd": "pytest --disable-warnings --color=no --tb=no --verbose",
    },
    "java": {
        "base_image": "maven:3.9.6-eclipse-temurin-17",
        "install_cmd": "mvn dependency:resolve -B -q 2>/dev/null || true",
        "test_cmd": "mvn test -B -Dsurefire.useFile=false -Dsurefire.printSummary=true",
    },
}

# ── Test output parsers ──

def parse_go_log(log):
    sm = {}
    for line in log.split("\n"):
        m = re.match(r"^--- (PASS|FAIL): (\S+)", line)
        if m:
            sm[m.group(2)] = "PASSED" if m.group(1) == "PASS" else "FAILED"
    return sm


def parse_pytest_log(log):
    sm = {}
    for line in log.split("\n"):
        m = re.match(r"^(\S+::\S+)\s+(PASSED|FAILED|ERROR)", line.strip())
        if m:
            sm[m.group(1)] = "PASSED" if m.group(2) == "PASSED" else "FAILED"
    return sm


def parse_maven_log(log):
    sm = {}
    for line in log.split("\n"):
        m = re.match(r"Tests run: (\d+), Failures: (\d+), Errors: (\d+)", line)
        if m:
            total, failures, errors = int(m.group(1)), int(m.group(2)), int(m.group(3))
            sm["maven_tests"] = "FAILED" if (failures > 0 or errors > 0) else "PASSED"
    return sm


PARSERS = {"go": parse_go_log, "python": parse_pytest_log, "java": parse_maven_log}

# ── Test package extraction (Go only) ──

def get_go_test_packages(test_patch, patch, base_cmd):
    if not PatchSet:
        return f"{base_cmd} ./..."
    extra = set()
    for p in [test_patch, patch]:
        try:
            for f in PatchSet(p):
                pkg = os.path.dirname(f.path)
                if pkg:
                    extra.add(f"./{pkg}/...")
        except Exception:
            pass
    return f"{base_cmd} {' '.join(sorted(extra))}" if extra else f"{base_cmd} ./..."


# ── Core logic ──

def build_image(owner, repo, commit, lang):
    cfg = LANG_CONFIG[lang]
    tag = f"v3-{owner}-{repo}:{commit[:8]}".lower()
    client = docker.from_env(timeout=300)
    try:
        client.images.get(tag)
        return tag
    except Exception:
        pass

    dockerfile = (
        f"FROM {cfg['base_image']}\n"
        f"RUN apt-get update -qq && apt-get install -y -qq git > /dev/null 2>&1 || true\n"
        f"RUN git clone https://github.com/{owner}/{repo} /testbed\n"
        f"WORKDIR /testbed\n"
        f"RUN git checkout {commit}\n"
        f"RUN {cfg['install_cmd']}\n"
    )
    df_path = f"/tmp/Dockerfile_v3_{commit[:8]}"
    with open(df_path, "w") as f:
        f.write(dockerfile)

    build_timeout = 600 if lang == "java" else 300
    try:
        r = subprocess.run(
            f"docker build -f {df_path} --platform linux/x86_64 --no-cache -t {tag} .",
            shell=True, capture_output=True, text=True, cwd="/tmp", timeout=build_timeout,
        )
    except subprocess.TimeoutExpired:
        print(f"    BUILD TIMEOUT: {owner}/{repo} at {commit[:8]}")
        return None

    if r.returncode == 0:
        print(f"    Built {owner}/{repo} at {commit[:8]}")
        return tag
    print(f"    BUILD FAILED: {r.stderr[-200:]}")
    return None


def validate_instance(inst, lang, output_dir, timeout=600):
    owner, repo_name = inst["repo"].split("/")
    pr = inst.get("pull_number", inst.get("instance_id", "unknown").split("-")[-1])
    bc = inst.get("base_commit", "")
    repo_key = f"{owner}__{repo_name}"
    iid = f"{repo_key}.pr_{pr}_v3"

    log_dir = Path(output_dir) / repo_key / iid
    log_dir.mkdir(parents=True, exist_ok=True)
    rpt = log_dir / "report.json"

    if rpt.exists():
        print(f"  [SKIP] PR#{pr}")
        return None

    if not bc:
        json.dump({"status": "no_base_commit"}, open(rpt, "w"), indent=2)
        return None

    tag = build_image(owner, repo_name, bc, lang)
    if not tag:
        json.dump({"status": "image_build_failed"}, open(rpt, "w"), indent=2)
        return None

    cfg = LANG_CONFIG[lang]
    if lang == "go":
        test_cmd = get_go_test_packages(inst["test_patch"], inst["patch"], cfg["test_cmd"])
    else:
        test_cmd = cfg["test_cmd"]

    client = docker.from_env(timeout=300)
    container = None
    tmp_files = []

    try:
        container = client.containers.create(
            image=tag, name=f"v3.{iid}", detach=True,
            command="tail -f /dev/null", platform="linux/x86_64", mem_limit="10g",
        )
        container.start()

        tp_f = f"/tmp/tp_{pr}.diff"
        fix_f = f"/tmp/fix_{pr}.diff"
        ev_f = f"/tmp/eval_{pr}.sh"
        tmp_files = [tp_f, fix_f, ev_f]

        with open(tp_f, "w") as f:
            f.write(inst["test_patch"])
        with open(fix_f, "w") as f:
            f.write(inst["patch"])
        subprocess.run(f"docker cp {tp_f} {container.id}:/test_patch.diff", shell=True)
        subprocess.run(f"docker cp {fix_f} {container.id}:/fix_patch.diff", shell=True)

        # Apply test_patch
        applied = False
        for cmd in ["git apply /test_patch.diff", "git apply --reject /test_patch.diff",
                     "patch --batch --fuzz=5 -p1 -i /test_patch.diff"]:
            if container.exec_run(cmd, workdir="/testbed").exit_code == 0:
                applied = True
                break
        if not applied:
            json.dump({"status": "test_patch_failed"}, open(rpt, "w"), indent=2)
            print(f"  [FAIL] PR#{pr}: test_patch failed")
            return None

        # Run tests (buggy state — should have failures)
        eval_script = f"#!/bin/bash\nset -uxo pipefail\ncd /testbed\n: '{TEST_OUTPUT_START}'\n{test_cmd}\n: '{TEST_OUTPUT_END}'\n"
        with open(ev_f, "w") as f:
            f.write(eval_script)
        subprocess.run(f"docker cp {ev_f} {container.id}:/eval.sh", shell=True)

        buggy_out, bto, _ = exec_run_with_timeout(container, "/bin/bash /eval.sh", timeout=timeout)
        (log_dir / "test_output.txt").write_text(buggy_out)
        if bto:
            json.dump({"timed_out": True, "timeout": timeout}, open(rpt, "w"), indent=2)
            print(f"  [FAIL] PR#{pr}: timed out")
            return None

        # Apply fix patch
        applied = False
        for cmd in ["git apply /fix_patch.diff", "git apply --reject /fix_patch.diff",
                     "patch --batch --fuzz=5 -p1 -i /fix_patch.diff"]:
            if container.exec_run(cmd, workdir="/testbed").exit_code == 0:
                applied = True
                break
        if not applied:
            json.dump({"status": "fix_failed"}, open(rpt, "w"), indent=2)
            print(f"  [FAIL] PR#{pr}: fix failed")
            return None

        # Run tests again (fixed state — should pass)
        clean_out, cto, _ = exec_run_with_timeout(container, "/bin/bash /eval.sh", timeout=timeout)
        (log_dir / "test_output_clean.txt").write_text(clean_out)
        (log_dir / "patch.diff").write_text(inst["patch"])

        # Compare results
        parser = PARSERS[lang]
        bsm = parser(buggy_out)
        csm = parser(clean_out) if not cto else {}

        report = {"FAIL_TO_PASS": [], "PASS_TO_PASS": [], "FAIL_TO_FAIL": [], "PASS_TO_FAIL": []}
        for t in bsm:
            if t not in csm:
                continue
            b, c = bsm[t], csm[t]
            if b == "FAILED" and c == "PASSED":
                report["FAIL_TO_PASS"].append(t)
            elif b == "PASSED" and c == "PASSED":
                report["PASS_TO_PASS"].append(t)
            elif b == "FAILED" and c == "FAILED":
                report["FAIL_TO_FAIL"].append(t)
            elif b == "PASSED" and c == "FAILED":
                report["PASS_TO_FAIL"].append(t)

        report["_test_patch"] = inst["test_patch"]
        report["_problem_statement"] = inst.get("problem_statement", "")
        report["_pull_number"] = pr
        report["_base_commit"] = bc
        report["_repo"] = inst["repo"]
        json.dump(report, open(rpt, "w"), indent=2)

        f2p = len(report["FAIL_TO_PASS"])
        p2p = len(report["PASS_TO_PASS"])
        status = "VALID" if f2p > 0 and p2p > 0 else "NO-BUG"
        print(f"  [{status}] {inst['repo']} PR#{pr}: F2P={f2p} P2P={p2p}")
        return report

    except Exception as e:
        print(f"  [ERROR] PR#{pr}: {e}")
        json.dump({"status": "error", "error": str(e)}, open(rpt, "w"), indent=2)
        return None

    finally:
        if container:
            try:
                container.remove(force=True)
            except Exception:
                pass
        if tag:
            try:
                subprocess.run(f"docker rmi {tag}", shell=True, capture_output=True)
            except Exception:
                pass
            subprocess.run("docker container prune -f && docker builder prune -f",
                           shell=True, capture_output=True)
        for f in tmp_files:
            try:
                os.unlink(f)
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description="Validate PRs as SWE-bench instances")
    parser.add_argument("input", help="JSONL file from collect_prs.py")
    parser.add_argument("--lang", choices=["go", "python", "java"], required=True)
    parser.add_argument("--output-dir", default="./v3_results", help="Output directory for reports")
    parser.add_argument("--timeout", type=int, default=600, help="Test timeout in seconds")
    args = parser.parse_args()

    if args.input.endswith(".jsonl"):
        instances = [json.loads(line) for line in open(args.input) if line.strip()]
    else:
        instances = json.load(open(args.input))

    print(f"v3 {args.lang}: {len(instances)} PRs", flush=True)

    valid = 0
    for inst in instances:
        r = validate_instance(inst, args.lang, args.output_dir, args.timeout)
        if r and r.get("FAIL_TO_PASS") and r.get("PASS_TO_PASS"):
            valid += 1

    print(f"\nDone: {valid}/{len(instances)} valid", flush=True)


if __name__ == "__main__":
    main()
