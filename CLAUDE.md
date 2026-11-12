# builds

One public home for Robert's runnable demos and prototypes (since 2026-11-12). Before this,
each demo was its own repo (`localdesk`, `ai-customers-workshop`, `signal-desk`).

## Rules

- **One folder per build, fully self-contained:** own `README.md`, own `requirements.txt`
  (or equivalent), own venv inside the folder, own `.env`, own port. No shared code or shared
  environment between folders; their dependency pins differ on purpose.
- **Read the folder's own `CLAUDE.md` before changing it** (localdesk, signal-desk and
  ai-customers-workshop have one).
- **New demo that is not tied to a course's materials goes here by default:** add a folder,
  add one row to the table in the root `README.md`, pick a free port (taken: 7860, 7870, 8000).
- **Public repo:** no customer names, no real data, no secrets. `.env` files are ignored at
  the root and per folder; check `git status` before every commit. Large generated data is
  rebuilt by a script, not committed.
- **Not for here:** static single-file browser demos (they live in `barcik-training-demos`,
  which has its own rules and its "nothing sent to a model" notice) and course code that
  notebooks link to (stays in the course repo).
- Git: commit a change to one build with the folder name first in the message
  (`localdesk: ...`). History of the imported builds was kept with `git subtree add`.
- Deploys are per build (see `localdesk/deploy/`, `signal-desk/deploy/`); scripts resolve
  paths relative to their own folder.
