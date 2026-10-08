"""Link-shortener resolution (FR-18).

Ad-gated hosts are reached through a chain of redirectors, so this is separate from `sites/`:
resolving a shortener produces a final URL, which is then handled by whatever downloads it.
"""
