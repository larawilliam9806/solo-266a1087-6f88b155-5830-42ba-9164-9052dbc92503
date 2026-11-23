import json
import os
import secrets
import threading

from flask import Flask, Response, abort, redirect, render_template, request, url_for

from geometry import IndexTooLarge, analyze, parse_rect
from store import BoardStore, SaveFailed, StaleDraft, lookup, validate_board_id

DATA_DIR = os.environ.get(
    "DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 上传 JSON 上限 16MB

try:
    store = BoardStore(DATA_DIR)
except OSError as _e:  # 数据目录不可用时不静默降级，保留明确原因
    store = None
    _store_init_error = f"登记数据目录不可用（{DATA_DIR}）：{_e}"
else:
    _store_init_error = None

# 署名 CSV 暂存（仅整页无缺口时生成），容量固定，与画布像素数无关
_store = {}
_store_lock = threading.Lock()
_STORE_CAP = 32


def _reject_constant(name):
    raise ValueError(f"JSON 中不允许 {name}")


def _loads(text):
    return json.loads(text, parse_constant=_reject_constant)


def _stash(csv_text):
    token = secrets.token_urlsafe(12)
    with _store_lock:
        while len(_store) >= _STORE_CAP:
            _store.pop(next(iter(_store)))
        _store[token] = csv_text
    return token


def _form_context(**extra):
    try:
        summaries = store.summaries() if store is not None else []
    except Exception:
        summaries = []
    ctx = {
        "summaries": summaries,
        "store_error": _store_init_error,
        "error": None,
        "board_id": "",
        "expected_version": "",
        "current_version": None,
        "json_text": "",
        "mode": "register",
        "saved": None,
        "lookup_photo": "",
    }
    ctx.update(extra)
    return ctx


def _read_json_input(require=True):
    """读取 JSON：优先文件上传，其次文本域。返回 (text, data)。"""
    f = request.files.get("file")
    if f is not None and f.filename:
        try:
            raw = f.read()
        except OSError as e:
            raise ValueError(f"读取上传文件失败：{e}")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as e:
            raise ValueError(f"上传文件不是 UTF-8 编码：{e}")
    else:
        text = request.form.get("json_text", "")
        if not text.strip():
            if require:
                raise ValueError("请选择 JSON 文件，或直接在文本框中粘贴 JSON")
            return text, None
    try:
        data = _loads(text)
    except ValueError as e:
        raise ValueError(f"JSON 解析失败：{e}")
    return text, data


def _render_input_error(message, status, stale=False, **extra):
    ctx = _form_context(error=message, **extra)
    if ctx["board_id"] and store is not None:
        current = store.get(ctx["board_id"])
        if stale:
            ctx["mode"] = "stale"
        if current is not None:
            ctx["current_version"] = current["version"]
        else:
            ctx["current_version"] = None
    return render_template("index.html", **ctx), status


@app.errorhandler(413)
def too_large(_e):
    return _render_input_error("上传内容超过 16MB 上限，未做任何改动。", 413)


@app.errorhandler(500)
def internal_error(_e):
    try:
        return _render_input_error(
            "处理失败：服务器内部错误。未完成的提交不会写入任何稿次；"
            "请重新载入页面确认当前稿次后再操作。", 500)
    except Exception:
        return "<h1>处理失败</h1><p>服务器内部错误，请返回重试。</p>", 500


@app.route("/")
def index():
    board_id = request.args.get("board", "")
    ctx = _form_context()
    if board_id and store is not None:
        ctx["board_id"] = board_id
        current = store.get(board_id)
        if current is not None:
            ctx["mode"] = "edit"
            ctx["expected_version"] = current["version"]
            ctx["current_version"] = current["version"]
        else:
            ctx["expected_version"] = 0
    saved = request.args.get("saved", "")
    if saved:
        ctx["saved"] = saved
        ctx["board"] = board_id
    return render_template("index.html", **ctx)


@app.route("/analyze", methods=["POST"])
def analyze_route():
    json_text = request.form.get("json_text", "")
    try:
        json_text, data = _read_json_input()
        report = analyze(data)
    except ValueError as e:
        return _render_input_error(f"核查未执行：{e}", 400, json_text=json_text)
    token = _stash(report["csv"]) if report["clean"] else None
    return render_template("result.html", report=report, token=token,
                           json_text=json_text)


@app.route("/download/<token>")
def download(token):
    with _store_lock:
        csv_text = _store.get(token)
    if csv_text is None:
        abort(404)
    return Response(
        csv_text.encode("utf-8-sig"),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=credits.csv"},
    )


@app.route("/register", methods=["POST"])
def register():
    if store is None:
        return _render_input_error(_store_init_error, 503)
    board_id_raw = request.form.get("board_id", "")
    version_raw = request.form.get("expected_version", "")
    try:
        board_id = validate_board_id(board_id_raw)
    except ValueError as e:
        return _render_input_error(
            str(e), 400, board_id=board_id_raw.strip(), expected_version="",
            json_text=request.form.get("json_text", ""))

    try:
        expected_version = int(version_raw)
        if expected_version < 0:
            raise ValueError
    except (TypeError, ValueError):
        return _render_input_error(
            "缺少有效的页面所见稿次，请重新打开页面后再登记。", 400,
            board_id=board_id, json_text=request.form.get("json_text", ""))

    json_text = ""
    try:
        json_text, data = _read_json_input()
        version = store.commit(board_id, expected_version, data)
    except StaleDraft as e:
        return _render_input_error(
            f"页面所见稿次已过期（提交时为 {expected_version}，"
            f"当前已生效稿次为 {e.current_version}）。"
            "本次提交未生效，当前稿保持不变；"
            "请按最新稿次重新载入页面确认后再提交。",
            409, stale=True, board_id=board_id,
            expected_version=expected_version, json_text=json_text)
    except SaveFailed as e:
        return _render_input_error(
            f"保存失败：{e}。本次提交未生效，当前稿保持不变，请稍后重试。",
            500, board_id=board_id, expected_version=expected_version,
            json_text=json_text)
    except (ValueError, IndexTooLarge) as e:
        return _render_input_error(
            f"登记未通过：{e}。当前稿保持不变。", 400,
            board_id=board_id, expected_version=expected_version,
            json_text=json_text)

    return redirect(url_for("index", board=board_id, saved=version))


@app.route("/lookup", methods=["POST"])
def lookup_route():
    if store is None:
        return _render_input_error(_store_init_error, 503)
    board_filter = request.form.get("board_filter", "").strip()
    photo_id = request.form.get("photo_id", "").strip()
    raw = {key: request.form.get(key, "") for key in ("x", "y", "w", "h")}
    try:
        if not photo_id:
            raise ValueError("请填写原片编号")
        rect_d = {}
        for key, val in raw.items():
            try:
                rect_d[key] = int(val)
            except (TypeError, ValueError):
                raise ValueError(f"瑕疵矩形的 {key} 必须是整数，得到：{val!r}")
        defect = parse_rect(rect_d, "瑕疵矩形")
    except ValueError as e:
        return render_template(
            "lookup.html", hits=None, versions=[], defect=None,
            photo_id=photo_id, board_filter=board_filter,
            photo_known=False, error=f"查询未执行：{e}", raw=raw), 400

    # 一次性取同一时刻生效的登记表，之后不再读取存储
    states = store.snapshot_states()
    if board_filter:
        states = {k: v for k, v in states.items() if k == board_filter}

    hits = lookup(states, photo_id, defect)
    versions = [
        {"board": bid, "version": state["record"]["version"]}
        for bid, state in sorted(states.items())
    ]
    photo_known = any(photo_id in state["by_photo"] for state in states.values())
    return render_template(
        "lookup.html", hits=hits, versions=versions, defect=defect,
        photo_id=photo_id, board_filter=board_filter,
        photo_known=photo_known, error=None, raw=raw)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
