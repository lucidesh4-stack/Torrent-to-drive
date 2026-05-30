# Changes.json — How to specify changes for deploy_all.py

> Copy the template below. Fill in the session name and file changes.
> Run: `python3 ai/deploy/deploy_all.py`
> Done. All files written, check.py verified, docs auto-updated, zip created.

---

## Template

```json
{
  "session": "YYYY-MM-DD — brief description of what changed",
  "changes": [
    {
      "file": "relative/path/to/file.py",
      "content": "... full file content as a string ..."
    }
  ]
}
```

---

## Rules

- `session` — date-stamped description. Appears in CHANGELOG, CONTEXT, ACTIVITY_LOG.
- `changes` — array of file objects. Each file: full content (not diff).
- Only list files that ACTUALLY changed. deploy_all.py writes all listed files.
- For JS fragments: edit `static/js/src/1-core.js` etc. deploy_all.py rebuilds `app.js` automatically.
- For CSS: edit `static/css/base.css` or `static/css/responsive.css`.
- For templates: edit `templates/index.html`.

---

## Examples

### Example 1: Fix one route

```json
{
  "session": "2026-05-31 — fix get_url error handling",
  "changes": [
    {
      "file": "streamly_hardened/app.py",
      "content": "from __future__ import annotations\n\nimport logging\n..."
    }
  ]
}
```

### Example 2: Add new JS feature

```json
{
  "session": "2026-05-31 — add keyboard shortcut for logout",
  "changes": [
    {
      "file": "streamly_hardened/static/js/src/6-main.js",
      "content": "  document.addEventListener(\"keydown\", (e) => {\n    if (e.ctrlKey && e.shiftKey && e.key === \"L\") {\n      // logout logic here\n    }\n  });\n"
    }
  ]
}
```

### Example 3: Multiple files

```json
{
  "session": "2026-05-31 — add storage check before add",
  "changes": [
    {
      "file": "streamly_hardened/app.py",
      "content": "... full app.py content ..."
    },
    {
      "file": "streamly_hardened/static/js/src/5-search.js",
      "content": "... full 5-search.js content ..."
    },
    {
      "file": "streamly_hardened/services.py",
      "content": "... full services.py content ..."
    }
  ]
}
```

---

## Checklist before writing changes.json

- [ ] Read the current file from workspace first
- [ ] Trace the full data path (caller → function → callee)
- [ ] Check CHANGELOG for related decisions
- [ ] Handle null/empty/max edge cases
- [ ] Match existing code style (indentation, naming, comments)
- [ ] Note any judgment calls in comments for user to review
- [ ] Verify: no CSS load order change, no direct app.js edit
