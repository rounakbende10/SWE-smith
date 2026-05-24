#!/usr/bin/env python3
"""
Verify RH SWE-bench instances using Claude as annotator.
Based on OpenAI's SWE-bench Verified rubric.

Sends each instance to Claude with the problem statement, patch, and test names.
Claude grades on:
  1. Problem statement specification (0-3)
  2. Test scope validity (0-3)
  3. Difficulty (<15 min, 15 min-1 hour, 1-4 hours, >4 hours)
  4. Other issues (0/1)

Instances with severity 2+ on criteria 1 or 2 are filtered out.

Usage:
    python3 verify_dataset.py dataset.json --output verified.json --limit 100
    python3 verify_dataset.py dataset.json --output verified.json --workers 4
"""

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import litellm
from litellm import completion
from tqdm import tqdm


GRADING_PROMPT = """You are a strict, conservative reviewer screening benchmark instances for a coding evaluation dataset. Your job is to REJECT any instance that has quality issues. When in doubt, REJECT — it is better to have a smaller, high-quality dataset than to include questionable instances.

An agent will receive ONLY the problem statement below (no access to the patch, tests, or any other information) and must find and fix the bug in the codebase. The agent's fix is then verified by running FAIL_TO_PASS tests (which should start passing) and PASS_TO_PASS tests (which should continue passing).

## Instance Details

**Repository:** {repo}
**Language:** {language}
**Bug Type:** {bug_type}

### Problem Statement (ONLY thing the agent sees):
{problem_statement}

### Gold Patch (the fix — agent does NOT see this):
```
{patch}
```

### Test Code (used to verify the fix — agent does NOT see this):
```
{test_patch}
```

### FAIL_TO_PASS test names:
{fail_to_pass}

## Section 1: Problem Statement Specification

Imagine you are an experienced engineer who must fix this issue using ONLY the problem statement above. You have access to the full codebase, but you CANNOT ask for clarification and you CANNOT see the tests or gold patch.

BE STRICT. Common problems to watch for:
- The problem statement describes symptoms but not what the correct behavior should be
- The problem statement is ambiguous about WHAT needs to change
- The problem statement references external context (other PRs, issues, discussions) without self-containedly explaining the problem
- The problem statement could be interpreted in multiple incompatible ways
- The problem statement says "fix X" but there are multiple valid ways to fix X that would produce different behaviors
- The problem statement uses vague language like "should work", "handle correctly", "improve" without defining what that means concretely

Rate 0-3:
- 0: Well-specified. Clear what the problem is AND what a correct solution looks like. Includes a reproducible example or clear expected vs actual behavior.
- 1: Some blanks to fill in, but there is ONE sensible interpretation of the solution. Minor details left to the engineer.
- 2: VAGUE. Multiple reasonable interpretations exist. The problem statement lacks concrete expected behavior, references external context without explanation, or describes a goal without specifying acceptance criteria. An engineer could write a valid fix that doesn't match what the tests expect. REJECT at this level.
- 3: Almost impossible to understand the problem without reading external PRs, linked issues, or prior discussions. The problem statement alone is insufficient. REJECT.

## Section 2: Test Scope (False Negatives)

CAREFULLY read the test code above. Check if the tests could UNFAIRLY REJECT a valid solution.

BE STRICT. Common problems:
- Tests check for a specific error message string that the agent couldn't know
- Tests check for a specific function/variable name introduced in the gold patch
- Tests check implementation details rather than behavior
- Tests are unrelated to what the problem statement describes
- Tests require a specific approach when multiple valid approaches exist

Rate 0-3:
- 0: Tests check BEHAVIOR described in the issue. Any correct fix would pass.
- 1: Tests mostly check behavior, but some unusual (but valid) solutions might fail.
- 2: Tests rely on IMPLEMENTATION DETAILS not mentioned in the problem statement. A reasonable alternative fix could fail these tests. REJECT at this level.
- 3: Tests are checking for something DIFFERENT from what the issue describes, or are too narrow/broad. REJECT.

## Section 3: Difficulty

How long for an experienced engineer (familiar with the codebase) to fix this?
- <15 min: trivial change (e.g., adding an assertion)
- 15 min - 1 hour: small change requiring thought
- 1-4 hours: substantially rewriting a function or editing multiple files
- >4 hours: very esoteric, requires substantial research, >100 lines changed

## Section 4: Other Issues

Flag ONLY for serious structural problems, such as:
- The FAIL_TO_PASS tests are trivially gameable (e.g., just deleting an assertion makes them pass)
- The instance has a fundamental setup issue that would prevent any solution from working

Do NOT flag for:
- Problem statements that mention file paths or function names — real GitHub issues commonly include this context, and it does NOT disqualify the instance
- Problem statements that hint at the solution approach — this is normal for bug reports
- Easy difficulty — easy instances are still valid for benchmarking

## Response Format
Respond in EXACTLY this JSON format, nothing else:
{{
    "specification_score": <0-3>,
    "specification_reason": "<explain what's unclear or ambiguous, referencing specific parts of the problem statement>",
    "test_scope_score": <0-3>,
    "test_scope_reason": "<explain what implementation details the tests rely on, referencing specific test assertions>",
    "difficulty": "<15 min|15 min - 1 hour|1-4 hours|>4 hours>",
    "difficulty_reason": "<brief explanation>",
    "other_issues": <0 or 1>,
    "other_issues_reason": "<explanation or empty>",
    "verdict": "<PASS or FAIL>",
    "verdict_reason": "<one-line summary>"
}}

Set verdict to FAIL if specification_score >= 2 OR test_scope_score >= 2 OR other_issues == 1.
Otherwise set verdict to PASS.

REMEMBER: Be conservative. When in doubt, REJECT."""


