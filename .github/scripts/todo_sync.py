#!/usr/bin/env python3
"""Synchronize the README Complete TODO List with TODO markers in *.py files.

MODE=check : validate main baseline, report main->dev changes, dry-run the edit.
MODE=apply : rebuild entries for *.py files changed between MAIN_REF and HEAD,
             keep every other README entry byte-for-byte, commit and push to dev.
"""
import difflib
import os
import re
import subprocess

ROOT = os.environ.get("GITHUB_WORKSPACE", ".")
MODE = os.environ.get("MODE", "check").strip().lower()
MAIN = os.environ.get("MAIN_REF", "origin/main")
BRANCH = os.environ.get("GITHUB_REF_NAME", "dev")
TITLE = "### \U0001F4DD Complete TODO List"
CONTRIBUTING = "## \U0001F91D Contributing"
ENTRY_RE = re.compile(r"^- \[ \] \*\*(?P<path>.+?):(?P<line>\d+)\*\* - (?P<msg>.*)$")

report = []


def out(msg=""):
    print(msg)
    report.append(str(msg))


def run(args, check=True):
    p = subprocess.run(args, cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if check and p.returncode != 0:
        raise RuntimeError(f"{args!r} rc={p.returncode}\nSTDOUT:{p.stdout}\nSTDERR:{p.stderr}")
    return p.stdout


def git(*args, check=True):
    return run(["git", *args], check=check)


def norm(msg):
    return " ".join(msg.split()).strip()


def parse_marker(text):
    marker = text.find("TODO")
    if marker == -1:
        return None
    tail = text[marker + 4:]
    msg = tail.strip()
    if msg.startswith(":"):
        msg = msg[1:].strip()
    return msg


def git_grep_todos(ref):
    p = subprocess.run(
        ["git", "grep", "-n", "-I", "-e", "TODO", ref, "--", "*.py"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    # rc 1 == no matches
    if p.returncode not in (0, 1):
        raise RuntimeError(f"git grep failed: {p.stderr}")
    result = {}
    for line in p.stdout.splitlines():
        parts = line.split(":", 3)
        if len(parts) == 4:  # <rev>:<path>:<line>:<text>
            _, path, lineno, text = parts
        else:               # <path>:<line>:<text>
            path, lineno, text = parts
        msg = parse_marker(text)
        if msg is None:
            continue
        result.setdefault(path, []).append((int(lineno), msg))
    for path in result:
        result[path].sort(key=lambda x: x[0])
    return result


def worktree_todos(path):
    full = os.path.join(ROOT, path)
    if not os.path.exists(full):
        return []
    with open(full, encoding="utf-8", errors="replace") as f:
        text = f.read()
    items = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        msg = parse_marker(raw)
        if msg is not None:
            items.append((lineno, msg))
    return items


def parse_readme(text):
    lines = text.splitlines()
    hidx = next(i for i, l in enumerate(lines) if l.strip() == TITLE)
    eidx = next((i for i in range(hidx + 1, len(lines)) if lines[i].startswith("## ")),
                len(lines))
    entries, malformed = [], []
    for i in range(hidx + 1, eidx):
        line = lines[i]
        if not line.strip():
            continue
        m = ENTRY_RE.match(line)
        if not m:
            malformed.append((i + 1, line))
        else:
            entries.append({"raw": line, "path": m.group("path"),
                            "line": int(m.group("line")), "msg": m.group("msg"),
                            "idx": i})
    return lines, hidx, eidx, entries, malformed


def render(path, line, msg):
    return f"- [ ] **{path}:{line}** - {msg}"


def entry_key(raw):
    m = ENTRY_RE.match(raw)
    return (m.group("path"), int(m.group("line")))


def changed_py_files():
    out = git("diff", "--name-only", f"{MAIN}...HEAD")
    return [p for p in out.splitlines() if p.endswith(".py")]


def build_new_entries(entries, changed):
    kept = [e["raw"] for e in entries if e["path"] not in changed]
    rendered_new, per_file = [], {}
    for path in changed:
        items = worktree_todos(path)
        per_file[path] = items
        for lineno, msg in items:
            rendered_new.append(render(path, lineno, msg))
    combined = kept + rendered_new
    combined.sort(key=entry_key)
    return combined, per_file


def main_alignment(entries):
    code = git_grep_todos(MAIN)
    by_file = {}
    for e in entries:
        by_file.setdefault(e["path"], []).append(e)
    files = sorted(set(code) | set(by_file))
    bad = 0
    out("=== MAIN baseline alignment (code @%s vs README) ===" % MAIN)
    for path in files:
        c = {}
        for ln, msg in code.get(path, []):
            c.setdefault(norm(msg), []).append(ln)
        r = {}
        for e in by_file.get(path, []):
            r.setdefault(norm(e["msg"]), []).append(e["line"])
        only_code = {k: v for k, v in c.items() if k not in r}
        only_readme = {k: v for k, v in r.items() if k not in c}
        moved = {k: (r[k], c[k]) for k in c.keys() & r.keys()
                 if sorted(c[k]) != sorted(r[k])}
        if only_code or only_readme or moved:
            bad += 1
            out(f"-- {path}")
            for k, v in sorted(only_code.items()):
                out(f"   CODE-ONLY lines={v} :: {k}")
            for k, v in sorted(only_readme.items()):
                out(f"   README-ONLY lines={v} :: {k}")
            for k, (rl, cl) in sorted(moved.items()):
                out(f"   LINE-DIFF readme={rl} code={cl} :: {k}")
    out(f"alignment: {bad} file(s) with differences; "
        f"code_total={sum(len(v) for v in code.values())} readme_total={len(entries)}")
    return bad


def git_commit_push(message, add_paths):
    for p in add_paths:
        git("add", p)
    cached = run(["git", "diff", "--cached", "--name-only"], check=True).strip()
    if not cached:
        out("nothing to commit")
        return False
    git("config", "user.name", "github-actions[bot]")
    git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    git("commit", "-m", message)
    git("push", "origin", f"HEAD:{BRANCH}")
    return True


def main():
    readme_path = os.path.join(ROOT, "README.md")
    with open(readme_path, encoding="utf-8") as f:
        text = f.read()

    lines, hidx, eidx, entries, malformed = parse_readme(text)
    if malformed:
        out("MALFORMED SECTION LINES:")
        for ln, l in malformed:
            out(f"  {ln}: {l}")
        if MODE == "apply":
            raise SystemExit("refusing to apply with malformed README lines")

    out(f"parsed README entries: {len(entries)}")
    main_alignment(entries)

    changed = changed_py_files()
    out("\n=== main->dev changed .py files ===")
    for p in changed:
        out("  " + p)

    combined, per_file = build_new_entries(entries, changed)
    out(f"\nentries after transform: {len(combined)} (was {len(entries)})")

    for path in changed:
        out(f"\n--- dev TODOs in {path} ---")
        for ln, msg in per_file[path]:
            out(f"  {ln}: {msg}")
        old = [e for e in entries if e["path"] == path]
        out(f"  old README entries for file: {len(old)}; new: {len(per_file[path])}")

    old_body = [e["raw"] for e in entries]
    diff = list(difflib.unified_diff(
        old_body, combined, fromfile="readme-old-section",
        tofile="readme-new-section", lineterm=""))
    out("\n=== planned section diff (entry lines) ===")
    for d in diff:
        out(d)

    keys = [entry_key(l) for l in combined]
    if keys != sorted(keys):
        raise SystemExit("FATAL: combined entries not sorted")
    out("\nsort order check: OK")

    if MODE != "apply":
        report_path = os.path.join(ROOT, ".github", "todo-report.txt")
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(report) + "\n")
        git_commit_push("chore(ci): add TODO sync dry-run report",
                        [".github/todo-report.txt"])
        out("check mode: report committed; README not modified")
        return

    new_lines = lines[:hidx + 1] + [""] + combined + [""] + lines[eidx:]
    new_text = "\n".join(new_lines)
    if not new_text.endswith("\n"):
        new_text += "\n"

    if new_text == text:
        out("apply mode: README already up to date; nothing to commit")
        return

    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(new_text)
    git_commit_push("docs(readme): update Complete TODO List for dev branch",
                    ["README.md"])
    out("apply mode: README.md updated and pushed")


if __name__ == "__main__":
    main()
