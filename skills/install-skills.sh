#!/usr/bin/env bash
set -euo pipefail

# Install skills by creating symlinks from ~/.claude/skills/ to this repository.
# Automatically discovers all skill directories (containing SKILL.md) under skills/.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILLS_SOURCE="${SCRIPT_DIR}"
SKILLS_TARGET="${HOME}/.claude/skills"

echo "Installing skills from: ${SKILLS_SOURCE}"
echo "Symlink target: ${SKILLS_TARGET}"
echo ""

mkdir -p "${SKILLS_TARGET}"

installed=0
skipped=0

for skill_dir in "${SKILLS_SOURCE}"/*/; do
  # Skip if not a directory
  [ -d "${skill_dir}" ] || continue

  # Skip if no SKILL.md present (not a valid skill)
  [ -f "${skill_dir}/SKILL.md" ] || continue

  skill_name="$(basename "${skill_dir}")"
  link_path="${SKILLS_TARGET}/${skill_name}"
  # Remove trailing slash for clean symlink source
  source_path="${skill_dir%/}"

  if [ -L "${link_path}" ]; then
    current_target="$(readlink -f "${link_path}")"
    expected_target="$(readlink -f "${source_path}")"
    if [ "${current_target}" = "${expected_target}" ]; then
      echo "  [skip] ${skill_name} (already linked)"
      skipped=$((skipped + 1))
      continue
    else
      echo "  [update] ${skill_name} (relink: ${current_target} -> ${expected_target})"
      rm "${link_path}"
    fi
  elif [ -e "${link_path}" ]; then
    echo "  [warn] ${skill_name}: ${link_path} exists but is not a symlink, skipping"
    skipped=$((skipped + 1))
    continue
  fi

  ln -s "${source_path}" "${link_path}"
  echo "  [install] ${skill_name} -> ${source_path}"
  installed=$((installed + 1))
done

echo ""
echo "Done. Installed: ${installed}, Skipped: ${skipped}"
