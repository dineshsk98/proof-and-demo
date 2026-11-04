# Runs every test file (tests/<area>/test_*.py), each in its own Python process from the repo root,
# and prints pass or fail per file plus a total. A failing file's last output lines are printed under it.
# Run: python -m tests            (or: python -m tests narration  to run only the files whose path has "narration")
import glob
import os
import subprocess
import sys
import time

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(TESTS_DIR)
TAIL_LINES = 30


def main():
    files = sorted(glob.glob(os.path.join(TESTS_DIR, "*", "test_*.py")))
    if len(sys.argv) > 1:
        files = [f for f in files if any(word in os.path.relpath(f, REPO_DIR) for word in sys.argv[1:])]
    if not files:
        sys.exit("No test files found.")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}  # some tests print "→", which a Windows console code page can't encode
    failed = []
    for path in files:
        name = os.path.relpath(path, REPO_DIR).replace("\\", "/")
        started = time.time()
        run = subprocess.run([sys.executable, path], cwd=REPO_DIR, env=env, capture_output=True, text=True,
                             encoding="utf-8", errors="replace")
        seconds = time.time() - started
        print(f"{'pass' if run.returncode == 0 else 'FAIL'}  {name}  ({seconds:.0f} s)", flush=True)
        if run.returncode != 0:
            failed.append(name)
            for line in (run.stdout + run.stderr).strip().splitlines()[-TAIL_LINES:]:
                print(f"      {line}")
    print(f"\n{len(files) - len(failed)} of {len(files)} test files passed" + (f", {len(failed)} failed" if failed else "."))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