def grade_instance(instance: dict, model: str) -> dict:
    f2p = instance.get("FAIL_TO_PASS", [])
    p2p = instance.get("PASS_TO_PASS", [])
    if isinstance(f2p, str):
        f2p = json.loads(f2p)
    if isinstance(p2p, str):
        p2p = json.loads(p2p)

    test_patch = instance.get("test_patch", "")
    if not test_patch:
        test_patch = "(No separate test patch — tests pre-exist in the codebase. Test names listed below.)"

    prompt = GRADING_PROMPT.format(
        repo=instance.get("repo", ""),
        language=instance.get("repo_language", "unknown"),
        bug_type=instance.get("bug_type", "unknown"),
        problem_statement=instance.get("problem_statement", ""),
        patch=instance.get("patch", ""),
        test_patch=test_patch,
        fail_to_pass="\n".join(f2p),
    )

    try:
        response = completion(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=2000,
        )
        content = response.choices[0].message.content.strip()
        start = content.find("{")
        end = content.rfind("}")
        if start >= 0 and end > start:
            grading = json.loads(content[start:end+1])
        else:
            grading = json.loads(content)
        grading["instance_id"] = instance["instance_id"]
        grading["_cost"] = getattr(response, "_hidden_params", {}).get("response_cost", 0)
        return grading
    except Exception as e:
        return {
            "instance_id": instance["instance_id"],
            "error": str(e),
            "verdict": "ERROR",
        }


def main():
    parser = argparse.ArgumentParser(description="Verify RH SWE-bench instances with Claude")
    parser.add_argument("dataset", help="Path to dataset JSON")
    parser.add_argument("--output", default="verified_results.json", help="Output path")
    parser.add_argument("--model", default="<YOUR_MODEL>", help="LiteLLM model")
    parser.add_argument("--limit", type=int, default=None, help="Max instances to grade")
    parser.add_argument("--workers", type=int, default=4, help="Parallel workers")
    parser.add_argument("--resume", action="store_true", help="Resume from existing output")
    args = parser.parse_args()

    with open(args.dataset) as f:
        instances = json.load(f)

    instances = [i for i in instances if i.get("problem_statement") and i.get("FAIL_TO_PASS")]
    print(f"Loaded {len(instances)} instances with problem statements")

    if args.limit:
        instances = instances[:args.limit]
        print(f"Limited to {args.limit}")

    already_done = set()
    results = []
    if args.resume and os.path.exists(args.output):
        with open(args.output) as f:
            results = json.load(f)
        already_done = {r["instance_id"] for r in results}
        print(f"Resuming: {len(already_done)} already graded")

    todo = [i for i in instances if i["instance_id"] not in already_done]
    print(f"Grading {len(todo)} instances with {args.model}")

    pass_count = 0
    fail_count = 0
    error_count = 0

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(grade_instance, inst, args.model): inst for inst in todo}

        with tqdm(total=len(todo), desc="Grading") as pbar:
            for future in as_completed(futures):
                result = future.result()
                results.append(result)

                verdict = result.get("verdict", "ERROR")
                if verdict == "PASS":
                    pass_count += 1
                elif verdict == "FAIL":
                    fail_count += 1
                else:
                    error_count += 1

                pbar.set_postfix(
                    {"pass": pass_count, "fail": fail_count, "err": error_count},
                    refresh=True,
                )
                pbar.update(1)

                if len(results) % 50 == 0:
                    with open(args.output, "w") as f:
                        json.dump(results, f, indent=2)

    with open(args.output, "w") as f:
        json.dump(results, f, indent=2)

    print(f"\n=== RESULTS ===")
    print(f"Total: {len(results)}")
    print(f"PASS: {pass_count}")
    print(f"FAIL: {fail_count}")
    print(f"ERROR: {error_count}")
    print(f"Pass rate: {pass_count / max(1, pass_count + fail_count):.1%}")

    verified = [r for r in results if r.get("verdict") == "PASS"]
    verified_ids = {r["instance_id"] for r in verified}
    verified_instances = [i for i in instances if i["instance_id"] in verified_ids]

    verified_output = args.output.replace(".json", "_dataset.json")
    with open(verified_output, "w") as f:
        json.dump(verified_instances, f, indent=2)
    print(f"Verified dataset: {len(verified_instances)} instances -> {verified_output}")


if __name__ == "__main__":
    main()
