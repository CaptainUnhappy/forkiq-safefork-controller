"""Trusted pre-push guard: pin both OIDs without permitting force updates."""
import json
import sys


def matches(expected, lines):
    for line in lines:
        fields = line.split()
        if len(fields) != 4:
            return False
        _, new, ref, old = fields
        if expected.get(ref) != [old, new] or new == "0" * 40:
            return False
    return True


if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8") as handle:
        expected = json.load(handle)
    sys.exit(0 if matches(expected, sys.stdin) else 1)
