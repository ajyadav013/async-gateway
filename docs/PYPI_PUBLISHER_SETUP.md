# Spec: configure PyPI Trusted Publishing for `asyncio-gateway`

A browser task. Everything below is exact — the values are copied from
`.github/workflows/publish.yml` and `pyproject.toml`, and OIDC compares them
character for character.

**You are logged in as `ajyadav013` on both PyPI and GitHub.** Do not create
accounts, do not generate API tokens, do not upload any package. This task only
configures who is *allowed* to upload later.

---

## Background you need

The repository `github.com/ajyadav013/asyncio-gateway` publishes **two**
distributions:

| Distribution | State | Why it exists |
|---|---|---|
| `asyncio-requests` | published, v2.7.3, ~110 downloads/month | the original name; gets one final `2.7.4` release pointing users at the new one |
| `asyncio-gateway` | **never published** | the renamed library, about to release v1.0.0 |

Both must be publishable from this one repository, so **two** trusted
publishers are needed. They differ *only* in the PyPI project name.

A publisher for `asyncio-requests` may already exist from an earlier attempt.
If it does, it will name the **wrong repository** — the repo was renamed from
`async-gateway` to `asyncio-gateway` after it was created — so it must be
corrected, not duplicated.

---

## Task 1 — GitHub environment (do this first)

The publisher config references a GitHub environment that must already exist.

1. Open <https://github.com/ajyadav013/asyncio-gateway/settings/environments>
2. If an environment named exactly `pypi` is listed, **stop — Task 1 is done.**
3. Otherwise: **New environment** → name it exactly `pypi` → **Configure
   environment**.
4. Add **no** protection rules, **no** secrets, **no** variables. The name is
   the entire contract.

**Verify:** the environments list shows `pypi`.

---

## Task 2 — publisher for `asyncio-gateway` (the new package)

This is a **pending** publisher, because the project does not exist on PyPI
yet. PyPI has a separate form for that — do not look for the project first.

1. Open <https://pypi.org/manage/account/publishing/>
2. Find the section for adding a **pending** publisher, GitHub tab.
3. Enter exactly:

   | Field | Value |
   |---|---|
   | PyPI Project Name | `asyncio-gateway` |
   | Owner | `ajyadav013` |
   | Repository name | `asyncio-gateway` |
   | Workflow name | `publish.yml` |
   | Environment name | `pypi` |

4. Submit.

**Verify:** the pending publishers list shows `asyncio-gateway` with those five
values.

---

## Task 3 — publisher for `asyncio-requests` (the existing package)

1. Open <https://pypi.org/manage/project/asyncio-requests/settings/publishing/>
2. **If a GitHub publisher is already listed:** check its *Repository name*.
   - Reads `asyncio-gateway` → correct, nothing to do.
   - Reads `async-gateway` (or anything else) → **remove it and add a new one**
     with the values below. PyPI does not allow editing a publisher in place.
3. **If none is listed:** add one.

   | Field | Value |
   |---|---|
   | Owner | `ajyadav013` |
   | Repository name | `asyncio-gateway` |
   | Workflow name | `publish.yml` |
   | Environment name | `pypi` |

   (No project-name field here — the page is already scoped to
   `asyncio-requests`.)

**Verify:** the publisher lists repository `asyncio-gateway`, workflow
`publish.yml`, environment `pypi`.

---

## The three mistakes that break this

**The repository name is not the package name.** Both happen to read
`asyncio-gateway` today, but for different reasons: one is a GitHub repo, the
other a PyPI distribution. In Task 3 the *project* is `asyncio-requests` while
the *repository* is `asyncio-gateway` — that asymmetry is correct, not a typo
to fix.

**`async-gateway` is a dead name.** It appears in this project's history
because PyPI rejected it (PEP 503 normalisation collides with an existing
`asyncgateway`). It must not appear in any field.

**Workflow name is the filename, not the display name.** `publish.yml`, not
"Publish to PyPI".

A mismatch in any field produces `invalid-publisher` at upload time — an error
whose message points at the workflow rather than at the wrong field, which is
why these values are worth double-checking now.

---

## Done when

- [ ] GitHub environment `pypi` exists
- [ ] Pending publisher for `asyncio-gateway` → repo `asyncio-gateway`,
      workflow `publish.yml`, environment `pypi`
- [ ] Publisher for `asyncio-requests` → repo `asyncio-gateway`, workflow
      `publish.yml`, environment `pypi`

Report back the state of all three, quoting each field you entered or found, so
a mismatch is visible without opening the pages again. Nothing has been
published at this point — that is the next step, and it happens from CI.
