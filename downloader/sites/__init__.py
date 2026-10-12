"""Native per-site adapters.

Only for hosts the engines do not cover or get wrong: TeraBox, the YouTube family, the ad-gated
hosts, and the social sites whose public APIs or embed endpoints give a cleaner answer than an
engine guess. Behaviour is grounded in the previous implementation's `downloader/site/*.py`
(branch `V5`) rather than re-derived from scratch.

Two of V5's modules are deliberately not ports, both for the same reason — they add a name without
adding a capability:

- `cyberdrop.py` wrapped the `cyberdrop-dl` subprocess, which is exactly what
  `downloader/cyberdropdl.py` already is. V5 kept both because the wrapper doubled as its site
  entry; here the delegate is supplied by `registry.delegate_for` and is never a chain member (D38),
  so an adapter would only ever be a second name for the same program.
- `bluesky.py` spoke to a third-party saver site whose page no longer carries a result for any post,
  and the host is already covered: gallery-dl resolves a real Bluesky video post to its `video/mp4`
  blob and an image post to each full-size blob, with the extension in its metadata. A native
  adapter is bought where the engines fail (D6), and here they do not.
"""
