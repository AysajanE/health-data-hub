# Health Data Hub Terms of Use

Last updated: May 29, 2026

These Terms of Use govern use of Health Data Hub, a local-first personal health analytics application in this repository. By configuring or using the application, you agree to these terms.

If you are using Health Data Hub only as private personal software on your own machine, these terms are intended to make the boundaries of the application clear: it is a local retrospective analytics tool, not a medical product or hosted service.

## What Health Data Hub Is

Health Data Hub v1 is a Sleep + Mood Retrospective Explainer. It is designed to combine the user's own Oura sleep data, the user's own mood log, and optional 8 Sleep data if stable, then show cautious retrospective explanations about patterns in the user's past data.

Health Data Hub v1 is local-first. It does not provide a hosted backend, a multi-tenant service, advertising, data resale, medical diagnosis, clinical monitoring, or emergency support.

## What Health Data Hub Is Not

Health Data Hub is not a medical device, medical service, clinical decision support tool, or substitute for professional medical advice.

The application's outputs are informational and retrospective. They may describe correlations or model-estimated associations in the user's past data. They must not be treated as proof of causation, a diagnosis, a treatment recommendation, a safety warning, or a prediction of future health or mood.

Do not use Health Data Hub for emergencies or urgent health decisions. If you have a medical concern, contact a qualified healthcare professional or emergency services.

## User Responsibilities

You are responsible for:

- using Health Data Hub only with data you own or are authorized to process;
- protecting your local machine, backups, provider accounts, API tokens, and credentials;
- complying with the terms of any third-party services you connect, including Oura and 8 Sleep;
- verifying that provider scopes and permissions match your intended use;
- reviewing outputs critically and not treating them as medical, causal, or prospective advice;
- keeping raw health data, provider payloads, tokens, `.env` files, databases, snapshots, and quarantine payloads out of tracked git history;
- deciding whether to publish, share, or keep private any repository artifacts.

## Oura and Other Provider Services

Health Data Hub can use the Oura API to retrieve the user's own Oura data. Oura is an independent third-party service. Oura controls its own accounts, devices, APIs, availability, membership requirements, terms, and privacy practices.

If you use the Oura API, you are responsible for complying with Oura's terms, the Oura API Agreement, Oura's privacy requirements, and any scope or access limits Oura applies. Health Data Hub does not guarantee that Oura data will be available, complete, accurate, timely, or accessible without interruption.

8 Sleep support is optional in Health Data Hub v1 and may be disabled or replaced by an Oura-only path if the 8 Sleep ingestion path is unstable, unavailable, or not authorized for the current slice.

## Local Data and Backups

Health Data Hub stores personal data locally by design. You are responsible for maintaining backups if you want them and for securing any backup destination you choose.

Deleting local Health Data Hub files removes only the local copies. It does not delete data from Oura, 8 Sleep, iCloud, GitHub, Time Machine, or any other third-party service.

## No Warranties

Health Data Hub is provided as-is and as-available. To the maximum extent permitted by law, no warranties are made about accuracy, reliability, completeness, fitness for a particular purpose, non-infringement, availability, provider compatibility, model performance, or absence of errors.

Health Data Hub's model gates are designed to reduce misleading output, but they cannot guarantee that an explanation is correct, useful, clinically meaningful, or stable over time.

## Limitation of Liability

To the maximum extent permitted by law, the repository owner, contributors, and maintainers will not be liable for any indirect, incidental, special, consequential, exemplary, or punitive damages, or for any loss of data, health outcome, business, profit, goodwill, or use arising from Health Data Hub or connected third-party services.

This limitation applies even if a remedy fails its essential purpose and even if the possibility of damage was known.

## Acceptable Use

Do not use Health Data Hub to:

- process data you do not own or have permission to use;
- provide medical advice to another person;
- operate a hosted multi-user service without adding appropriate legal, privacy, security, and compliance controls;
- bypass provider terms, authentication, rate limits, or access restrictions;
- commit, publish, or disclose another person's health data or credentials;
- misrepresent correlation as causation or retrospective explanation as prospective medical advice.

## Repository and Software Rights

These terms do not grant any license to source code beyond any license file that may be separately published in this repository. If no license file is present, all rights not expressly granted by the repository owner are reserved.

Project documentation may describe intended behavior that is not yet implemented. The slice ledger, tests, and verification scripts are the source of truth for implementation status.

## Changes

These terms may be updated as the application changes. Material changes should be committed to the repository with a new "Last updated" date.

## Contact

For questions about these terms, contact the repository owner or use the contact email configured for the Health Data Hub Oura developer application.
