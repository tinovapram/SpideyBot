"""The download pipeline: resolve a link, fetch the bytes, hand files to the delivery layer.

Named `downloader/` rather than `download/` so the code that downloads is never confused with
`data/downloads/`, where the fetched bytes actually land.

A real package rather than an implicit namespace one, matching `app/`, `handlers/` and
`telegram/`. A namespace package loses to any same-named regular package on `sys.path`, which is
exactly how `tests.fakes` resolved into site-packages and failed to import.

Layout follows the plan's P3 section: engines and the registry at this level, per-site adapters in
`sites/`, and shortener families in `bypass/`.
"""
