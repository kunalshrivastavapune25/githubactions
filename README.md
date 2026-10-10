Both cases are handled. The drift stage notices whether the stack already exists and picks the right change set type.

| Stage | New stack (first deployment) | Next deployment (adding resources to an existing stack) |
|---|---|---|
| Preflight, lint, security scan | Same as always | Same as always |
| 🔍 Drift check | Skipped, because nothing exists yet. The box still turns green, with the note "nothing to drift". | Runs on the resources already in the stack. The new resource isn't deployed yet, so it can't have drifted. |
| 📝 Change set | Type **CREATE**. CloudFormation makes an empty placeholder stack (`REVIEW_IN_PROGRESS`), and every resource shows as **Add**. | Type **UPDATE**. The new resource shows as **Add**, and anything that references it may show as **Modify**. |
| Risk rating | Mostly LOW. IAM, security group and network resources are rated MEDIUM. | Same rules, plus any replacements or deletions of existing resources. |
| 📧 Approval email | Shows Type CREATE. The CLI section waits for `stack-create-complete`. Rejecting means deleting the change set and the empty placeholder stack, and the email includes both commands. | The normal approval email. |
| 🚀 Execute (approval gate mode) | No drift re-check, since there's nothing to drift. | Drift is checked again just before executing. |
| If execution fails | The stack ends in `ROLLBACK_COMPLETE`. The next run stops at the drift box and emails you the exact `delete-stack` command, because AWS only allows deleting a stack in that state. | It rolls back to the last good state (`UPDATE_ROLLBACK_COMPLETE`), and the next run works normally. |

A few details worth knowing:

- **Name clashes are caught before anything deploys.** Say your new resource has a fixed name that already exists in the account, like a bucket someone created by hand. CloudFormation's pre-deployment validation rejects the change set. The run stops at the Change set box, and its summary names the resource and the exact error.
- **Parameters.** A new stack takes every value from `params.json` or the template default. On updates, a parameter missing from `params.json` keeps its currently deployed value, the same as your old `aws cloudformation deploy` step.
- **A CREATE change set that nobody executes** just leaves the empty placeholder stack behind. The next run treats it as new again and makes a fresh change set, so nothing gets stuck.
- **`driftPolicy` has no effect on a new stack.**

Two additions would make these cases smoother:
- **Auto-delete a failed first creation**, so you can fix the template and re-run without deleting the stack by hand.
- **Import a resource that already exists**, instead of failing on the name clash.

Do you want me to add either?

I'll build a shareable page with two diagrams stacked: the new pipeline (now including the new-stack and new-resource checks) on top, and your old pipeline below with each risky step marked.

Published. I'll also export the two diagrams as high-resolution PNGs, so you can drop them into a client deck or email.

The exported PNGs are crisp. The numbered markers need their key when the image is used alone, so I'll export the risk table as a third image.

I've published the comparison page. It has:
- an at-a-glance comparison table
- the proposed flow, now including the new-stack and new-resource checks
- your current flow underneath, with numbered risk markers
- a short table explaining each numbered risk

The page is private, so your client can't open it until you share it from the page's **Share** menu. If you'd rather paste the diagrams into a deck or email, the three PNG files are the proposed flow, the current flow, and the risk table that explains the numbers (the numbers mean nothing without it).

All ten risks come straight from `latest_cft_deployment.yml`, so each one can be backed up by pointing at the line. Two that the client may question:
- **Risk 7 (failure diagnostics):** for small templates, the "fetch failure reason" block in the old file never runs. GitHub stops the step as soon as `aws cloudformation deploy` fails, before it reaches that block.
- **Risk 5 (parameters with spaces or commas):** if the WAF logs bucket parameter failure you hit earlier came from this workflow, that's a real example you can cite here.
<img width="2080" height="2020" alt="image" src="https://github.com/user-attachments/assets/096da4b6-83ac-43ff-8dd1-39b48bc69822" />
<img width="2080" height="3620" alt="image" src="https://github.com/user-attachments/assets/3032fc94-89d6-4ba3-a1e4-4991ba8a79c1" />
<img width="2080" height="1734" alt="image" src="https://github.com/user-attachments/assets/42e2308e-a7a9-4691-a2cc-6846eba03ef1" />













