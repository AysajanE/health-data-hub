# S07 Read API and Streamlit UI Autonomous Brief

Autonomy profile: guarded zero-supervision for S07 only.

Manual gates are forbidden. S07 builds the read API and Streamlit UI for retrospective v1 output only.

## Provider Policy Requirements

- UI may show `Sleep source: Oura`.
- UI may show `8 Sleep: not active in v1 provider path`.
- UI must not show `Merged from Oura + 8 Sleep`.
- UI must not show `8 Sleep-adjusted sleep score`.
- UI must not show `8 Sleep says...`.
- UI must not imply 8 Sleep was averaged, blended, reconciled, or used as fallback for v1 model features.

## Required UI Language

Use `top model contributors`, `patterns associated with this rating`, `model-estimated change in your past data`, `correlation, not proven causation`, `insufficient stable signal`, and `collecting model-ready days`.

Do not use `drivers`, `biggest drivers`, `caused`, `what made you tired`, `you should`, `you would have felt`, `tomorrow prediction`, `recommendations today`, or prospective intervention language in v1.
