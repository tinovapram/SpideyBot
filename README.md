# SpideyBot

SpideyBot is a lightweight Telegram downloader built on top of the Kurigram framework. It provides a fully‑featured **Downloader** stack, session management, quota handling, and a production‑ready CLI.

## Project Highlights

* **Short‑link bypass** – custom `RedirectAdapter` handles over‑200 known hosts.
* **Session security** – `/logout` revokes the Telegram token and cleans local files.
* **Modular architecture** – split into handlers, downloader, and CLI layers.
* **Tests** – 840 unit and integration tests that ensure API contract compliance.
* **Database** – SQLite/PostgreSQL support via Alembic migrations.
* **Docker** – ready‑to‑use Dockerfile for containerized deployment.

## Getting Started

```bash
# Create a virtual environment and install dependencies
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt

# Create the bundled PostgreSQL cluster (generates its own password)
python main.py db init

# Start it, then bring the schema to head
python main.py db start
python main.py db migrate

# Start the bot
python main.py run
```

`run` starts an existing cluster itself, so only `db migrate` needs repeating after a schema
change. It refuses to start, naming the command to run, when the cluster has not been created
or the schema is behind the migrations — both of which would otherwise surface much later as a
query that failed for no visible reason. To use an external PostgreSQL instead of the bundled
cluster, set `SPIDEY_DB_PASSWORD` and point the `[db]` section of `config/local.toml` at it;
the local cluster is then left untouched.

For a full list of DB commands use:
```
python main.py db migrate   # apply pending migrations
python main.py db status    # cluster state and the DSN in use
python main.py db reset --force   # delete the cluster and all its data
```

## Command‑Line Interface

`app.cli` exposes a set of subcommands – the main entry point is `main.py`. For example:

```bash
# Start the bot in debug mode
python main.py -d run

# Run the test suite
pytest
```

The exposed commands mirror the project plan and surface the most common bot functions.

## Running Tests

```bash
pytest -q
```

All 796 tests should pass:
```
796 passed, 59 warnings in 80.91s (0:01:20)
```

## Contributions

Feel free to open issues or pull requests. All contributions are welcome – just make sure tests continue to pass!

---

SpideyBot – The spidey‑tap.
