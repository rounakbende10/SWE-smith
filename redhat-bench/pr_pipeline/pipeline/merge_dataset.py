"""
Step 4: Merge validated instances into the dataset.

Usage:
    python merge_dataset.py instances_rated.json --dataset path/to/dataset.json

Deduplicates by instance_id, prints before/after stats.
"""
import argparse
import json
import sys


def main():
    parser = argparse.ArgumentParser(description="Merge instances into dataset")
    parser.add_argument("input", help="JSON file from generate_issues.py")
    parser.add_argument("--dataset", required=True, help="Path to existing dataset JSON")
    parser.add_argument("--dry-run", action="store_true", help="Print stats without writing")
    args = parser.parse_args()

    dataset = json.load(open(args.dataset))
    new_instances = json.load(open(args.input))

    existing_ids = {inst["instance_id"] for inst in dataset}
    print(f"Existing dataset: {len(dataset)} instances")
    print(f"New instances: {len(new_instances)}")

    added = 0
    skipped = 0
    for inst in new_instances:
        iid = inst["instance_id"]
        if iid in existing_ids:
            skipped += 1
            continue

        entry = {
            "instance_id": iid,
            "repo": inst["repo"],
            "base_commit": inst.get("base_commit", ""),
            "patch": inst["patch"],
            "test_patch": inst.get("test_patch", ""),
            "problem_statement": inst.get("problem_statement", ""),
            "FAIL_TO_PASS": inst["FAIL_TO_PASS"] if isinstance(inst["FAIL_TO_PASS"], str) else json.dumps(inst["FAIL_TO_PASS"]),
            "PASS_TO_PASS": inst["PASS_TO_PASS"] if isinstance(inst["PASS_TO_PASS"], str) else json.dumps(inst["PASS_TO_PASS"]),
            "difficulty": inst.get("difficulty", ""),
        }
        dataset.append(entry)
        existing_ids.add(iid)
        added += 1

    print(f"Added: {added}, Skipped (duplicate): {skipped}")
    print(f"Final dataset: {len(dataset)} instances")

    repos = {}
    for inst in dataset:
        r = inst.get("repo", "unknown")
        repos[r] = repos.get(r, 0) + 1
    print("\nRepo distribution:")
    for r, c in sorted(repos.items(), key=lambda x: -x[1]):
        print(f"  {r}: {c}")

    if args.dry_run:
        print("\n(Dry run — not saving)")
    else:
        with open(args.dataset, "w") as f:
            json.dump(dataset, f, indent=2)
        print(f"\nSaved to {args.dataset}")


if __name__ == "__main__":
    main()
