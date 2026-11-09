"""Loopback authenticated read-only projection; layout is the only UI write."""

import base64
import hashlib
import hmac
import json
import math
import secrets
import time
from pathlib import Path

from quart import Quart, request, jsonify, send_from_directory

from .decay import DAY, state_at


def ensure_auth(root, owner):
    """Local credential helper stores password DPAPI encrypted, never in frontend."""
    import win32crypt

    path = Path(root) / "config/memory_ui_auth.json"
    if not path.exists():
        password, salt = secrets.token_urlsafe(20), secrets.token_bytes(16)
        key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 300000)
        encrypted = win32crypt.CryptProtectData(
            password.encode(), "Dafeiyu memory panel", None, None, None, 0
        )
        path.write_text(
            json.dumps(
                {
                    "viewer_id": owner,
                    "salt": salt.hex(),
                    "hash": key.hex(),
                    "password_dpapi": base64.b64encode(encrypted).decode(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    auth = json.loads(path.read_text(encoding="utf-8"))
    if auth["viewer_id"] != owner:
        raise ValueError("Memory UI viewer identity mismatch")
    return auth


def create_app(services, auth, ui_root, *, port=6200, demo_mode=False):
    app = Quart("dafeiyu_memory_ui", static_folder=None)
    app.config.update(MAX_CONTENT_LENGTH=256 * 1024, PROPAGATE_EXCEPTIONS=False)
    sessions, attempts = {}, []
    origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    ui_root = Path(ui_root)

    @app.before_request
    async def authorize():
        if request.host not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            return jsonify(error="invalid_host"), 403
        if (
            request.method in {"PUT", "POST", "DELETE"}
            and request.headers.get("Origin") not in origins
        ):
            return jsonify(error="invalid_origin"), 403
        if request.path in {"/", "/login"} or request.path.startswith("/assets/"):
            return None
        cookie = request.cookies.get("dafeiyu_memory_session", "")
        session = sessions.get(cookie)
        if not session or session["expires"] <= time.time():
            sessions.pop(cookie, None)
            return jsonify(error="authentication_required"), 401
        if request.method != "GET" and not hmac.compare_digest(
            request.headers.get("X-CSRF-Token", ""), session["csrf"]
        ):
            return jsonify(error="invalid_csrf"), 403

    @app.after_request
    async def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    def viewer():
        return sessions[request.cookies["dafeiyu_memory_session"]]["viewer"]

    def service(scope):
        if viewer() != auth["viewer_id"] or scope not in services:
            return None
        return services[scope]

    @app.get("/")
    async def index():
        if demo_mode:
            return (
                (ui_root / "index.html")
                .read_text(encoding="utf-8")
                .replace("<header>", "<header><strong>演示数据，未接入机器人</strong>")
            )
        return await send_from_directory(ui_root, "index.html")

    @app.get("/assets/<path:name>")
    async def assets(name):
        if name not in {"app.js", "style.css", "vendor/cytoscape.min.js"}:
            return jsonify(error="not_found"), 404
        return await send_from_directory(ui_root, name)

    @app.post("/login")
    async def login():
        now = time.time()
        attempts[:] = [stamp for stamp in attempts if now - stamp < 60]
        if len(attempts) >= 10:
            return jsonify(error="login_rate_limited"), 429
        data = await request.get_json(silent=True) or {}
        password = data.get("password")
        if (
            not isinstance(password, str)
            or len(password) > 200
            or data.get("viewer_id") != auth["viewer_id"]
        ):
            attempts.append(now)
            return jsonify(error="invalid_credentials"), 401
        key = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(auth["salt"]), 300000
        )
        if not hmac.compare_digest(key.hex(), auth["hash"]):
            attempts.append(now)
            return jsonify(error="invalid_credentials"), 401
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        sessions[token] = {
            "viewer": auth["viewer_id"],
            "csrf": csrf,
            "expires": now + 8 * 3600,
        }
        response = jsonify(ok=True)
        response.set_cookie(
            "dafeiyu_memory_session",
            token,
            httponly=True,
            samesite="Strict",
            max_age=8 * 3600,
            path="/",
        )
        return response

    @app.get("/memory-ui/session")
    async def session_info():
        session = sessions[request.cookies["dafeiyu_memory_session"]]
        return jsonify(viewer_id=session["viewer"], csrf=session["csrf"])

    @app.post("/logout")
    async def logout():
        sessions.pop(request.cookies.get("dafeiyu_memory_session"), None)
        response = jsonify(ok=True)
        response.delete_cookie("dafeiyu_memory_session")
        return response

    @app.get("/memory-ui/scopes")
    async def scopes():
        first = next(iter(services.values()), None)
        preference = (
            first.repo.one(
                "SELECT last_scope FROM mv2_viewer_prefs WHERE viewer_id=? AND bot_id=?",
                (viewer(), first.bot_id),
            )
            if first
            else None
        )
        return jsonify(
            default_scope=preference["last_scope"]
            if preference and preference["last_scope"] in services
            else next(iter(services), None),
            scopes=[
                {
                    "id": scope,
                    "label": "群 " + scope.rsplit(":", 1)[-1]
                    if ":group:" in scope
                    else "主人私聊",
                }
                for scope in services
            ]
            if viewer() == auth["viewer_id"]
            else [],
        )

    @app.put("/memory-ui/preferences")
    async def preferences():
        data = await request.get_json(silent=True) or {}
        if not isinstance(data, dict) or set(data) != {"scope_id"}:
            return jsonify(error="invalid_preferences"), 400
        memory = service(data["scope_id"])
        if not memory:
            return jsonify(error="not_found"), 404
        with memory.repo.transaction():
            memory.db.execute(
                "INSERT OR REPLACE INTO mv2_viewer_prefs VALUES(?,?,?,?)",
                (viewer(), memory.bot_id, memory.scope, memory.now()),
            )
        return jsonify(ok=True)

    def graph_data(memory, *, neighbors_of=None):
        try:
            days = int(request.args.get("future_days", "0"))
            cursor = request.args.get("cursor", "")
            revision = memory.repo.graph_revision(memory.scope)
            offset = 0
            if cursor:
                version, offset = [int(x) for x in cursor.split(":")]
                if version != revision:
                    return {"error": "revision_changed"}, 409
            if not 0 <= days <= 180 or not 0 <= offset <= 20000:
                raise ValueError()
        except (ValueError, TypeError):
            return {"error": "invalid_projection_or_cursor"}, 400
        layer, status, subject = (
            request.args.get(key, "") for key in ["layer", "status", "subject"]
        )
        query = request.args.get("search", "")[:100].casefold()
        rows = memory.repo.rows(
            "SELECT * FROM mv2_memories WHERE scope_id=? AND status!='retracted' ORDER BY id",
            (memory.scope,),
        )
        if neighbors_of:
            if not memory.get(neighbors_of):
                return {"error": "not_found"}, 404
            ids = {neighbors_of}
            for edge in memory.repo.rows(
                "SELECT src,dst FROM mv2_edges WHERE scope_id=? AND (src=? OR dst=?) LIMIT 24",
                (memory.scope, neighbors_of, neighbors_of),
            ):
                ids.update(edge.values())
            rows = [row for row in rows if row["id"] in ids]
        visible = []
        for row in rows:
            if not row["value"] or state_at(row, memory.now()) == "expired":
                continue
            node = memory.project(row, memory.now() + days * DAY)
            if (
                not memory.permitted(node["subjects"])
                or (layer and node["layer"] != layer)
                or (status and node["status_now"] != status)
                or (subject and subject not in node["subjects"])
                or (
                    query
                    and query not in row["summary"].casefold()
                    and query not in row["value"].casefold()
                )
            ):
                continue
            visible.append(node)
        nodes = visible[offset : offset + 300]
        ids = {node["id"] for node in nodes}
        edges = []
        for edge in memory.repo.rows(
            "SELECT * FROM mv2_edges WHERE scope_id=?", (memory.scope,)
        ):
            if edge["src"] in ids and edge["dst"] in ids:
                weight = edge["weight"] * 2 ** (
                    -max(0, memory.now() + days * DAY - edge["anchor"])
                    / (edge["half_life"] * DAY)
                )
                if weight >= 0.1:
                    edges.append(
                        {
                            "id": "a:" + edge["src"] + ":" + edge["dst"],
                            "source": edge["src"],
                            "target": edge["dst"],
                            "type": edge["type"],
                            "edge_class": "association",
                            "weight": weight,
                        }
                    )
        for link in memory.repo.rows(
            "SELECT d.* FROM mv2_dependencies d JOIN mv2_memories m ON m.id=d.parent_id WHERE m.scope_id=?",
            (memory.scope,),
        ):
            if link["parent_id"] in ids and link["child_id"] in ids:
                edges.append(
                    {
                        "id": "d:" + link["parent_id"] + ":" + link["child_id"],
                        "source": link["parent_id"],
                        "target": link["child_id"],
                        "type": link["relation_type"],
                        "edge_class": "structure",
                        "weight": 1,
                    }
                )
        c = memory.control()
        if revision != memory.repo.graph_revision(memory.scope):
            return {"error": "revision_changed"}, 409
        return {
            "schema_version": 2,
            "graph_revision": revision,
            "scope_epoch": c["epoch"],
            "policy_version": c["policy_version"],
            "generated_at": memory.now(),
            "projection_days": days,
            "nodes": [
                {
                    "id": n["id"],
                    "node_type": "memory",
                    "memory_revision": n["revision"],
                    "layer": n["layer"],
                    "status": n["status_now"],
                    "label": n["summary"][:60],
                    "strength_now": n["strength_now"],
                    "half_life_days": n["half_life_days"],
                    "importance": n["importance"],
                    "protected": bool(n["protected"]),
                    "expires_at": n["expires_at"],
                    "subject_id": n["subject_id"],
                }
                for n in nodes
            ],
            "edges": edges[:900],
            "truncated": len(visible) > offset + 300 or len(edges) > 900,
            "next_cursor": f"{revision}:{offset + 300}"
            if len(visible) > offset + 300
            else None,
            "association_status": "built"
            if any(e["edge_class"] == "association" for e in edges)
            else "联想边尚未构建",
        }, 200

    @app.get("/memory-ui/graph")
    async def graph():
        memory = service(request.args.get("scope_id"))
        return (jsonify(error="not_found"), 404) if not memory else graph_data(memory)

    @app.get("/memory-ui/memories/<mid>")
    async def detail(mid):
        memory = service(request.args.get("scope_id"))
        data = memory.explain(viewer(), mid, admin=True) if memory else None
        if (
            not data
            or data["status_now"] == "expired"
            or not memory.permitted(data["subjects"])
        ):
            return jsonify(error="not_found"), 404
        data.update(
            scope_epoch=memory.control()["epoch"],
            graph_revision=memory.repo.graph_revision(memory.scope),
        )
        return jsonify(data)

    @app.get("/memory-ui/neighbors/<mid>")
    async def neighbors(mid):
        memory = service(request.args.get("scope_id"))
        return (
            (jsonify(error="not_found"), 404)
            if not memory
            else graph_data(memory, neighbors_of=mid)
        )

    @app.get("/memory-ui/traces")
    async def traces():
        memory = service(request.args.get("scope_id"))
        return (
            (jsonify(error="not_found"), 404)
            if not memory
            else jsonify(traces=memory.traces())
        )

    @app.get("/memory-ui/updates")
    async def updates():
        memory = service(request.args.get("scope_id"))
        if not memory:
            return jsonify(error="not_found"), 404
        return jsonify(
            graph_revision=memory.repo.graph_revision(memory.scope), **memory.snapshot()
        )

    @app.route("/memory-ui/layout", methods=["GET", "PUT"])
    async def layout():
        data = await request.get_json(silent=True) if request.method == "PUT" else {}
        scope = (
            (data or {}).get("scope_id")
            if request.method == "PUT"
            else request.args.get("scope_id")
        )
        memory = service(scope)
        if not memory:
            return jsonify(error="not_found"), 404
        if request.method == "GET":
            row = memory.repo.one(
                "SELECT layout FROM mv2_layouts WHERE viewer_id=? AND scope_id=? AND version=1",
                (viewer(), scope),
            )
            return jsonify(layout=json.loads(row["layout"]) if row else None)
        if not isinstance(data, dict) or set(data) - {
            "scope_id",
            "positions",
            "zoom",
            "pan",
            "filters",
        }:
            return jsonify(error="invalid_layout"), 400
        try:
            positions = data.get("positions", {})
            if not isinstance(positions, dict) or len(positions) > 1000:
                raise ValueError()
            for mid, position in positions.items():
                row = memory.get(mid)
                if (
                    not row
                    or not row["value"]
                    or state_at(row, memory.now()) in {"expired", "retracted"}
                    or not memory.permitted(memory.project(row)["subjects"])
                    or set(position) != {"x", "y", "locked"}
                    or not isinstance(position["locked"], bool)
                ):
                    raise ValueError()
                if any(
                    isinstance(position[key], bool)
                    or not isinstance(position[key], (int, float))
                    or not math.isfinite(position[key])
                    or abs(position[key]) > 1e6
                    for key in ["x", "y"]
                ):
                    raise ValueError()
            zoom = data.get("zoom", 1)
            pan = data.get("pan", {"x": 0, "y": 0})
            if (
                isinstance(zoom, bool)
                or not isinstance(zoom, (int, float))
                or not 0.05 <= zoom <= 10
                or set(pan) != {"x", "y"}
                or any(
                    not isinstance(v, (int, float))
                    or not math.isfinite(v)
                    or abs(v) > 1e6
                    for v in pan.values()
                )
            ):
                raise ValueError()
            filters = data.get("filters", {})
            if (
                not isinstance(filters, dict)
                or set(filters) - {"layer", "status"}
                or any(not isinstance(v, str) or len(v) > 20 for v in filters.values())
            ):
                raise ValueError()
        except (TypeError, ValueError):
            return jsonify(error="invalid_layout"), 400
        with memory.repo.transaction():
            memory.db.execute(
                "INSERT OR REPLACE INTO mv2_layouts VALUES(?,?,1,?)",
                (viewer(), scope, json.dumps(data)),
            )
        return jsonify(ok=True)

    @app.get("/memory-ui/voice")
    async def voice_status():
        memory = service(request.args.get("scope_id"))
        voice = getattr(memory, "voice_coordinator", None) if memory else None
        if not voice:
            return jsonify(ready=False, reason="voice_not_configured")
        return jsonify(
            **await voice.status(),
            mode=voice.options.get("mode"),
            last_error=voice.last_error,
        )

    @app.post("/memory-ui/voice/preview")
    async def voice_preview():
        data = await request.get_json()
        memory = service(data.get("scope_id"))
        voice = getattr(memory, "voice_coordinator", None) if memory else None
        if not voice or viewer() != auth["viewer_id"]:
            return jsonify(error="not_available"), 403
        snapshot = memory.snapshot()
        text = data.get("text", "")
        if not isinstance(text, str) or not 1 <= len(text) <= 200:
            return jsonify(error="invalid_text"), 400
        asset = await voice.prepare(
            "local-preview", text, viewer(), lambda: memory.unchanged(snapshot)
        )
        if not asset:
            return jsonify(error="voice_not_ready"), 503
        return jsonify(
            audio_url="/memory-ui/voice/audio/"
            + asset["job"]
            + "?scope_id="
            + memory.scope
        )

    @app.get("/memory-ui/voice/audio/<job>")
    async def voice_audio(job):
        from quart import send_file

        memory = service(request.args.get("scope_id"))
        voice = getattr(memory, "voice_coordinator", None) if memory else None
        if not voice or len(job) != 32 or not all(c in "0123456789abcdef" for c in job):
            return jsonify(error="not_found"), 404
        voice.purge()
        row = voice.db.execute(
            "SELECT path,snapshot,state,created FROM voice_jobs WHERE id=? AND scope=?",
            (job, memory.scope),
        ).fetchone()
        if (
            not row
            or not row[0]
            or row[2] in {"cancelled", "failed", "expired"}
            or not memory.unchanged(json.loads(row[1]))
        ):
            return jsonify(error="expired"), 410
        path = Path(row[0])
        if path.parent.resolve() != voice.directory.resolve() or path.is_symlink():
            return jsonify(error="invalid_asset"), 403
        return await send_file(path, mimetype="audio/wav")

    return app
