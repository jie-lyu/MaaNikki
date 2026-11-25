# resource_android

Android emulator layer, loaded on top of `resource/` by the `nikki_android` controller (`attach_resource_path`) and,
for the agent's own lookups, by `agent/app_paths.py` (`resource_layers`). It holds only what differs on the phone
layout: pipeline nodes with other regions or templates, phone images under `image/` with the same relative names as
the PC images they replace, and catalog entries in `image/game/catalog.json` (merged field by field over the base
catalog). See `docs/android-support-plan.md`, decisions 3 and 4. The Windows package does not include this directory.
