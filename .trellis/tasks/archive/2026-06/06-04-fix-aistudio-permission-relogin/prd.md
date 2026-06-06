# Fix AI Studio permission after relogin

## Goal

Fix the AI Studio streaming permission failure that still occurs after two accounts were re-logged in, and verify the fix through automated checks plus real WSL API and frontend UI system tests.

## What I already know

* The failing request leases account `acc_ecb5f1e6` for external model `gemini-3.5-flash` and opens an isolated browser client.
* The hook logs show `requested=models/gemini-3-flash-preview, captured=models/gemini-3-flash-preview` even though the user requested `gemini-3.5-flash`.
* The stream then fails twice with `The caller does not have permission` after clearing snapshot cache once.
* Source inspection found `src/aistudio_api/infrastructure/gateway/wire_codec.py` mapping `gemini-3.5-flash` to `models/gemini-3-flash-preview`.
* Repo memory says startup warmup must use a safe dedicated warmup model and must not assume the default text model is authorized for every account.
* User follow-up showed `/accounts/{id}/test` returned HTTP 200 while startup preflight still isolated both real accounts because their storage states lacked AI Studio browser storage.
* User follow-up also exposed `DELETE /accounts/{id}` returning HTTP 500 with `NameError: name 'shutil' is not defined`.

## Requirements

* Preserve a logged-in account as healthy only when real generation with the selected/requested model can succeed.
* Do not silently route `gemini-3.5-flash` traffic to a different preview model that may not be authorized for free accounts.
* When a model is unsupported or unauthorized, report a clear model/account error and avoid repeated reuse of bad snapshot/template data.
* Keep existing Gemini/OpenAI-compatible request contracts working.
* Add or update focused tests around model wire encoding and the permission failure path.
* Make account health-check UI wording clear that `/accounts/{id}/test` is a credential-shape check and not a real generation permission proof.
* Account deletion must be covered by unit tests plus real WSL API/UI smoke tests using a temporary copy of the accounts directory, not the source credential directory.

## Acceptance Criteria

* [ ] A request for `gemini-3.5-flash` no longer captures or sends `models/gemini-3-flash-preview` unless that is intentionally configured and verified.
* [ ] The observed permission failure is fixed at root cause or downgraded to a clear unsupported-model response before gateway submission.
* [ ] Relevant unit tests pass.
* [ ] Real WSL API system test passes using the account credentials under `/home/bamboo/nexus-studio/data/accounts`.
* [ ] Real frontend UI system test passes against the local studio/API flow.
* [ ] `DELETE /accounts/{id}` returns 200/404 controlled responses and does not raise ASGI 500; the UI delete flow removes the row without console or 5xx errors.
* [ ] `/accounts/{id}/test` success is not presented as proof of real model generation permission; real readiness must come from `GET /health` warmup or actual API/UI generation.
* [ ] Final branch is merged to `main` through a PR, local `main` is synced, and the merged feature branch is deleted.

## Definition of Done

* Tests added/updated where appropriate.
* Lint/type-check or targeted equivalents run.
* Real API and UI system tests run for this code/API/browser/gateway change.
* Trellis spec update decision recorded.
* Work committed with task metadata.

## Out of Scope

* Reworking the whole provider/account architecture.
* Requiring the user to log in again unless real testing proves the stored accounts are invalid.

## Technical Notes

* Likely affected areas: gateway wire codec, model capabilities/selection, stream retry/auth handling, account health or warmup validation.
* Use existing WSL smoke scripts and `SYSTEM_TEST_PLAN.md` for final verification.
