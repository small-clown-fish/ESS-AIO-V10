# Release Notes

## v9.9.24 Web Lite Health Check Fix

This release fixes the Web-only launcher false alarm where Runtime was actually running and returned `{ok: true}`, but Web.exe still displayed "Runtime not reachable" because the launcher used an overly strict api_schema text check.

Use `ESS-AIO-Web.exe` from `dist/ESS-AIO-Web-Lite/`. Closing the browser still does not stop Runtime; use the Web shutdown page or `ESS-AIO-Shutdown.exe`.

# RELEASE NOTES

## v9.9.23 Web Lite Packaging Repair

Use `ESS-AIO-Web.exe` from the top level of the Web-Lite folder. Do not move EXE files out of the folder. Runtime is bundled under `ESS-AIO-Runtime/` and Shutdown is bundled as `ESS-AIO-Shutdown.exe`.

If the browser is unreachable, check `logs/runtime_stdout.log`, `logs/runtime_stderr.log`, and `%LOCALAPPDATA%/ESS-AIO/logs/runtime_fatal.log`.
