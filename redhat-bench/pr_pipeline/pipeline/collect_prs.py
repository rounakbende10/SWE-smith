"""
Step 1: Collect merged PRs that modify both code and test files.

Usage:
    python collect_prs.py repos.json output.jsonl [--max-pulls 500]

Input (repos.json):
    [
        {"repo": "prometheus/prometheus", "lang": "go"},
        {"repo": "ansible/awx", "lang": "python"},
        {"repo": "keycloak/keycloak", "lang": "java"}
    ]

Output (output.jsonl):
    One JSON object per line with: repo, pull_number, instance_id, base_commit, patch, test_patch, problem_statement

Requires:
    pip install requests unidiff
    GITHUB_TOKEN env var (use `gh auth token` or a personal access token)
"""
import argparse
import json
import os
import sys
import time

import requests
from unidiff import PatchSet

TEST_PATTERNS = {
    "go": ["_test.go"],
    "python": ["test_", "tests/", "_test.py"],
    "java": ["Test.java", "test/", "Tests.java"],
}


def check_rate_limit(headers):
    r = requests.get("https://api.github.com/rate_limit", headers=headers)
    if r.status_code != 200:
        return 999
    remaining = r.json()["resources"]["core"]["remaining"]
    reset = r.json()["resources"]["core"]["reset"]
    if remaining < 10:
        wait = max(reset - time.time(), 0) + 5
        print(f"  Rate limit low ({remaining}), waiting {wait:.0f}s...", flush=True)
        time.sleep(wait)
    return remaining


def get_prs_with_tests(owner, repo, token, max_pulls=500, lang="go", max_instances=50):
    headers = {"Authorization": f"token {token}"}
    patterns = TEST_PATTERNS.get(lang, ["test"])

    instances = []
    page = 1
    total_checked = 0

    while total_checked < max_pulls:
        check_rate_limit(headers)

        r = requests.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls",
            headers=headers,
            params={"state": "closed", "sort": "updated", "direction": "desc",
                    "per_page": 100, "page": page},
        )
        if r.status_code == 403:
            print("  Rate limited, waiting 60s...", flush=True)
            time.sleep(60)
            continue
        if r.status_code != 200:
            print(f"  API error: {r.status_code}", flush=True)
            break

        pulls = r.json()
        if not pulls:
            break

        merged = [p for p in pulls if p.get("merged_at")]
        print(f"  Page {page}: {len(merged)}/{len(pulls)} merged", flush=True)
        total_checked += len(pulls)

        for pr in merged:
            files_r = requests.get(
                f"{pr['url']}/files", headers=headers, params={"per_page": 100}
            )
            if files_r.status_code != 200:
                continue

            files = files_r.json()
            has_test = any(any(p in f["filename"] for p in patterns) for f in files)
            has_code = any(not any(p in f["filename"] for p in patterns) for f in files)

            if not has_test or not has_code:
                continue

            diff_r = requests.get(
                pr["url"],
                headers={**headers, "Accept": "application/vnd.github.v3.diff"},
            )
            if diff_r.status_code != 200:
                continue

            try:
                patch = PatchSet(diff_r.text)
            except Exception:
                continue

            test_files = []
            code_files = []
            for f in patch:
                if any(p in f.path for p in patterns):
                    test_files.append(f)
                else:
                    code_files.append(f)

            if not test_files or not code_files:
                continue

            instances.append({
                "repo": f"{owner}/{repo}",
                "pull_number": pr["number"],
                "instance_id": f"{owner}__{repo}-{pr['number']}",
                "base_commit": pr["base"]["sha"],
                "patch": "\n".join(str(f) for f in code_files),
                "test_patch": "\n".join(str(f) for f in test_files),
                "problem_statement": (pr.get("title", "") + "\n\n" + (pr.get("body", "") or "")),
            })

            print(f"  Found PR#{pr['number']}: {len(code_files)} code + {len(test_files)} test files", flush=True)

            if len(instances) >= max_instances:
                break

        if len(instances) >= max_instances:
            break

        page += 1
        time.sleep(0.5)

    return instances


def main():
    parser = argparse.ArgumentParser(description="Collect PRs with test changes")
    parser.add_argument("repos", help="JSON file with repo list")
    parser.add_argument("output", help="Output JSONL file")
    parser.add_argument("--max-pulls", type=int, default=500, help="Max PRs to check per repo")
    parser.add_argument("--max-instances", type=int, default=50, help="Max instances per repo")
    args = parser.parse_args()

    repos = json.load(open(args.repos))
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("ERROR: Set GITHUB_TOKEN env var (try: export GITHUB_TOKEN=$(gh auth token))")
        sys.exit(1)

    all_instances = []
    for entry in repos:
        owner, repo = entry["repo"].split("/")
        lang = entry.get("lang", "go")
        print(f"\n=== {owner}/{repo} ({lang}) ===", flush=True)
        instances = get_prs_with_tests(owner, repo, token, args.max_pulls, lang, args.max_instances)
        print(f"  Total: {len(instances)} PRs with test changes", flush=True)
        all_instances.extend(instances)
        with open(args.output, "w") as f:
            for inst in all_instances:
                f.write(json.dumps(inst) + "\n")

    print(f"\nTotal: {len(all_instances)} PRs saved to {args.output}", flush=True)


if __name__ == "__main__":
    main()
