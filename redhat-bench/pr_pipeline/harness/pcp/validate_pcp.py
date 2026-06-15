"""Validate PCP C-fix PRs with per-commit source builds."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_DIR = "/workspace/pcp"
QA_DIR = "/var/lib/pcp/testsuite"
RESULTS_DIR = Path("/workspace/results")
TIMEOUT = 180


def extract_qa_tests(test_patch):
    tests = set()
    for line in test_patch.split("\n"):
        if line.startswith("diff --git") and " b/" in line:
            path = line.split(" b/")[1].strip()
            if path.startswith("qa/"):
                base = os.path.basename(path).split(".")[0]
                if base.isdigit():
                    tests.add(base)
    return sorted(tests)


def extract_build_dirs(patch):
    dirs = set()
    for line in patch.split("\n"):
        if line.startswith("diff --git") and " b/" in line:
            path = line.split(" b/")[1].strip()
            if path.endswith((".c", ".h")) and path.startswith("src/"):
                parts = path.split("/")
                if parts[1] in ("libpcp", "libpcp3"):
                    dirs.add("src/libpcp/src")
                elif parts[1] == "libpcp_web":
                    dirs.add("src/libpcp_web/src")
                elif parts[1] == "libpcp_pmda":
                    dirs.add("src/libpcp_pmda/src")
                elif parts[1] == "libpcp_archive":
                    dirs.add("src/libpcp_archive/src")
                elif parts[1] == "pmdas":
                    dirs.add(f"src/{parts[1]}/{parts[2]}")
                elif parts[1] in ("pmcd", "pmproxy", "pmfind"):
                    if len(parts) > 3 and parts[2] == "src":
                        dirs.add(f"src/{parts[1]}/src")
                    else:
                        dirs.add(f"src/{parts[1]}")
                else:
                    dirs.add(os.path.dirname(path))
    return sorted(dirs)


def run_cmd(cmd, timeout=120):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout + r.stderr


def full_build_install():
    """Full configure + make + make install."""
    print("    [build] configure...", flush=True)
    rc, out = run_cmd(f"cd {REPO_DIR} && ./configure --prefix=/usr --sysconfdir=/etc --localstatedir=/var 2>&1 | tail -3", 120)
    if rc != 0:
        print(f"    [build] configure FAILED", flush=True)
        return False

    print("    [build] make -j4...", flush=True)
    rc, out = run_cmd(f"cd {REPO_DIR} && make -j4 2>&1 | tail -5", 900)
    if rc != 0:
        print(f"    [build] make FAILED: {out[-200:]}", flush=True)
        return False

    print("    [build] make install...", flush=True)
    rc, out = run_cmd(f"cd {REPO_DIR} && make install 2>&1 | tail -3", 600)
    if rc != 0:
        print(f"    [build] install FAILED", flush=True)
        return False

    # Sync full qa directory to testsuite (includes subdirs like ceph, smart, etc.)
    run_cmd(f"rsync -a {REPO_DIR}/qa/ {QA_DIR}/ --exclude='.git'", 120)

    # Rebuild testsuite binaries
    run_cmd(f"cd {QA_DIR} && make -C src -j4 2>&1 | tail -3", 300)

    # Restart services
    subprocess.run("/usr/libexec/pcp/lib/pmcd restart", shell=True, capture_output=True, timeout=30)
    subprocess.run("/usr/libexec/pcp/lib/pmlogger restart", shell=True, capture_output=True, timeout=30)
    time.sleep(3)
    return True


def incremental_rebuild(dirs):
    """Rebuild only changed directories, then force-install artifacts."""
    for d in dirs:
        full = f"{REPO_DIR}/{d}"
        if not os.path.isdir(full):
            print(f"    [rebuild] dir not found: {d}", flush=True)
            continue

        # Force recompile changed files
        run_cmd(f"cd {REPO_DIR} && git diff --name-only | xargs -r touch", 10)
        time.sleep(1)

        rc, out = run_cmd(f"cd {full} && make -j4 CHECK_STATICS= 2>&1 | tail -5", 120)
        # Try make install, but also force-copy shared libs since check-statics may block it
        run_cmd(f"cd {full} && make install CHECK_STATICS= 2>&1 | tail -3", 60)

        # Force-copy any rebuilt shared libraries
        run_cmd(f"find {full} -name '*.so.*' -newer {full}/GNUmakefile -exec cp -f {{}} /usr/lib64/ \\;", 10)
        # Force-copy PMDA shared objects
        if "pmdas" in d:
            pmda_name = d.split("/")[-1]
            run_cmd(f"find {full} -name 'pmda_*.so' -exec cp -f {{}} /usr/libexec/pcp/pmdas/{pmda_name}/ \\;", 10)
            run_cmd(f"find {full} -name 'pmda{pmda_name}' -perm /111 -exec cp -f {{}} /usr/libexec/pcp/pmdas/{pmda_name}/ \\;", 10)

    subprocess.run("ldconfig", shell=True, capture_output=True, timeout=10)
    subprocess.run("/usr/libexec/pcp/lib/pmcd restart", shell=True, capture_output=True, timeout=30)
    time.sleep(3)
    return True


def apply_patch(patch_text, label):
    pf = f"/tmp/{label}.diff"
    if not patch_text.endswith("\n"):
        patch_text += "\n"
    with open(pf, "w") as f:
        f.write(patch_text)

    # Strip binary diffs that GitHub API can't provide full content for
    text_only = []
    skip = False
    for line in patch_text.split("\n"):
        if line.startswith("diff --git"):
            skip = False
            text_only.append(line)
        elif line.startswith("Binary files") or "GIT binary patch" in line:
            skip = True
            # Remove the diff header we just added
            while text_only and not text_only[-1].startswith("diff --git"):
                text_only.pop()
            if text_only:
                text_only.pop()
        elif not skip:
            text_only.append(line)

    pf_text = f"/tmp/{label}_text.diff"
    with open(pf_text, "w") as f:
        f.write("\n".join(text_only) + "\n")

    # Try full patch first, then text-only
    for df in [pf, pf_text]:
        for cmd in [f"git apply {df}", f"git apply --3way {df}"]:
            r = subprocess.run(f"cd {REPO_DIR} && {cmd}", shell=True, capture_output=True, text=True)
            if r.returncode == 0:
                return True

    # Last resort: try text-only patch ignoring whitespace
    r = subprocess.run(f"cd {REPO_DIR} && git apply --ignore-whitespace {pf_text}",
                       shell=True, capture_output=True, text=True)
    if r.returncode == 0:
        return True

    print(f"    [apply] all methods failed for {label}", flush=True)
    return False


def run_qa_test(test_num):
    try:
        env = os.environ.copy()
        env["PCPQA_SYSTEMD"] = "yes"
        env["PCP_SYSTEMDUNIT_DIR"] = "/usr/lib/systemd/system"
        r = subprocess.run(
            f"cd {QA_DIR} && timeout {TIMEOUT} ./check {test_num}",
            shell=True, capture_output=True, text=True, timeout=TIMEOUT + 10, env=env,
        )
        output = r.stdout + r.stderr
        if "Passed" in output:
            return "PASSED", output
        elif "not run" in output or "no such test" in output:
            return "SKIP", output
        else:
            return "FAILED", output
    except subprocess.TimeoutExpired:
        return "FAILED", "timeout"
    except Exception as e:
        return "FAILED", str(e)


def validate_instance(inst):
    pr = inst["pull_number"]
    bc = inst.get("base_commit", "")
    repo_key = "performancecopilot__pcp"
    iid = f"{repo_key}.pr_{pr}_v3"

    log_dir = RESULTS_DIR / repo_key / iid
    log_dir.mkdir(parents=True, exist_ok=True)
    rpt = log_dir / "report.json"

    if rpt.exists():
        print(f"  [SKIP] PR#{pr}: already done", flush=True)
        return None

    qa_tests = extract_qa_tests(inst.get("test_patch", ""))
    build_dirs = extract_build_dirs(inst.get("patch", ""))

    if not qa_tests:
        print(f"  [SKIP] PR#{pr}: no qa tests", flush=True)
        json.dump({"status": "no_qa_tests"}, open(rpt, "w"), indent=2)
        return None

    print(f"\n  PR#{pr}: tests={qa_tests} rebuild={build_dirs}", flush=True)

    # Checkout base_commit
    run_cmd(f"cd {REPO_DIR} && git checkout -- . && git clean -fd 2>/dev/null")
    rc, out = run_cmd(f"cd {REPO_DIR} && git checkout {bc} 2>&1")
    if rc != 0:
        run_cmd(f"cd {REPO_DIR} && git clean -fd 2>/dev/null")
        rc, out = run_cmd(f"cd {REPO_DIR} && git checkout {bc} 2>&1")
        if rc != 0:
            print(f"  [FAIL] PR#{pr}: checkout {bc[:8]} failed: {out[-200:]}", flush=True)
            json.dump({"status": "checkout_failed"}, open(rpt, "w"), indent=2)
            return None

    print(f"    checked out {bc[:8]}", flush=True)

    # Full build at base_commit
    if not full_build_install():
        print(f"  [FAIL] PR#{pr}: base build failed", flush=True)
        json.dump({"status": "base_build_failed"}, open(rpt, "w"), indent=2)
        return None

    # Apply test_patch
    if not apply_patch(inst["test_patch"], f"tp_{pr}"):
        print(f"  [FAIL] PR#{pr}: test_patch failed", flush=True)
        json.dump({"status": "test_patch_failed"}, open(rpt, "w"), indent=2)
        return None

    # Copy qa tests to testsuite
    for tn in qa_tests:
        for ext in ["", ".out", ".out.bad"]:
            src = f"{REPO_DIR}/qa/{tn}{ext}"
            dst = f"{QA_DIR}/{tn}{ext}"
            if os.path.exists(src):
                subprocess.run(f"cp {src} {dst}", shell=True)

    # Run tests (buggy state — should fail)
    buggy_results = {}
    for tn in qa_tests:
        status, output = run_qa_test(tn)
        buggy_results[f"qa/{tn}"] = status
        (log_dir / f"test_{tn}_buggy.txt").write_text(output)
        print(f"    buggy qa/{tn}: {status}", flush=True)

    # Apply fix patch
    if not apply_patch(inst["patch"], f"fix_{pr}"):
        print(f"  [FAIL] PR#{pr}: fix patch failed", flush=True)
        json.dump({"status": "fix_failed"}, open(rpt, "w"), indent=2)
        return None

    # Incremental rebuild of changed components
    if build_dirs:
        if not incremental_rebuild(build_dirs):
            print(f"  [FAIL] PR#{pr}: rebuild failed", flush=True)
            json.dump({"status": "rebuild_failed"}, open(rpt, "w"), indent=2)
            return None

    # Copy updated qa tests
    for tn in qa_tests:
        for ext in ["", ".out", ".out.bad"]:
            src = f"{REPO_DIR}/qa/{tn}{ext}"
            dst = f"{QA_DIR}/{tn}{ext}"
            if os.path.exists(src):
                subprocess.run(f"cp {src} {dst}", shell=True)

    # Run tests (fixed state — should pass)
    clean_results = {}
    for tn in qa_tests:
        status, output = run_qa_test(tn)
        clean_results[f"qa/{tn}"] = status
        (log_dir / f"test_{tn}_clean.txt").write_text(output)
        print(f"    clean qa/{tn}: {status}", flush=True)

    # Build report
    report = {"FAIL_TO_PASS": [], "PASS_TO_PASS": [], "FAIL_TO_FAIL": [], "PASS_TO_FAIL": []}
    for t in buggy_results:
        if t not in clean_results:
            continue
        b, c = buggy_results[t], clean_results[t]
        if b == "SKIP" or c == "SKIP":
            continue
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
    (log_dir / "patch.diff").write_text(inst["patch"])

    f2p = len(report["FAIL_TO_PASS"])
    f2f = len(report["FAIL_TO_FAIL"])
    status = "VALID" if f2p > 0 else "NO-BUG"
    print(f"  [{status}] PR#{pr}: F2P={f2p} P2P={len(report['PASS_TO_PASS'])} F2F={f2f}", flush=True)
    return report


def main():
    f = sys.argv[1]
    instances = [json.loads(line) for line in open(f) if line.strip()]

    c_instances = []
    for inst in instances:
        patch = inst.get("patch", "")
        has_c = any(
            line.split(" b/")[1].strip().endswith((".c", ".h"))
            for line in patch.split("\n")
            if line.startswith("diff --git") and " b/" in line
        )
        if has_c:
            c_instances.append(inst)

    print(f"PCP C-fix validation: {len(c_instances)}/{len(instances)} PRs (per-commit builds)", flush=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    valid = 0
    for inst in c_instances:
        r = validate_instance(inst)
        if r and r.get("FAIL_TO_PASS"):
            valid += 1
    print(f"\nDone: {valid}/{len(c_instances)} valid", flush=True)


if __name__ == "__main__":
    main()
