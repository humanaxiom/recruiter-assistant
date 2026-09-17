"""Pure, I/O-free helpers shared between the smoke suite and the stress/e2e
harness (``scripts/e2e.sh`` / ``scripts/stress.sh``).

Nothing in this package touches the network, Postgres, Neo4j or an LLM — see
``driver.py`` (HTML scraping), ``roster.py`` (CSV fixture generation) and
``report.py`` (percentile summaries). All I/O lives in the scripts that import
them.
"""

from __future__ import annotations
