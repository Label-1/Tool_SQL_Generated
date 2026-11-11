"""Optional localhost-only Ollama adapter. Never accepts network URLs or credentials.

AI outputs a *proposed plan patch*, not executable SQL. Preview only runs SQL
compiled by the trusted visual builder after metadata validation.
"""
from __future__ import annotations
import copy
import json
import os
import re
import sys
import urllib.request
from .models import QueryPlan, Table, Projection, Relation, Filter, Sort
from .sql_builder import validate_plan

LOCAL_URL = "http://127.0.0.1:11434"
SYSTEM = """You are a Thai-language database query planning assistant inside a strictly read-only SQL builder.
Return ONLY one valid JSON object. Never output raw SQL. Never invent table IDs or column names.
You may only select tables/columns in SCHEMA. Gender M means male when the user explicitly says so.
For a fresh query choose action=build and include base, tables, joins, columns, filters.
For modifying an EXISTING query, choose action=columns_only if they only ask to change displayed columns;
choose action=filters_only if they only change filters; otherwise action=build with a complete new plan.
Prefer EXISTS for a match/existence question if displaying only columns from one table.
Valid action: build, columns_only, filters_only.
JSON keys: action, base (table ID), tables (table ID array), joins (array with left, left_column, right, right_column, kind),
columns (array of {table, column, aggregate(optional), alias(optional)}),
filters (array of {table, column, operator, value, logical(optional)}),
distinct (boolean), explanation (Thai string).
Join kind INNER, LEFT, RIGHT, or EXISTS. Operator =, !=, >, >=, <, <=, LIKE, IN, IS NULL, IS NOT NULL.
Each table ID is one of the provided table IDs, e.g. T1. Use literal column names from SCHEMA.
Do not send credentials or customer data. Unknown names require an explanation rather than a guess."""


def table_ids(tables: dict[str, Table]) -> tuple[dict[str, str], dict[str, str]]:
    items = list(tables)
    to_key = {"T" + str(i): key for i, key in enumerate(items)}
    to_id = {value: key for key, value in to_key.items()}
    return to_key, to_id


def metadata_prompt(tables: dict[str, Table], plan: QueryPlan, question: str) -> str:
    to_key, to_id = table_ids(tables)
    # Search first, then include canvas tables; never forward rows, passwords, or history from other queries.
    terms = {s.lower() for s in question.split() if len(s) > 1}
    def score(k: str) -> int:
        t = tables[k]
        haystack = (t.qualified_name + " " + " ".join(c.name for c in t.columns)).lower()
        return 5 * (k in [n.key for n in plan.nodes]) + sum(2 for term in terms if term in haystack)
    keys = sorted(tables, key=score, reverse=True)[:24]
    descriptions = [{
        "id": to_id[k], "database": tables[k].catalog, "schema": tables[k].schema, "table": tables[k].name,
        "columns": [{"name": c.name, "type": c.dtype} for c in tables[k].columns[:45]],
    } for k in keys]
    def as_ids(obj):
        if isinstance(obj, dict):
            return {a: as_ids(b) for a, b in obj.items()}
        if isinstance(obj, list):
            return [as_ids(x) for x in obj]
        return to_id.get(obj, obj) if isinstance(obj, str) else obj
    current = as_ids(plan.to_dict())
    current.pop("user_context", None)
    # Never copy long/likely identifying user-provided values into AI context.
    for f in current.get("filters", []):
        value = str(f.get("value", ""))
        if len(value) > 8 or re.search(r"\d{7,}", value):
            f["value"] = "[REDACTED_VALUE]"
    safe_question = re.sub(r"(?<!\d)\d{9,16}(?!\d)", "[REDACTED_ID]", question[:1500])
    return json.dumps({"question": safe_question, "schema": descriptions,
                       "current_query_only": current}, ensure_ascii=False)


