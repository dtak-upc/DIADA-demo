"""
Project registry: each project is a name bound to a folder of CSVs, so the
same install can browse several unrelated data sources without one
overwriting the other's computed profiles. Persisted to a small JSON file
(the registry itself - just id/name/folder per project, not the computed
data, which lives under app/storage.py keyed by project id) so the list of
projects and which one is active survive backend restarts.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from . import storage
from .config import PROJECTS_FILE


class InvalidProjectError(Exception):
    pass


class ProjectNotFoundError(Exception):
    pass


@dataclass
class Project:
    id: str
    name: str
    folder: str


def _read_registry() -> dict:
    if not PROJECTS_FILE.exists():
        return {"projects": [], "active_project_id": None}
    try:
        data = json.loads(PROJECTS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"projects": [], "active_project_id": None}
    data.setdefault("projects", [])
    data.setdefault("active_project_id", None)
    return data


def _write_registry(data: dict) -> None:
    PROJECTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROJECTS_FILE.write_text(json.dumps(data), encoding="utf-8")


def list_projects() -> list[Project]:
    return [Project(**p) for p in _read_registry()["projects"]]


def get_project(project_id: str) -> Optional[Project]:
    for p in list_projects():
        if p.id == project_id:
            return p
    return None


def get_active_project() -> Optional[Project]:
    registry = _read_registry()
    active_id = registry.get("active_project_id")
    if active_id is None:
        return None
    for p in registry["projects"]:
        if p["id"] == active_id:
            return Project(**p)
    return None


def _validate_folder(raw_path: str) -> Path:
    path = Path(raw_path).expanduser().resolve()
    if not path.is_dir():
        raise InvalidProjectError(f"'{path}' is not a directory")
    if not any(path.glob("*.csv")):
        raise InvalidProjectError(f"No CSV files found in '{path}'")
    return path


def create_project(name: str, folder: str) -> Project:
    name = name.strip()
    if not name:
        raise InvalidProjectError("Project name can't be empty")
    path = _validate_folder(folder)

    project = Project(id=uuid.uuid4().hex[:12], name=name, folder=str(path))
    registry = _read_registry()
    registry["projects"].append(asdict(project))
    registry["active_project_id"] = project.id
    _write_registry(registry)
    return project


def set_active_project(project_id: str) -> Project:
    project = get_project(project_id)
    if project is None:
        raise ProjectNotFoundError(f"No project with id '{project_id}'")
    registry = _read_registry()
    registry["active_project_id"] = project_id
    _write_registry(registry)
    return project


def delete_project(project_id: str) -> None:
    registry = _read_registry()
    remaining = [p for p in registry["projects"] if p["id"] != project_id]
    if len(remaining) == len(registry["projects"]):
        raise ProjectNotFoundError(f"No project with id '{project_id}'")
    registry["projects"] = remaining
    if registry.get("active_project_id") == project_id:
        registry["active_project_id"] = None
    _write_registry(registry)
    storage.clear_project_data(project_id)
