---
name: unity-development
description: Conventions for Unity C# work: scripts, scenes, prefabs, and verifying changes without breaking serialized assets.
---

# Unity development

- Do not hand-edit `.unity`/`.prefab` YAML unless the task requires it; prefer scripts and editor tooling.
- Keep `.meta` files with their assets. Never regenerate GUIDs.
- Prefer ScriptableObjects for shared config; avoid `Find*` calls in `Update`.
- Verify by compiling (batchmode) and running EditMode/PlayMode tests when present.
