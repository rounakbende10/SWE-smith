#!/bin/bash
# Quick validation of Harbor dataset generation

set -e

DATASET_DIR="${1:-/Users/rbende/Desktop/Code-mode/SWE-smith/redhat-bench/harbor-dataset}"

echo "Validating Harbor dataset at: $DATASET_DIR"
echo

# Count tasks
TASK_COUNT=$(find "$DATASET_DIR" -maxdepth 1 -type d -name "task-*" | wc -l | tr -d ' ')
echo "Total tasks: $TASK_COUNT"

# Check eval.yaml exists
if [ ! -f "$DATASET_DIR/eval.yaml" ]; then
    echo "ERROR: eval.yaml not found"
    exit 1
fi
echo "✓ eval.yaml exists"

# Sample a few tasks and check structure
for task_idx in 0 100 250 400 530; do
    task_dir="$DATASET_DIR/task-$(printf "%04d" $task_idx)"

    if [ ! -d "$task_dir" ]; then
        echo "WARNING: task-$(printf "%04d" $task_idx) not found (expected for task-0530 if <531 tasks)"
        continue
    fi

    echo
    echo "Checking task-$(printf "%04d" $task_idx)..."

    # Check required files
    for file in task.toml input.yaml instruction.md; do
        if [ ! -f "$task_dir/$file" ]; then
            echo "  ERROR: $file missing"
            exit 1
        fi
    done

    # Check directories
    for dir in environment solution tests; do
        if [ ! -d "$task_dir/$dir" ]; then
            echo "  ERROR: $dir/ missing"
            exit 1
        fi
    done

    # Check scripts
    for script in environment/Dockerfile solution/solve.sh tests/test.sh; do
        if [ ! -f "$task_dir/$script" ]; then
            echo "  ERROR: $script missing"
            exit 1
        fi
    done

    # Check solve.sh and test.sh are executable
    if [ ! -x "$task_dir/solution/solve.sh" ]; then
        echo "  ERROR: solve.sh not executable"
        exit 1
    fi

    if [ ! -x "$task_dir/tests/test.sh" ]; then
        echo "  ERROR: test.sh not executable"
        exit 1
    fi

    # Check solve.sh contains base64 patch
    if ! grep -q "base64 -d | patch -p1" "$task_dir/solution/solve.sh"; then
        echo "  ERROR: solve.sh doesn't contain expected patch command"
        exit 1
    fi

    # Check test.sh writes to reward.txt
    if ! grep -q "/logs/verifier/reward.txt" "$task_dir/tests/test.sh"; then
        echo "  ERROR: test.sh doesn't write to reward.txt"
        exit 1
    fi

    # Extract difficulty from task.toml
    difficulty=$(grep '^difficulty = ' "$task_dir/task.toml" | cut -d'"' -f2)
    echo "  ✓ Difficulty: $difficulty"

    # Extract language from Dockerfile
    if grep -q "FROM golang:" "$task_dir/environment/Dockerfile"; then
        lang="Go"
    elif grep -q "FROM python:" "$task_dir/environment/Dockerfile"; then
        lang="Python"
    else
        lang="Unknown"
    fi
    echo "  ✓ Language: $lang"

    # Check if synthetic or PR-based
    if grep -q "Apply setup patch to introduce the bug" "$task_dir/environment/Dockerfile"; then
        type="Synthetic"
    elif grep -q "Apply test patch to add test files" "$task_dir/environment/Dockerfile"; then
        type="PR-based"
    else
        type="Unknown"
    fi
    echo "  ✓ Type: $type"
done

echo
echo "================================================"
echo "Validation complete!"
echo "Dataset appears well-formed with $TASK_COUNT tasks"
echo "================================================"
