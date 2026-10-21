# Local source candidate

This is a source snapshot, not an installed application or deployment approval.
Python dependencies, Playwright browser binaries, private configuration and historical data are intentionally absent. The extracted runtime was checked offline with an already installed Python environment; no supplier acquisition or paid-service readiness is claimed.

For normal use, separately provision a compatible Python environment with the pinned `engine/requirements.txt` and a compatible Playwright Chromium installation. Installation requires separate permission and network access. This packaging task ran no installation commands.

From the extracted repository root, copy the public placeholder template with `cp engine/.env.example engine/.env`, then set only separately authorized credentials and provider/model settings. The supplied template is generated for this package and contains only empty credentials; it is not a copy of private configuration. It deliberately selects no provider, model, endpoint or proxy. Current defaults and supported options are in `engine/config.py`. Native source parsing without credentials does not establish online search or model availability.

After provisioning, run the environment's Python from the repository root:

```sh
/path/to/provisioned/python -m uvicorn engine.app:app --host 127.0.0.1 --port 8765
```

Open `http://127.0.0.1:8765/`. On macOS, `start.command` is also available when the environment has specifically been provisioned at `engine/.venv`. That environment is not bundled. `engine/run.sh` and `engine/run.cmd` are existing installation scripts and may install dependencies/browser binaries; they are included to keep documented entrypoints available and were not executed or qualified by this task.

`engine/run_catalogue_refresh.py` is an explicit recorded-card refresh CLI, not an installed scheduler or permission to call suppliers. Its inputs are existing separately owned operational data; no database or job is bundled. Do not point it at unrelated historical state. Optional calibration is separate from this package's offline qualification.

The included historical HANDOFF/DEVELOPER_ONBOARDING/CALIBRATION documents provide context, not proof of this candidate's online readiness; some discuss prior runs or optional developer tooling outside this clean source snapshot. Use the current README/INSTRUCTION/STATUS and this installation note for scope. The Google Fonts CSS reference remains optional and unchanged; the offline ASGI smoke did not fetch remote fonts. Full native96 business acceptance, live account/provider/browser configuration and deployment remain separate gates.
