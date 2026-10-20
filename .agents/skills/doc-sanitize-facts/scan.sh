#!/usr/bin/env bash
#
# scan.sh — flag documentation that still carries non-fact "filler" and find
# broken / terminal-style relative links. A signal for the facts-only-docs
# skill, not a verdict: a hit isn't always filler, and a clean file can still
# need verifying against the code.
#
# Usage:
#   bash .claude/skills/facts-only-docs/scan.sh [dir]   (default: docs)
#
# Portable to macOS bash 3.2 (no associative arrays).

set -u

DIR="${1:-docs}"

if [ ! -d "$DIR" ]; then
  echo "✗ not a directory: $DIR" >&2
  exit 1
fi

# Filler pattern table: "LABEL|||EXTENDED-REGEX" (case-insensitive grep).
PATTERNS=(
  "rationale|||\bDecision\.|\bTradeoff\b|anti-pattern|prior art|alternatives considered|^#{1,6} .*\bWhy\b"
  "status/date|||Status:|ratified|\bPoC\b|as of 20[0-9][0-9]|audited"
  "dated-snapshot|||20[0-9][0-9]-[0-9][0-9]-[0-9][0-9]"
  "ticket-ref|||MaxChat/[0-9]|Intents/|Tier-[0-9]|§[0-9]"
  "AC/story|||acceptance criteria|\bAC:|given/when/then|as a user|user stor(y|ies)"
  "planning|||delivery plan|\bwave [0-9]|open questions|follow-ups?"
)

flagged=0

while IFS= read -r f; do
  hits=""
  for entry in "${PATTERNS[@]}"; do
    label="${entry%%|||*}"
    regex="${entry##*|||}"
    n=$(grep -nEi "$regex" "$f" 2>/dev/null | wc -l | tr -d ' ')
    if [ "$n" -gt 0 ]; then
      hits="$hits ${label}(${n})"
    fi
  done

  # Link checks: terminal-style :line suffix (breaks on GitHub) and broken
  # relative targets. Skip absolute (http/mailto) and pure-anchor (#...) links.
  termlinks=0
  broken=0
  fdir=$(dirname "$f")
  while IFS= read -r target; do
    [ -z "$target" ] && continue
    case "$target" in
      http*|mailto:*|"#"*) continue ;;
    esac
    path="${target%%#*}"
    [ -z "$path" ] && continue
    case "$path" in
      *:[0-9]*) termlinks=$((termlinks + 1)) ;;
    esac
    clean="${path%%:*}"
    if [ ! -e "$fdir/$clean" ]; then
      broken=$((broken + 1))
    fi
  done < <(grep -oE '\]\([^)]+\)' "$f" 2>/dev/null | sed -E 's/^\]\(//; s/\)$//')

  if [ -n "$hits" ] || [ "$termlinks" -gt 0 ] || [ "$broken" -gt 0 ]; then
    flagged=$((flagged + 1))
    echo "$f"
    [ -n "$hits" ] && echo "  filler:$hits"
    if [ "$termlinks" -gt 0 ] || [ "$broken" -gt 0 ]; then
      echo "  links: ${termlinks} terminal-suffix, ${broken} broken"
    fi
  fi
done < <(find "$DIR" -type f -name '*.md' | sort)

echo ""
if [ "$flagged" -eq 0 ]; then
  echo "✓ no filler signals or link issues in $DIR"
else
  echo "→ $flagged file(s) flagged. Interpret, don't auto-trust — verify facts against code."
fi