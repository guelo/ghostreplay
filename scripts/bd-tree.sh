#!/usr/bin/env bash
# bd-tree.sh — Draw a dependency tree of all beads tickets
# Usage: ./scripts/bd-tree.sh [--open] [--no-color]
#   --open      Show only open tickets (hide fully-closed subtrees)
#   --no-color  Disable colored output

set -euo pipefail

OPEN_ONLY=false
USE_COLOR=true

for arg in "$@"; do
  case "$arg" in
    --open)     OPEN_ONLY=true ;;
    --no-color) USE_COLOR=false ;;
    -h|--help)
      echo "Usage: $0 [--open] [--no-color]"
      echo "  --open      Hide fully-closed subtrees"
      echo "  --no-color  Disable colored output"
      exit 0
      ;;
  esac
done

python3 - "$OPEN_ONLY" "$USE_COLOR" << 'PYEOF'
import re, subprocess, sys

OPEN_ONLY = sys.argv[1].lower() == "true"
USE_COLOR = sys.argv[2].lower() == "true"

# ── ANSI colors ──────────────────────────────────────────────────────────────
if USE_COLOR:
    C_RESET  = "\033[0m"
    C_DIM    = "\033[2m"
    C_GREEN  = "\033[32m"
    C_YELLOW = "\033[33m"
    C_CYAN   = "\033[36m"
    C_RED    = "\033[31m"
    C_BOLD   = "\033[1m"
    C_BLUE   = "\033[34m"
    C_MAG    = "\033[35m"
else:
    C_RESET = C_DIM = C_GREEN = C_YELLOW = C_CYAN = ""
    C_RED = C_BOLD = C_BLUE = C_MAG = ""

# ── Parse bd list ────────────────────────────────────────────────────────────
result = subprocess.run(
    ["bd", "list", "--status=all", "--limit", "0"],
    capture_output=True, text=True,
)
lines = result.stdout.strip().split("\n")

issues = {}
for line in lines:
    line = line.strip()
    if not line:
        continue

    if line.startswith("✓"):
        status = "closed"
    elif line.startswith("◐"):
        status = "in_progress"
    elif line.startswith("○"):
        status = "open"
    else:
        continue

    id_match = re.search(r"(g-[\w.]+)", line)
    if not id_match:
        continue
    iid = id_match.group(1)

    pri_match = re.search(r"\[(?:● )?(P\d)\]", line)
    priority = pri_match.group(1) if pri_match else "P9"

    type_match = re.search(r"\[(task|feature|bug|epic)\]", line)
    itype = type_match.group(1) if type_match else "task"

    title_match = re.search(r"\]\s*-\s*(.+?)(?:\s*\(blocked by:|\s*$)", line)
    title = title_match.group(1).strip() if title_match else iid

    blocked_by = []
    bb = re.search(r"blocked by:\s*([\w\-., ]+?)(?:\)|,\s*blocks:)", line)
    if not bb:
        bb = re.search(r"blocked by:\s*([\w\-., ]+)\)", line)
    if bb:
        blocked_by = [x.strip() for x in bb.group(1).split(",") if x.strip()]

    blocks_list = []
    bl = re.search(r"blocks:\s*([\w\-., ]+)\)", line)
    if bl:
        blocks_list = [x.strip() for x in bl.group(1).split(",") if x.strip()]

    issues[iid] = {
        "id": iid, "status": status, "priority": priority,
        "type": itype, "title": title,
        "blocked_by": blocked_by, "blocks": blocks_list,
    }


# ── Helpers ──────────────────────────────────────────────────────────────────
def pkey(iid):
    p = issues.get(iid, {}).get("priority", "P9")
    return (int(p[1]) if p.startswith("P") else 9, iid)

def has_open_descendant(iid, visited=None):
    """True if this node or any descendant is open."""
    if visited is None:
        visited = set()
    if iid in visited or iid not in issues:
        return False
    visited.add(iid)
    if issues[iid]["status"] != "closed":
        return True
    children = [c for c in issues[iid]["blocks"] if c in issues]
    return any(has_open_descendant(c, visited) for c in children)

STATUS_ICON = {"closed": "✓", "open": "○", "in_progress": "◐"}

TYPE_ICON = {"epic": "E", "feature": "F", "task": "T", "bug": "B"}

def status_color(s):
    return {
        "closed": C_GREEN, "open": C_YELLOW, "in_progress": C_CYAN,
    }.get(s, "")

def type_color(t):
    return {
        "epic": C_MAG, "feature": C_BLUE, "task": "", "bug": C_RED,
    }.get(t, "")

