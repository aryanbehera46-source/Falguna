# Falguna Bootstrap v0.6 — local web app

## Frozen acceptance checklist

- A localhost-only browser interface identifies Falguna Engineering as an internal alpha.
- An operator can select an approved local repository profile, enter one bounded objective and explicit editable paths, choose an approved verification profile, and set a profile-bounded cost cap.
- The interface reuses the existing mission, run, status, summary, evidence, audit, cost, review, checkpoint, containment, and decision controls.
- Live status presents meaningful milestones and the existing seven actionable failure categories.
- Final evidence presents requirement coverage, native tests, browser applicability, independent review, files, cost, risk, unresolved issues, evidence hashes, audit validity, and merge approval state.
- Approve Merge, Reject, and Request Changes record human intent only. The web server contains no merge or deployment implementation.
- One browser-only representative mission reaches `DONE_CANDIDATE` or a clearly classified failure, with merge approval left `PENDING`.
- The acceptance produces zero security violations and zero protected-main merges.

No public binding, automatic merge, deployment, credential storage, arbitrary verification command entry, broader Company OS function, or replacement of the proven v0.5 control plane is in scope.
