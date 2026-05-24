"""
Step 3: Generate issue text and difficulty ratings for validated instances.

Reads valid reports from validate_prs.py output, generates problem_statement
using an LLM, and rates difficulty.

Usage:
    python generate_issues.py ./v3_results --output instances.json [--model gpt-5-mini]

Requires:
    pip install openai
    OPENAI_API_KEY env var
"""
import argparse
import glob
import json
import os
import sys
import time

from openai import OpenAI

ISSUE_PROMPT = """You are a software engineer helping to create a realistic dataset of synthetic GitHub issues.
You will be given a patch that introduces a bug and test output.
Output: A realistic GitHub issue. DO NOT explain the fix, do not mention tests.
<IMPORTANT>
- DO NOT GIVE AWAY THE FIX!
- DO NOT SAY THAT EXISTING TEST(s) FAILED.
- DO NOT SUGGEST RUNNING ANY TESTING COMMANDS.
</IMPORTANT>
<patch>
{patch}
</patch>
<test_output>
{test_output}
</test_output>
**Issue Text**
<START WRITING>"""

DIFFICULTY_PROMPT = """Rate the difficulty of fixing this bug. Choose one:
- "<15 min fix"
- "15 min - 1 hour"
- "1-4 hours"
- ">4 hours"
Patch:
{patch}
Reply with ONLY one of the four difficulty levels."""

DIFFICULTY_LEVELS = ["<15 min fix", "15 min - 1 hour", "1-4 hours", ">4 hours"]


def generate_issue(client, model, patch, test_output):
    try:
        r = client.responses.create(
            model=model,
            input=ISSUE_PROMPT.format(patch=patch[:4000], test_output=test_output[:3000]),
            max_output_tokens=500,
        )
        return r.output_text.strip()
    except Exception as e:
        print(f"  Issue gen error: {e}", flush=True)
        return ""


def rate_difficulty(client, model, patch):
    try:
        r = client.responses.create(
            model=model,
            input=DIFFICULTY_PROMPT.format(patch=patch[:4000]),
            max_output_tokens=50,
        )
        text = r.output_text.strip()
        for level in DIFFICULTY_LEVELS:
            if level in text:
                return level
        return "<15 min fix"
    except Exception as e:
        print(f"  Rating error: {e}", flush=True)
        return "<15 min fix"


def collect_valid_instances(results_dir):
    instances = []
    for rpt_path in glob.glob(os.path.join(results_dir, "*", "*", "report.json")):
        d = json.load(open(rpt_path))
        f2p = d.get("FAIL_TO_PASS", [])
        p2p = d.get("PASS_TO_PASS", [])
        if not f2p or not p2p:
            continue

        log_dir = os.path.dirname(rpt_path)
        patch_path = os.path.join(log_dir, "patch.diff")
        test_out_path = os.path.join(log_dir, "test_output.txt")

        inst = {
            "instance_id": os.path.basename(log_dir),
            "repo": d.get("_repo", ""),
            "base_commit": d.get("_base_commit", ""),
            "patch": open(patch_path).read() if os.path.exists(patch_path) else "",
            "test_patch": d.get("_test_patch", ""),
            "FAIL_TO_PASS": f2p,
            "PASS_TO_PASS": p2p,
            "_test_output": open(test_out_path).read()[-3000:] if os.path.exists(test_out_path) else "",
        }
        instances.append(inst)

    return instances


def main():
    parser = argparse.ArgumentParser(description="Generate issues and difficulty ratings")
    parser.add_argument("results_dir", help="Directory with validate_prs.py output")
    parser.add_argument("--output", default="instances_rated.json", help="Output JSON file")
    parser.add_argument("--model", default="gpt-5-mini", help="OpenAI model for generation")
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        print("ERROR: Set OPENAI_API_KEY env var")
        sys.exit(1)

    client = OpenAI(api_key=api_key)
    instances = collect_valid_instances(args.results_dir)
    print(f"Found {len(instances)} valid instances", flush=True)

    for i, inst in enumerate(instances):
        inst["problem_statement"] = generate_issue(
            client, args.model, inst["patch"], inst.pop("_test_output", "")
        )
        inst["difficulty"] = rate_difficulty(client, args.model, inst["patch"])
        d = inst["difficulty"]
        print(f"  [{i + 1}/{len(instances)}] {inst['instance_id']}: {d}", flush=True)
        time.sleep(0.2)

    with open(args.output, "w") as f:
        json.dump(instances, f, indent=2)

    diffs = {}
    for inst in instances:
        d = inst.get("difficulty", "unknown")
        diffs[d] = diffs.get(d, 0) + 1
    print(f"\nSaved {len(instances)} instances to {args.output}", flush=True)
    print("Difficulty distribution:", flush=True)
    for d, c in sorted(diffs.items()):
        print(f"  {d}: {c}", flush=True)


if __name__ == "__main__":
    main()
