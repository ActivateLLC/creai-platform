# Creai builder

Exports a Godot project to the web, headless and CPU-only.

`POST /export/web` with `{"files": {"project.godot": "...", "main.tscn": "...", "player.gd": "..."}}`
(binary files as `base64:<data>`) returns the exported `index.html`, `.wasm`, `.pck` and friends,
base64 encoded, ready to publish. Guarded by `X-Build-Token`.

Bounded: 200 files, 40 MB, allow-listed extensions, no path escapes, export timeout.
