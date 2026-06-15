"""
tokenctl - visualizador de uso de assistentes de IA (Claude Code + Codex).

Lê os transcripts locais de cada ferramenta, normaliza num registro único e
calcula o *valor equivalente em API* (quanto custaria pago por uso) por
provedor, modelo, projeto, dia e máquina.

Fontes (cada registro recebe um `uid` estável para deduplicação):
    - Claude Code: $CLAUDE_PROJECTS_DIR ou ~/.claude/projects/**/*.jsonl
    - Codex CLI:   $CODEX_SESSIONS_DIR ou ~/.codex/sessions/**/rollout-*.jsonl
    - Exports de outras máquinas: $DATA_DIR ou ./data/*.jsonl

Comandos:
    uv run app.py serve              # sobe o dashboard (padrão)
    uv run app.py export [arquivo]   # exporta um resumo enxuto desta máquina

Para juntar várias máquinas, copie os arquivos exportados (`usage-<host>.jsonl`)
para o diretório `DATA_DIR` (padrão `./data`) da máquina que roda o dashboard -
por scp, Syncthing, pen drive, etc. O viewer mescla e deduplica tudo sozinho.

Segurança/privacidade: roda 100% local (servidor em 127.0.0.1) e sem telemetria.
Só lê metadados de uso - provedor, modelo, data/hora, tokens e o nome da pasta do
projeto (mais hostname e id de sessão, para agrupar). NUNCA lê o conteúdo das
conversas, chaves de API, e-mail nem id de conta/usuário.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

# --------------------------------------------------------------------------- #
# Preços - USD por 1 milhão de tokens.
# --------------------------------------------------------------------------- #
# Anthropic: (input, output). Cache: leitura 0.1x | escrita 5m 1.25x | 1h 2x.
ANTHROPIC_PRICING = {
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-opus-4-1": (15.0, 75.0),
    "claude-opus-4-0": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-sonnet-4-0": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-3-5-haiku-20241022": (0.80, 4.0),
}
ANTHROPIC_DEFAULT = (5.0, 25.0)

# OpenAI: (input, output, cached_input).
OPENAI_PRICING = {
    "gpt-5.5": (5.0, 30.0, 0.50),
    "gpt-5.5-pro": (30.0, 180.0, 3.0),
    "gpt-5.4": (2.50, 15.0, 0.25),
    "gpt-5.4-mini": (0.75, 4.50, 0.075),
    "gpt-5.4-nano": (0.20, 1.25, 0.02),
    # modelo interno de review do Codex - sem preço público; aproximado por gpt-5.5
    "codex-auto-review": (5.0, 30.0, 0.50),
}
OPENAI_DEFAULT = (5.0, 30.0, 0.50)


def _match(model: str, table: dict):
    if model in table:
        return table[model]
    for key, val in table.items():
        if model.startswith(key):
            return val
    return None


def anthropic_cost(model, inp, out, cache_read, cache_5m, cache_1h) -> float:
    p_in, p_out = _match(model, ANTHROPIC_PRICING) or ANTHROPIC_DEFAULT
    return (inp * p_in + out * p_out + cache_read * p_in * 0.10
            + cache_5m * p_in * 1.25 + cache_1h * p_in * 2.0) / 1_000_000


def openai_cost(model, uncached_in, out, cached_in) -> float:
    p_in, p_out, p_cached = _match(model, OPENAI_PRICING) or OPENAI_DEFAULT
    return (uncached_in * p_in + out * p_out + cached_in * p_cached) / 1_000_000


# --------------------------------------------------------------------------- #
# Diretórios das fontes
# --------------------------------------------------------------------------- #
def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_PROJECTS_DIR",
                               Path.home() / ".claude" / "projects"))


def codex_dir() -> Path:
    return Path(os.environ.get("CODEX_SESSIONS_DIR",
                               Path.home() / ".codex" / "sessions"))


def data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", Path(__file__).parent / "data"))


def _iter_lines(path: Path):
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
    except OSError:
        return


# --------------------------------------------------------------------------- #
# Parsers → registro unificado
#   {provider, model, ts, project, session, machine,
#    input, output, cache_read, cache_write, tokens, cost, uid}
# --------------------------------------------------------------------------- #
def iter_claude_records(machine: str):
    d = claude_dir()
    if not d.is_dir():
        return
    for path in d.rglob("*.jsonl"):
        for obj in _iter_lines(path):
            if obj.get("type") != "assistant":
                continue
            msg = obj.get("message") or {}
            usage = msg.get("usage") or {}
            model = msg.get("model") or ""
            if not usage or not model or model == "<synthetic>":
                continue
            cc = usage.get("cache_creation") or {}
            inp = int(usage.get("input_tokens") or 0)
            out = int(usage.get("output_tokens") or 0)
            cr = int(usage.get("cache_read_input_tokens") or 0)
            c5 = int(cc.get("ephemeral_5m_input_tokens") or 0)
            c1 = int(cc.get("ephemeral_1h_input_tokens") or 0)
            cw = c5 + c1
            mid, rid = msg.get("id"), obj.get("requestId")
            uid = f"cc:{mid}:{rid}" if mid or rid else f"cc:{obj.get('uuid')}"
            yield {
                "provider": "claude", "model": model,
                "ts": obj.get("timestamp"),
                "project": Path(obj.get("cwd", "")).name or "(?)",
                "session": obj.get("sessionId"), "machine": machine,
                "input": inp, "output": out,
                "cache_read": cr, "cache_write": cw,
                "tokens": inp + out + cr + cw,
                "cost": anthropic_cost(model, inp, out, cr, c5, c1),
                "uid": uid,
            }


def iter_codex_records(machine: str):
    d = codex_dir()
    if not d.is_dir():
        return
    for path in d.rglob("rollout-*.jsonl"):
        model = "gpt-5.5"
        session = path.stem
        project = "(?)"
        for obj in _iter_lines(path):
            payload = obj.get("payload") or {}
            ptype = obj.get("type")
            if ptype == "session_meta":
                session = payload.get("id") or session
                project = Path(payload.get("cwd", "")).name or project
            elif ptype == "turn_context":
                model = payload.get("model") or model
                project = Path(payload.get("cwd", "")).name or project
            elif ptype == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info") or {}
                last = info.get("last_token_usage") or {}
                if not last:
                    continue
                total_in = int(last.get("input_tokens") or 0)
                cached = int(last.get("cached_input_tokens") or 0)
                uncached = max(0, total_in - cached)
                out = int(last.get("output_tokens") or 0)
                if total_in == 0 and out == 0:
                    continue
                grand = ((info.get("total_token_usage") or {})
                         .get("total_tokens") or 0)
                yield {
                    "provider": "codex", "model": model,
                    "ts": obj.get("timestamp"),
                    "project": project, "session": session, "machine": machine,
                    "input": uncached, "output": out,
                    "cache_read": cached, "cache_write": 0,
                    "tokens": total_in + out,
                    "cost": openai_cost(model, uncached, out, cached),
                    "uid": f"cx:{session}:{obj.get('timestamp')}:{grand}",
                }


def local_dt(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def local_date(ts: str | None) -> str:
    dt = local_dt(ts)
    return dt.date().isoformat() if dt else "?"


# --------------------------------------------------------------------------- #
# Carregamento + deduplicação
# --------------------------------------------------------------------------- #
def load_records() -> list[dict]:
    machine = socket.gethostname()
    seen: set = set()
    records: list[dict] = []

    def add(rec: dict | None):
        if not rec:
            return
        uid = rec.get("uid") or id(rec)
        if uid in seen:
            return
        seen.add(uid)
        records.append(rec)

    # fontes ao vivo desta máquina
    for rec in iter_claude_records(machine):
        add(rec)
    for rec in iter_codex_records(machine):
        add(rec)

    # exports (unificados) de outras máquinas
    dd = data_dir()
    if dd.is_dir():
        for path in dd.rglob("*.jsonl"):
            for obj in _iter_lines(path):
                if "uid" in obj and "cost" in obj:
                    add(obj)

    return records


# --------------------------------------------------------------------------- #
# Agregação
# --------------------------------------------------------------------------- #
FIELDS = ("input", "output", "cache_read", "cache_write", "tokens", "cost")


def compute_periods(records: list[dict]) -> dict:
    """Totais para a sessão atual, o dia de hoje e a semana corrente.

    A "sessão atual" é aquela com atividade mais recente (maior timestamp).
    A semana começa no domingo, igual ao heatmap.
    """
    now = datetime.now().astimezone()
    today = now.date()
    week_start = today - timedelta(days=(today.weekday() + 1) % 7)

    # sessão com atividade mais recente
    sess_last: dict = {}
    for r in records:
        s, dt = r.get("session"), local_dt(r.get("ts"))
        if s and dt and (s not in sess_last or dt > sess_last[s]):
            sess_last[s] = dt
    cur_session = max(sess_last, key=sess_last.get) if sess_last else None

    acc = {p: {**{f: 0.0 for f in FIELDS}, "messages": 0, "_sessions": set()}
           for p in ("session", "today", "week")}

    def add(p: str, r: dict, s):
        b = acc[p]
        for f in FIELDS:
            b[f] += r.get(f, 0)
        b["messages"] += 1
        if s:
            b["_sessions"].add(s)

    for r in records:
        s, dt = r.get("session"), local_dt(r.get("ts"))
        d = dt.date() if dt else None
        if s and s == cur_session:
            add("session", r, s)
        if d == today:
            add("today", r, s)
        if d is not None and week_start <= d <= today:
            add("week", r, s)

    out = {}
    for p, b in acc.items():
        out[p] = {f: b[f] for f in FIELDS}
        out[p]["messages"] = b["messages"]
        out[p]["sessions"] = len(b["_sessions"])
    return out


def aggregate(records: list[dict]) -> dict:
    totals = defaultdict(float)
    stores = {k: {} for k in
              ("daily", "by_model", "by_project", "by_machine", "by_provider")}
    sessions: set = set()

    def bucket(store: dict, key) -> dict:
        if key not in store:
            store[key] = defaultdict(float)
            store[key]["_sessions"] = set()
        return store[key]

    for r in records:
        sess = r.get("session")
        if sess:
            sessions.add(sess)
        for f in FIELDS:
            totals[f] += r.get(f, 0)
        totals["messages"] += 1

        keys = {
            "daily": local_date(r.get("ts")),
            "by_model": f"{r['model']}",
            "by_project": r.get("project") or "(?)",
            "by_machine": r.get("machine") or "?",
            "by_provider": r.get("provider") or "?",
        }
        for name, key in keys.items():
            b = bucket(stores[name], key)
            for f in FIELDS:
                b[f] += r.get(f, 0)
            b["messages"] += 1
            if sess:
                b["_sessions"].add(sess)

    def serialize(store, name, sort_key=None):
        rows = []
        for key, b in store.items():
            row = {name: key, "sessions": len(b["_sessions"])}
            row.update({k: v for k, v in b.items() if k != "_sessions"})
            rows.append(row)
        rows.sort(key=sort_key or (lambda x: x[name]))
        return rows

    by_cost = lambda x: -x["cost"]
    return {
        "totals": {**totals, "sessions": len(sessions),
                   "days": len([k for k in stores["daily"] if k != "?"]),
                   "providers": len(stores["by_provider"])},
        "periods": compute_periods(records),
        "daily": serialize(stores["daily"], "date"),
        "by_model": serialize(stores["by_model"], "model", by_cost),
        "by_project": serialize(stores["by_project"], "project", by_cost),
        "by_machine": serialize(stores["by_machine"], "machine", by_cost),
        "by_provider": serialize(stores["by_provider"], "provider", by_cost),
    }


# --------------------------------------------------------------------------- #
# Export (registros unificados desta máquina)
# --------------------------------------------------------------------------- #
def cmd_export(outfile: str | None):
    machine = socket.gethostname()
    out = Path(outfile) if outfile else (data_dir() / f"usage-{machine}.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    seen: set = set()
    with out.open("w", encoding="utf-8") as fh:
        for rec in (*iter_claude_records(machine), *iter_codex_records(machine)):
            uid = rec.get("uid")
            if uid in seen:
                continue
            seen.add(uid)
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    print(f"Exportados {n} registros desta máquina ({machine}) -> {out}")
    return out


# --------------------------------------------------------------------------- #
# Servidor web
# --------------------------------------------------------------------------- #
def build_app():
    from fastapi import Body, FastAPI
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles

    app = FastAPI(title="tokenctl")
    static = Path(__file__).parent / "static"

    @app.get("/api/summary")
    def summary():
        # reparseia os transcripts a cada request - novos usos aparecem sozinhos
        return JSONResponse(aggregate(load_records()))

    @app.post("/api/upload")
    def upload(files: list = Body(default=[], embed=True)):
        # recebe exports de outras máquinas e os grava em DATA_DIR; o viewer
        # passa a lê-los e mesclá-los junto com os desta máquina.
        dd = data_dir()
        dd.mkdir(parents=True, exist_ok=True)
        saved, skipped = [], []
        for item in files:
            if not isinstance(item, dict):
                continue
            name = Path(str(item.get("name") or "")).name
            content = str(item.get("content") or "")
            if not name.endswith(".jsonl") or not content.strip():
                skipped.append(name or "(sem nome)")
                continue
            (dd / name).write_text(content, encoding="utf-8")
            saved.append(name)
        return JSONResponse({"saved": saved, "skipped": skipped})

    @app.get("/")
    def index():
        return FileResponse(static / "index.html")

    app.mount("/", StaticFiles(directory=static), name="static")
    return app


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "export":
        cmd_export(sys.argv[2] if len(sys.argv) > 2 else None)
    elif cmd == "serve":
        import uvicorn
        host = os.environ.get("HOST", "127.0.0.1")
        port = int(os.environ.get("PORT", "8888"))
        print(f"Dashboard em http://{host}:{port}")
        uvicorn.run(build_app(), host=host, port=port)
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