def available_ram_gb() -> float:
    try:
        if sys.platform == "win32":
            import ctypes
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                           ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                           ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                           ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                           ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            status = MEMORYSTATUSEX()
            status.dwLength = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return float(status.ullAvailPhys) / (1024 ** 3)
        else:
            return float(os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")) / (1024 ** 3)
    except Exception:
        pass
    return 4.0  # conservative fallback


class OllamaLocalAI:
    def __init__(self, model: str = ""):
        self.model = model
        # no proxy use, no arbitrary endpoint, no cloud fallback
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(LOCAL_URL + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with self.opener.open(req, timeout=120) as res:
            return json.loads(res.read(512_000).decode("utf-8"))

    def list_models(self) -> list[str]:
        req = urllib.request.Request(LOCAL_URL + "/api/tags")
        with self.opener.open(req, timeout=3) as res:
            data = json.loads(res.read(250_000).decode("utf-8"))
        return [m["name"] for m in data.get("models", [])
                if isinstance(m.get("name"), str) and not self._is_cloud(m["name"])]

    @staticmethod
    def _is_cloud(name: str) -> bool:
        low = name.lower()
        return ":cloud" in low or low.endswith("-cloud") or "/cloud" in low

    def _choose_model(self) -> str:
        req = urllib.request.Request(LOCAL_URL + "/api/tags")
        with self.opener.open(req, timeout=3) as res:
            models = json.loads(res.read(250_000).decode("utf-8")).get("models", [])
        models = [m for m in models if isinstance(m.get("name"), str) and not self._is_cloud(m["name"])]
        if not models:
            raise RuntimeError("No local models are installed in Ollama. Install a small model first.")
        if self.model != "AUTO":
            if not any(m["name"] == self.model for m in models):
                raise PermissionError("Selected model is not installed locally. Cloud models are forbidden.")
            return self.model
        ram = available_ram_gb()
        budget_gb = max(0.4, min(8.0, (ram - 2.2) * 0.65))
        fits = [m for m in models if 0 < int(m.get("size", 0)) <= budget_gb * 1024 ** 3]
        if not fits:
            raise RuntimeError(f"No installed model fits available RAM (~{ram:.1f} GB free). Use Visual Builder or install a smaller local model.")
        return sorted(fits, key=lambda m: int(m.get("size", 0)))[0]["name"]

    def propose(self, question: str, tables: dict[str, Table], plan: QueryPlan) -> dict:
        if not self.model:
            raise RuntimeError("Choose a local model beside the AI prompt or use AUTO")
        model = self._choose_model()
        ram = available_ram_gb()
        payload = {"model": model, "prompt": metadata_prompt(tables, plan, question),
                   "system": SYSTEM, "format": "json", "stream": False,
                   "keep_alive": 0, "options": {"temperature": 0, "num_ctx": 2048 if ram < 8 else 4096,
                                             "num_predict": 900, "num_thread": min(4, os.cpu_count() or 2)}}
        result = self._post("/api/generate", payload)
        response = result.get("response", "")
        if not isinstance(response, str) or len(response) > 100_000:
            raise ValueError("Invalid Local AI response")
        patch = json.loads(response)
        if not isinstance(patch, dict):
            raise ValueError("AI response is not a query plan")
        return patch


def apply_ai_patch(current: QueryPlan, patch: dict, tables: dict[str, Table]) -> QueryPlan:
    """Strictly validate all AI table and field references; never trust model SQL."""
    if not isinstance(patch, dict):
        raise ValueError("AI plan must be an object")
    to_key, _ = table_ids(tables)
    def resolve(tid):
        if tid not in to_key:
            raise ValueError("AI referenced an unknown table ID: " + str(tid))
        return to_key[tid]
    def col(tid, cname):
        key = resolve(tid)
        if cname != "*" and not tables[key].has_column(cname):
            raise ValueError("AI referenced a column that does not exist: " + str(cname))
        return key
    action = patch.get("action")
    if action not in ("build", "columns_only", "filters_only"):
        raise ValueError("Unknown AI action")
    plan = copy.deepcopy(current)
    if action == "build":
        ids = patch.get("tables", [])
        if not isinstance(ids, list) or not ids or len(ids) > 20:
            raise ValueError("AI did not provide a valid table list")
        plan.nodes = []
        plan.joins = []
        plan.projections = []
        plan.filters = []
        plan.group_by = []
        plan.sort = None
        for i, tid in enumerate(ids):
            key = resolve(tid)
            plan.add_node(tables[key], x=65 + (i % 3) * 310, y=90 + (i // 3) * 210)
        plan.base = resolve(patch.get("base"))
        plan.projections = []
        for j in patch.get("joins", []):
            left, right = col(j["left"], j["left_column"]), col(j["right"], j["right_column"])
            plan.joins.append(Relation(left, j["left_column"], right, j["right_column"], j.get("kind", "INNER").upper()))
    if action in ("build", "columns_only"):
        selections = patch.get("columns", [])
        if not isinstance(selections, list) or not selections:
            raise ValueError("AI must select at least one output column")
        plan.projections = [Projection(col(p["table"], p["column"]), p["column"],
                                 p.get("aggregate", ""), p.get("alias", "")) for p in selections]
    if action in ("build", "filters_only"):
        fs = patch.get("filters", [])
        if not isinstance(fs, list):
            raise ValueError("AI filters must be an array")
        plan.filters = [Filter(col(f["table"], f["column"]), f["column"],
                               f.get("operator", "="), str(f.get("value", "")),
                               f.get("logical", "AND")) for f in fs]
    if action == "build":
        plan.distinct = bool(patch.get("distinct", False))
    validate_plan(plan, tables)
    return plan
