# Pull request

The title, body template, and browser-evidence steps for the PR that build-it opens in
stage 4. Always use the template below. Don't look for a repository PR template.

---

## Title

`<type>[(<scope>)][!]: <specific outcome>`, under 100 characters. Use the same types as the
commits. Write it after reading the final diff, not from the idea.

- Bad: `fix: fix payment bug` (says nothing).
- Bad: `chore: prevent duplicate payment requests` (wrong type: this changes behaviour).
- Good: `fix(checkout): prevent duplicate payment requests`.

---

## Body

Write for a reviewer who did not do the work and has not read the plan. Fill in this
template and delete any section marked optional that has nothing useful to say.

```markdown
<Why: one short paragraph on the problem or need, in the user's terms.>

<What: one short paragraph on what changed and the choices that matter. Group by idea,
not by file or commit.>

## Testing

<Optional. Only what CI won't show: manual checks, Verify steps outside the test suite,
and anything not verified.>

## Risk

<Level from the plan's Review section>: <its one-sentence reason>.
<Add the script's unreviewed-commits warning here when it reported one.>

## Evidence

<Optional. Only if collected: what each artifact proves, the URL path and viewport.>

## Model / Harness

- Model: <the model you are running as, or "unknown">
- Harness: <the agent harness running this skill>
- Plan: `<plans>/done/<NNN>-build-<slug>.md` on this branch. It records the key
  decisions, deviations from the reviewed plan, and the review results.
```

- Bad: "Added a new validation function and updated the controller to call it. Also added
  some tests." (Describes the diff, not the reason.)
- Good: "Customers sometimes see a payment failure when they submit checkout twice in quick
  succession, because we can create two payment requests for one order. This change makes
  payment creation idempotent, so a retry is safe. The checkout service now passes the order
  ID as the idempotency key."

Link an issue with `Closes #<n>` only when merging fully resolves it.

---

## Browser evidence

Collect evidence only when the change has a user-visible result in a browser and the app can
run from the worktree without real credentials or external services. If the change has a
user-visible result but the app can't run that way, skip it and give the reason in the
Testing section in one line.

1. **Define the flow** before you open a browser: the start URL, the actions, and the
   expected result, taken from the plan's requirements and ACs. Don't wander into unrelated
   pages.
2. **Start the app** with the project's documented dev command, in the background, from the
   worktree. Stop it when you are done.
3. **Capture** the smallest set that proves the change with a headless browser: a
   screenshot of a stable state, a before/after pair for a visual fix, or a short recording
   (under about 30 seconds) where the movement matters.
4. **Store** artifacts in a temporary directory outside the worktree. Delete it after the
   PR step.
5. **Check every artifact before upload.** Open each one and look at it. Don't upload
   anything that shows credentials, tokens, cookies, personal data, or real customer data.
   Strip sensitive query or fragment values from any URL you mention. If you can't confirm
   an artifact is safe, don't upload it.
6. **Attach** each artifact with `pr.py open --attach '<path>#<alt text>'`. In the body's
   Evidence section, say what each artifact proves.

Never present a failed attempt as proof. If the flow doesn't behave as the plan says, that
is a bug: fix it as stage 4 step 6 of the skill describes, then capture the evidence
again.
