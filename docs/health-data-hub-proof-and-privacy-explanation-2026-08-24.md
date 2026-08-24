# Health Data Hub: Why the Proof and Privacy Systems Exist

**Saved:** August 24, 2026, America/Toronto

**Repository revision at save time:** `1ca90a96a261ee2d165380c516aebf12328d9ad1`

**Purpose:** Preserve the plain-language explanation of which controls are useful for this personal health application and which parts have become disproportionate.

## The shortest answer

This repository has mixed together two different projects:

1. Building a private sleep-and-mood application for one person.
2. Building a system that can autonomously write code and produce a tamper-resistant record proving every decision without trusting a person.

Most of the confusing checkers, receipts, and proof-refresh work belong to the second project, not to the health application itself.

## Why did one commit make old proofs stale?

An ordinary Git commit does not require refreshed proof.

This repository added its own integration-receipt system. A receipt records exact digital fingerprints of files that were reviewed for a completed part of the project.

Think of it this way:

- A receipt says, “I checked version A of these files.”
- A later commit changes one of those files to version B.
- The receipt remains truthful about version A.
- The receipt cannot claim that version B was reviewed.
- The checker therefore rejects it until another receipt covers version B.

The August 17 commit changed shared design documents, AutoKeel code, readiness checks, and tests that several old receipts had frozen. As a result:

- S01's proof became stale.
- S02's proof became stale.
- S04's proof became stale.
- S05's proof became stale.
- S03 remained valid because none of the files protected by its receipt changed.

This proves that not every commit requires refreshed proof. The refresh happens only when a commit changes a file covered by a receipt. The practical problem is that existing receipts cover too many broad, frequently changing files.

## Why was the proof requirement created?

It was created to solve a real historical problem.

Some completed work existed on old ship branches but was not actually part of `main`. Earlier checkers could still report that work as complete because they checked the old branch without proving that its result survived on the branch we continued using.

The receipt system was meant to answer:

> Is the work we previously approved still present in today's code?

That is a legitimate question.

The implementation went too far by freezing shared AutoKeel code, global checks, shared tests, and large planning documents that naturally change as the project continues. That means removing an obsolete paragraph from a design document can invalidate a completed model slice even when no model behavior changed.

## Is the proof system overbuilt?

For this one-person personal application, yes.

The underlying idea is reasonable. The current breadth and enforcement are disproportionate.

The current system would make more sense for:

- a regulated product;
- a product being certified for third parties;
- a system whose owner is not trusted;
- a fully autonomous operation that must prove every action without human judgment.

It is excessive for:

- one person;
- one computer;
- personal health data;
- no customers;
- no external auditor;
- no commercial deployment;
- an owner who can personally review and run the application.

A proportionate rule would be:

- Preserve existing receipts as historical records.
- Use normal Git history and tests for ongoing development.
- Recheck a completed part only when its actual product behavior or an essential safety rule changes.
- Do not recheck warehouse, API, or model work merely because a shared planning document or AutoKeel script changed.
- Create one release record at meaningful milestones rather than after routine commits.

## Why have any privacy boundary?

The finished application must read the owner's health data. That is its purpose.

The code-building tools generally do not need real health data. They can build and test with fake examples.

The useful separation is:

```text
Code-building tools -> source code and fake test data only

Reviewed application -> credentials and real health data
```

This is not about commercialization or protecting one customer from another customer. It is about preventing unfinished, generated, or buggy code from accidentally:

- printing health data;
- placing it in a test report;
- committing it to Git;
- sending it through an unintended network request;
- damaging the real database;
- reading credentials it does not need.

That risk exists because AutoKeel, Keel, Plan Orchestrator, tests, dependencies, and generated scripts run as the same macOS user who owns the health files.

A file mode such as `0600` means only that macOS user may read the file. It does not prevent another program running as that same user from reading it.

Likewise:

- `.gitignore` prevents normal Git tracking; it does not stop a program from reading a file.
- Removing secrets from environment variables closes one route; it does not prevent a program from opening `.env.local`.
- A written instruction saying “do not read private data” is a rule, not an operating-system barrier.

That is the real technical concern.

## Is the current privacy system necessary?

Not in its current form.

These ordinary protections are sensible for a personal health application:

- Keep health data out of Git.
- Keep credentials out of logs.
- Require a token for mood submissions.
- Bind the local service only where intended.
- Use owner-only file permissions.
- Use fake data in automated tests.
- Avoid retaining raw provider responses unnecessarily.
- Keep backups private.
- Protect the database from concurrent-write corruption.

The current S11 stop goes much further. It requires the autonomous builder to be unable to read private directories and requires a second independent program to verify the result produced by the first program.

That is mainly needed to support the claim:

> The autonomous system completed everything correctly without relying on the owner.

If the owner is willing to review the code and personally activate the real application, that second independent proof system is unnecessary.

The present S11 rule is also internally stuck:

- It requires `private/` to exist and be readable by the owner so the application can use it.
- It then fails because the current process, also running as the owner, can read `private/`.
- It also fails because a proposed independent activation checker has not been implemented.

The checker is therefore deliberately stopping the build because the originally demanded zero-human certification system is unfinished. It is not merely checking whether the personal health application is safe enough to use.

## What are the builders and checkers?

Several unrelated components have similar names:

- **Health Data Hub:** The actual personal application.
- **Keel compiler:** Converts a written plan into implementation tasks.
- **Plan Orchestrator:** Runs those tasks and builds the code.
- **AutoKeel:** Supervises the compiler and task runner, records progress, and decides whether another task may start.
- **S11 readiness checker:** Runs before the S11 builder. It checks plans, dependencies, file permissions, and whether the builder is isolated from private data. It does not check sleep or mood values.
- **Activation checker:** Intended to run after the application is built. It would check that the real mood form wrote the expected record and that the application works with the local database.
- **Outer activation checker:** A proposed second checker that would verify that the first checker did not merely claim success. This is useful for zero-human certification but unnecessary if the owner personally performs and observes activation.
- **Integration receipt builder:** Creates historical proof documents about completed slices. It does not build the health application.
- **Integration receipt checker:** Checks whether files covered by those historical proof documents have changed.

## Proportionate operating model for this personal tool

1. Build and test with fake health records.
2. Keep `data/`, `private/`, credentials, and the real database outside Git.
3. Keep the check that prevents health data and secrets from being tracked by Git.
4. Keep normal product tests for the API, database, features, model, and interface.
5. Do not let automated coding tools run arbitrary commands against the live health database.
6. After the code is reviewed, have the owner start the real sync and application.
7. Let the finished application—not the code builder—access Oura credentials and the health database.
8. Preserve existing proof receipts as history, but stop making them blocking requirements for routine development.
9. Remove the missing outer-validator requirement unless fully unattended, zero-human activation is genuinely required.

If completely unattended autonomous building becomes important later, use one real isolation boundary around the whole builder: a separate system account, container, or virtual machine containing only source code and fake data. That would be simpler and stronger than wrapping every step in additional proof paperwork.

## Final conclusion

Strong everyday protection for real health data is worthwhile.

The current historical receipt system and self-certifying autonomous-build machinery are not necessary for this one-person application in their present form. They should be simplified before producing more receipt-refresh work or adding more certification machinery.
