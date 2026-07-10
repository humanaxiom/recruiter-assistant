"""Flask frontend — dashboard for tasks, runs, and gate status.

Talks to the FastAPI backend over HTTP; renders server-side templates.
"""

from __future__ import annotations

import os

import httpx
from flask import Flask, flash, redirect, render_template, request, url_for

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-only")
API = os.environ.get("API_BASE_URL", "http://api:8000")


@app.get("/")
def dashboard() -> str:
    # In a full build this lists tasks via a /tasks list endpoint
    return render_template("dashboard.html", api_base=API)


@app.post("/tasks/new")
def new_task():  # type: ignore[no-untyped-def]
    title = request.form.get("title", "").strip()
    spec = request.form.get("spec", "").strip()
    if len(title) < 3 or len(spec) < 10:
        flash("Title (≥3 chars) and spec (≥10 chars) required", "error")
        return redirect(url_for("dashboard"))

    try:
        resp = httpx.post(f"{API}/tasks", json={"title": title, "spec": spec}, timeout=10)
        resp.raise_for_status()
        task = resp.json()
        flash(f"Task {task['id']} queued on branch {task['branch']}", "success")
    except httpx.HTTPError as exc:
        flash(f"API error: {exc}", "error")

    return redirect(url_for("dashboard"))


@app.get("/tasks/<task_id>")
def task_detail(task_id: str) -> str:
    task = httpx.get(f"{API}/tasks/{task_id}", timeout=10).json()
    lineage = httpx.get(f"{API}/tasks/{task_id}/lineage", timeout=10).json()
    return render_template("task_detail.html", task=task, lineage=lineage)