def fmt(iid):
    i = issues[iid]
    sc = status_color(i["status"])
    tc = type_color(i["type"])
    icon = STATUS_ICON.get(i["status"], "?")
    typ = TYPE_ICON.get(i["type"], "?")
    title = i["title"]
    if len(title) > 60:
        title = title[:57] + "..."

    return (
        f"{sc}{icon}{C_RESET} "
        f"[{i['priority']}]"
        f"[{tc}{typ}{C_RESET}] "
        f"{C_DIM}{iid}{C_RESET}: "
        f"{title}"
    )


# ── Tree printer ─────────────────────────────────────────────────────────────
expanded = set()
output_lines = []

def print_tree(node_id, prefix="", is_last=True, depth=0, ancestors=None):
    if ancestors is None:
        ancestors = set()
    if node_id not in issues:
        return

    if OPEN_ONLY and not has_open_descendant(node_id):
        return

    connector = "└── " if is_last else "├── "
    label = fmt(node_id)

    children = list(dict.fromkeys(
        c for c in issues[node_id]["blocks"] if c in issues
    ))
    children.sort(key=pkey)

    if OPEN_ONLY:
        children = [c for c in children if has_open_descendant(c)]

    if node_id in ancestors:
        line = f"{prefix}{connector}{label}  {C_DIM}⟳ cycle{C_RESET}" if depth > 0 else label
        output_lines.append(line)
        return

    if node_id in expanded and children:
        ref = f"  {C_DIM}(→ see above){C_RESET}"
        line = (f"{prefix}{connector}{label}{ref}" if depth > 0 else f"{label}{ref}")
        output_lines.append(line)
        return

    line = f"{prefix}{connector}{label}" if depth > 0 else label
    output_lines.append(line)
    expanded.add(node_id)
    ancestors.add(node_id)

    for i, child_id in enumerate(children):
        is_child_last = i == len(children) - 1
        if depth > 0:
            new_prefix = prefix + ("    " if is_last else "│   ")
        else:
            new_prefix = "    "
        print_tree(child_id, new_prefix, is_child_last, depth + 1, ancestors.copy())


# ── Identify roots ───────────────────────────────────────────────────────────
roots = sorted(
    [iid for iid, iss in issues.items()
     if not any(b in issues for b in iss["blocked_by"])],
    key=pkey,
)

epics = [r for r in roots if issues[r]["type"] in ("epic", "feature") and issues[r]["blocks"]]
task_trees = [r for r in roots if r not in epics and issues[r]["blocks"]]
standalone = [r for r in roots if not issues[r]["blocks"]]

# ── Render ───────────────────────────────────────────────────────────────────
BAR = "═" * 80
output_lines.append(f"{C_BOLD}{BAR}{C_RESET}")
mode = "OPEN ONLY" if OPEN_ONLY else "ALL TICKETS"
output_lines.append(f"{C_BOLD}  GHOSTREPLAY DEPENDENCY TREE  ({mode}){C_RESET}")
output_lines.append(f"{C_BOLD}{BAR}{C_RESET}")
output_lines.append(
    f"  {C_GREEN}✓{C_RESET}=closed  "
    f"{C_YELLOW}○{C_RESET}=open  "
    f"{C_CYAN}◐{C_RESET}=in-progress  "
    f"[{C_MAG}E{C_RESET}]pic  "
    f"[{C_BLUE}F{C_RESET}]eature  "
    f"[T]ask  "
    f"[{C_RED}B{C_RESET}]ug"
)
output_lines.append(f"{C_BOLD}{BAR}{C_RESET}")

if epics:
    output_lines.append(f"\n{C_BOLD}── Epic / Feature Trees ──{C_RESET}\n")
    for r in epics:
        print_tree(r)
        output_lines.append("")

if task_trees:
    output_lines.append(f"\n{C_BOLD}── Task Trees ──{C_RESET}\n")
    for r in task_trees:
        print_tree(r)
        output_lines.append("")

unprinted = [iid for iid in standalone if iid not in expanded]
if OPEN_ONLY:
    unprinted = [iid for iid in unprinted if has_open_descendant(iid)]

if unprinted:
    output_lines.append(f"\n{C_BOLD}── Standalone ──{C_RESET}\n")
    for iid in sorted(unprinted, key=pkey):
        output_lines.append(f"  {fmt(iid)}")

# ── Summary ──────────────────────────────────────────────────────────────────
total = len(issues)
closed = sum(1 for i in issues.values() if i["status"] == "closed")
opened = total - closed

output_lines.append(f"\n{C_BOLD}{BAR}{C_RESET}")
output_lines.append(
    f"  {C_BOLD}{total}{C_RESET} tickets  │  "
    f"{C_GREEN}✓ {closed} closed{C_RESET}  │  "
    f"{C_YELLOW}○ {opened} open{C_RESET}"
)
output_lines.append(f"{C_BOLD}{BAR}{C_RESET}")

print("\n".join(output_lines))
PYEOF
