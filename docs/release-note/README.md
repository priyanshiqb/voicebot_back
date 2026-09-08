# Release Notes

What changed, release by release — written for the people integrating against this API.

| Version | Date | Summary |
|---|---|---|
| [1.0.2](1.0.2.md) | unreleased | Minutes-driven plans: bands, per-minute pricing, `for-minutes/`, quantity on signup and change-plan, `base_price` and `allocated_credits` removed |
| [1.0.1](1.0.1.md) | 2026-08-14 | Session retention & zero-query validation, FCM device-token lifecycle, push delivery hardening, session↔device link, push self-test endpoint |

---

## What goes in a release note

Every note has **exactly two sections**, and both are independently copyable, as is each
endpoint entry and the page as a whole.

### Section 1 — What's New

High-level module updates only. One entry per feature: what it is, why it changed in a
sentence or two, and a link to the detailed document — the **flow** doc if one exists,
otherwise the **API** doc. No endpoint detail here.

### Section 2 — Endpoint Changes

Every endpoint whose contract or behaviour changed, with:

- the HTTP method and full path as its heading
- **changed request fields** — named, typed, and marked optional/required
- **changed response fields** — named and typed
- **new or changed status codes** — with the exact message
- **new server-side side effects** that a client cannot see in the payload
- what the client should *do* about it, when action is needed

Unchanged parts are stated as *unchanged* rather than omitted, so a reader never has to guess
whether something was overlooked.

A short table at the end covers behaviour changes that alter no endpoint signature.

---

## Copy buttons

Buttons are generated from paired HTML comments, which are invisible when the file is read on
GitHub:

```markdown
<!--copy:start id="unique-id" label="Copy Section 1"-->
...markdown to copy...
<!--copy:end-->
```

- `id` must be unique within the page.
- `label` is the button text.
- **Regions may nest.** A section wraps its endpoints, and copying the section yields
  everything inside it — the parser folds each child's text into its parent.
- Each marker must sit **alone on its own line**.

Implemented in [`docs/index.html`](../index.html) as the first docsify plugin, so the existing
**Copy Page Markdown** button still yields clean markdown with no directives in it.

---

## Adding the next release

Ask which is wanted before writing anything — the two are not interchangeable:

**Amend the current version** when the change ships as part of an unreleased version, or
corrects something already written here.

**Create a new version** when the previous one has been released and consumed. Then:

1. Create `docs/release-note/<version>.md` following the two-section structure above.
2. Add a row to the table at the top of this page, newest first.
3. Add a sidebar entry in [`docs/_sidebar.md`](../_sidebar.md) under **🚀 Release Notes**,
   newest first.
4. Scope it to the commits being released, and verify endpoint claims against the generated
   OpenAPI document rather than from memory — see
   [Command Runbook §7](../runbook.md).
