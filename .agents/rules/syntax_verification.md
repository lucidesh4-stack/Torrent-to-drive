---
trigger: always_on
description: Mandatory rules for automated AST, syntax verification, and deterministic code editing in Streamly/CloudFlow.
---

# Deterministic AST & Code Syntax Verification Guidelines

1. **Pre-Completion Syntax Checks**:
   - Whenever any JavaScript (`.js`) file is created or modified, execute `node --check "<path>"` via `run_command` to guarantee zero syntax errors, unclosed brackets, or invalid tokens before concluding the task.
   - Whenever any JSON (`.json`) file is created or modified, verify validity using `node -e "JSON.parse(fs.readFileSync('<path>'))"`.
   - Whenever Python (`.py`) code is modified, if Python is available in the local or container environment, verify it with `python -m py_compile "<path>"`.

2. **AST-Aware Structural Edits**:
   - Prefer surgical replacements with `replace_file_content` targeting exact functions, blocks, or statements rather than rewriting entire files.
   - Use `ast-grep` or AST search when performing structural pattern matching or multi-file refactors.
   - Never leave syntax unbalanced: always maintain matching parentheses, braces, quotes, and commas.

3. **Automatic Error Remediation**:
   - If any syntax verification check fails, immediately fix the syntax error in the same turn before notifying the user or proceeding to deployment.
