# Telethon to Kurigram Migration Plan

## Status

Deferred. The project will continue using Telethon; this migration is not an active work item.

## Scope

- Replace Telethon with Kurigram.
- Use `tgcrypto-pyrofork` for accelerated cryptographic operations.
- Use `winuvloop` on Windows, without changing event-loop behavior on other platforms unless package requirements dictate otherwise.
- Preserve current bot behavior and handle existing user sessions safely.

## Implementation steps

1. **Verify package and runtime compatibility**
   - Confirm Kurigram, `tgcrypto-pyrofork`, and `winuvloop` package names, supported Python versions, and documented initialization requirements.
   - Keep the project's Python `>=3.13` requirement intact unless compatibility findings require a decision.

2. **Update dependencies**
   - Replace Telethon and `cryptg` in `pyproject.toml` with Kurigram and `tgcrypto-pyrofork`.
   - Add `winuvloop` with an appropriate Windows-only environment marker if its packaging supports that installation pattern.

3. **Port client startup and lifecycle**
   - Update `core/bot.py` for Kurigram client construction, start/stop lifecycle, and event-loop setup.
   - Apply the Windows event-loop integration before loop creation, while retaining the current loop on other operating systems.
   - Preserve signal handling, graceful shutdown, and worker startup behavior.

4. **Port client APIs throughout the application**
   - Migrate handler registration, filters, callback queries, buttons, conversations/login, propagation, and Telegram exceptions in `handler/`.
   - Migrate session client construction and lifecycle in `core/sessions.py`.
   - Update client and message typing/adapters in `core/worker.py`.
   - Port media retrieval, upload, download, and message/entity handling in `downloader/telegram.py` and `utils/files.py`.
   - Port error handling and send/edit helpers in `utils/telethon.py`; rename it only if the new name improves clarity without disrupting imports.
   - Search all Python sources and directly related documentation for remaining Telethon-specific references.

5. **Handle existing sessions safely**
   - Do not assume Telethon SQLite sessions are compatible with Kurigram/Pyrogram session files.
   - Verify whether a supported conversion path exists before implementing one.
   - If conversion cannot be verified, leave existing session files untouched and require users to log in again; do not silently delete or overwrite them.

6. **Update tests and documentation**
   - Adapt existing tests or add focused tests for startup, handler registration, login/session flows, media operations, and platform-specific event-loop selection.
   - Update directly related architecture and strategy documentation to identify Kurigram and the session behavior.

7. **Validate**
   - Install the updated dependencies only after dependency changes are approved and made.
   - Run focused tests, then available lint/type checks.
   - Verify dependency imports and startup/event-loop selection on Windows and a non-Windows platform where available.
   - Review the final diff for remaining Telethon imports, stale docs, and unintended session-file changes.

## Likely files to update

- `pyproject.toml`
- `core/bot.py`
- `core/sessions.py`
- `core/worker.py`
- `handler/`
- `downloader/telegram.py`
- `utils/files.py`
- `utils/telethon.py`
- Directly related documentation and tests
