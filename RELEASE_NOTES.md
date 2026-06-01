# RELEASE NOTES

## v9.9.23 Web Lite Packaging Repair

Use `ESS-AIO-Web.exe` from the top level of the Web-Lite folder. Do not move EXE files out of the folder. Runtime is bundled under `ESS-AIO-Runtime/` and Shutdown is bundled as `ESS-AIO-Shutdown.exe`.

If the browser is unreachable, check `logs/runtime_stdout.log`, `logs/runtime_stderr.log`, and `%LOCALAPPDATA%/ESS-AIO/logs/runtime_fatal.log`.
