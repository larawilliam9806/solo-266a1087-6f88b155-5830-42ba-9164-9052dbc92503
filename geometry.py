"""展板露出核查的核心几何计算。

全程只操作矩形列表（扫描线求并集面积、矩形差集），
内存占用随矩形数量增长，与展板像素尺寸无关。

坐标约定：原点左上，x 向右，y 向下；矩形为半开区间 [x0, x1) x [y0, y1)。
"""

from collections import defaultdict


def _as_int(v, name):
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError(f"{name} 必须是整数，得到：{v!r}")
    return v


def _parse_rect(d, name):
    """解析 {"x","y","w"/"width","h"/"height"} 为 (x0, y0, x1, y1)。"""
    if not isinstance(d, dict):
        raise ValueError(f"{name} 必须是对象")
    try:
        x = _as_int(d["x"], f"{name}.x")
        y = _as_int(d["y"], f"{name}.y")
    except KeyError as e:
        raise ValueError(f"{name} 缺少字段 {e}")
    w = d.get("w", d.get("width"))
    h = d.get("h", d.get("height"))
    w = _as_int(w, f"{name}.w")
    h = _as_int(h, f"{name}.h")
    if w <= 0 or h <= 0:
        raise ValueError(f"{name} 的宽高必须为正整数，得到 w={w}, h={h}")
    return (x, y, x + w, y + h)


def clip(rect, bounds):
    x0, y0, x1, y1 = rect
    bx0, by0, bx1, by1 = bounds
    nx0, ny0 = max(x0, bx0), max(y0, by0)
    nx1, ny1 = min(x1, bx1), min(y1, by1)
    if nx0 >= nx1 or ny0 >= ny1:
        return None
    return (nx0, ny0, nx1, ny1)


def intersect(a, b):
    """两个半开矩形的交集；仅边缘相碰（面积为 0）时返回 None。"""
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    if x0 >= x1 or y0 >= y1:
        return None
    return (x0, y0, x1, y1)


def parse_rect(d, name="矩形"):
    """公开的矩形解析入口，供登记/反查表单复用。"""
    return _parse_rect(d, name)


def subtract(rect, hole):
    """rect 减去 hole，返回互不重叠的矩形列表（0~4 个）。"""
    x0, y0, x1, y1 = rect
    ix0, iy0 = max(x0, hole[0]), max(y0, hole[1])
    ix1, iy1 = min(x1, hole[2]), min(y1, hole[3])
    if ix0 >= ix1 or iy0 >= iy1:
        return [rect]
    out = []
    if y0 < iy0:
        out.append((x0, y0, x1, iy0))      # 上
    if iy1 < y1:
        out.append((x0, iy1, x1, y1))      # 下
    if x0 < ix0:
        out.append((x0, iy0, ix0, iy1))    # 左
    if ix1 < x1:
        out.append((ix1, iy0, x1, iy1))    # 右
    return out


def subtract_many(rect, holes):
    pieces = [rect]
    for h in holes:
        nxt = []
        for p in pieces:
            nxt.extend(subtract(p, h))
        pieces = nxt
        if not pieces:
            break
    return pieces


def union_area(rects):
    """矩形并集面积，扫描线实现，O(n^2) 时间、O(n) 内存。"""
    events = []
    for x0, y0, x1, y1 in rects:
        events.append((x0, 1, y0, y1))
        events.append((x1, -1, y0, y1))
    events.sort()
    area = 0
    prev_x = None
    active = []
    for x, delta, y0, y1 in events:
        if prev_x is not None and x > prev_x and active:
            active.sort()
            length = 0
            cur_s, cur_e = active[0]
            for s, e in active[1:]:
                if s > cur_e:
                    length += cur_e - cur_s
                    cur_s, cur_e = s, e
                elif e > cur_e:
                    cur_e = e
            length += cur_e - cur_s
            area += length * (x - prev_x)
        if delta == 1:
            active.append((y0, y1))
        else:
            active.remove((y0, y1))
        prev_x = x
    return area


def parse_input(data):
    """校验并解析上传的 JSON，返回 (board, photos, pieces)。

    photos: [{"id", "credit", "permit": (x0,y0,x1,y1)}]，保持声明顺序。
    pieces: [{"photo", "crop": rect, "at": (ax, ay)}]，从下往上排列。
    """
    if not isinstance(data, dict):
        raise ValueError("顶层必须是 JSON 对象")

    b = data.get("board")
    if not isinstance(b, dict):
        raise ValueError("缺少 board 对象")
    W = _as_int(b.get("width"), "board.width")
    H = _as_int(b.get("height"), "board.height")
    if not (1 <= W <= 10**9) or not (1 <= H <= 10**9):
        raise ValueError("board 宽高必须是 1..10^9 的整数")
    board = (0, 0, W, H)

    raw_photos = data.get("photos")
    if not isinstance(raw_photos, list) or not raw_photos:
        raise ValueError("photos 必须是非空数组")
    photos = []
    seen = set()
    for i, p in enumerate(raw_photos):
        if not isinstance(p, dict):
            raise ValueError(f"photos[{i}] 必须是对象")
        pid = p.get("id")
        if not isinstance(pid, str) or not pid:
            raise ValueError(f"photos[{i}].id 必须是非空字符串")
        if pid in seen:
            raise ValueError(f"原片编号重复：{pid!r}")
        seen.add(pid)
        credit = p.get("credit", "")
        if not isinstance(credit, str):
            raise ValueError(f"photos[{i}].credit 必须是字符串")
        permit = _parse_rect(p.get("permit"), f"photos[{i}].permit")
        photos.append({"id": pid, "credit": credit, "permit": permit})

    raw_pieces = data.get("pieces")
    if not isinstance(raw_pieces, list):
        raise ValueError("pieces 必须是数组（从下往上排列）")
    pieces = []
    for i, pc in enumerate(raw_pieces):
        if not isinstance(pc, dict):
            raise ValueError(f"pieces[{i}] 必须是对象")
        pid = pc.get("photo")
        if pid not in seen:
            raise ValueError(f"pieces[{i}].photo 引用了未声明的原片：{pid!r}")
        crop = _parse_rect(pc.get("crop"), f"pieces[{i}].crop")
        at = pc.get("at")
        if not isinstance(at, dict):
            raise ValueError(f"pieces[{i}].at 必须是对象")
        ax = _as_int(at.get("x"), f"pieces[{i}].at.x")
        ay = _as_int(at.get("y"), f"pieces[{i}].at.y")
        pieces.append({"photo": pid, "crop": crop, "at": (ax, ay)})

    return board, photos, pieces


def _visible_by_piece(board, pieces, max_parts_per_piece=None):
    """返回 (board_rects, visible)。

    board_rects[i]：第 i 片裁剪到展板内的落版矩形（可能为 None）。
    visible[i]：第 i 片实际露出的矩形列表（展板坐标）。
    """
    board_rects = []
    for pc in pieces:
        cx0, cy0, cx1, cy1 = pc["crop"]
        ax, ay = pc["at"]
        board_rects.append(clip((ax, ay, ax + (cx1 - cx0), ay + (cy1 - cy0)), board))

    # 从最上层往下扫描：visible = 本片矩形 − 所有更上层矩形（不透明）
    covered = []
    visible = [None] * len(pieces)
    for i in range(len(pieces) - 1, -1, -1):
        r = board_rects[i]
        if r is None:
            visible[i] = []
            continue
        visible[i] = subtract_many(r, covered)
        if (max_parts_per_piece is not None
                and len(visible[i]) > max_parts_per_piece):
            raise IndexTooLarge(
                f"第 {i + 1} 片露出过于零碎（超过 {max_parts_per_piece} 个矩形）")
        covered.append(r)
    return board_rects, visible


def _rows(photos, pieces, visible):
    """按原片归集可见部分（平移回原片坐标），返回核查行。"""
    per_photo = defaultdict(list)
    for pc, vis in zip(pieces, visible):
        cx0, cy0 = pc["crop"][0], pc["crop"][1]
        ax, ay = pc["at"]
        ox, oy = ax - cx0, ay - cy0  # 原片坐标 = 展板坐标 − 偏移
        for x0, y0, x1, y1 in vis:
            per_photo[pc["photo"]].append((x0 - ox, y0 - oy, x1 - ox, y1 - oy))

    rows = []
    total_unlicensed = 0
    for ph in photos:
        rects = per_photo.get(ph["id"], [])
        exposed = union_area(rects)
        unl_rects = []
        for r in rects:
            unl_rects.extend(subtract(r, ph["permit"]))
        unlicensed = union_area(unl_rects)
        point = (unl_rects[0][0], unl_rects[0][1]) if unl_rects else None
        total_unlicensed += unlicensed
        rows.append({
            "id": ph["id"],
            "credit": ph["credit"],
            "exposed": exposed,
            "unlicensed": unlicensed,
            "point": point,
        })
    return rows, total_unlicensed


def analyze(data):
    """返回核查报告字典。"""
    board, photos, pieces = parse_input(data)
    if len(pieces) > INDEX_MAX_PIECES:
        raise IndexTooLarge(
            f"裁片数量 {len(pieces)} 超过处理上限 {INDEX_MAX_PIECES}")
    _, visible = _visible_by_piece(
        board, pieces, max_parts_per_piece=INDEX_MAX_PARTS_PER_PIECE)
    rows, total_unlicensed = _rows(photos, pieces, visible)

    clean = total_unlicensed == 0
    csv_text = None
    if clean:
        import csv
        import io
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\r\n")
        w.writerow(["编号", "署名"])
        for row in rows:
            if row["exposed"] > 0:
                w.writerow([row["id"], row["credit"]])
        csv_text = buf.getvalue()

    return {
        "board": (board[2], board[3]),
        "piece_count": len(pieces),
        "rows": rows,
        "total_exposed": sum(r["exposed"] for r in rows),
        "total_unlicensed": total_unlicensed,
        "clean": clean,
        "csv": csv_text,
    }


class IndexTooLarge(ValueError):
    """露出数据规模超出登记上限。"""


INDEX_MAX_PIECES = 5000
INDEX_MAX_PARTS_PER_PIECE = 20000
INDEX_MAX_TOTAL_PARTS = 500000


def build_snapshot(data, max_pieces=None, max_parts_per_piece=None,
                   max_total_parts=None):
    """生成登记用的送印稿快照。

    返回按片保存的露出矩形（展板坐标），供反查直接使用；
    不建立像素画布，规模只随露出矩形数量增长。

    未通过刊用核查时 ``clean`` 为 False，调用方应拒绝登记。
    """
    board, photos, pieces = parse_input(data)

    max_pieces = INDEX_MAX_PIECES if max_pieces is None else max_pieces
    max_parts_per_piece = (INDEX_MAX_PARTS_PER_PIECE
                           if max_parts_per_piece is None
                           else max_parts_per_piece)
    max_total_parts = (INDEX_MAX_TOTAL_PARTS if max_total_parts is None
                       else max_total_parts)
    if not pieces:
        raise ValueError("pieces 不能为空，无法登记没有裁片的送印稿")
    if len(pieces) > max_pieces:
        raise IndexTooLarge(
            f"裁片数量 {len(pieces)} 超过登记上限 {max_pieces}")

    board_rects, visible = _visible_by_piece(
        board, pieces, max_parts_per_piece=max_parts_per_piece)
    rows, total_unlicensed = _rows(photos, pieces, visible)

    total_parts = sum(len(parts) for parts in visible)
    if total_parts > max_total_parts:
        raise IndexTooLarge(
            f"露出数据共 {total_parts} 个矩形，超过登记上限 {max_total_parts}")

    credit_by_photo = {ph["id"]: ph["credit"] for ph in photos}
    snap_pieces = []
    for i, pc in enumerate(pieces):
        snap_pieces.append({
            "no": i + 1,
            "photo": pc["photo"],
            "credit": credit_by_photo[pc["photo"]],
            "crop": list(pc["crop"]),
            "at": list(pc["at"]),
            "board_rect": list(board_rects[i]) if board_rects[i] else None,
            "exposed": [list(r) for r in visible[i]],
        })

    return {
        "board": {"width": board[2], "height": board[3]},
        "photos": [{"id": ph["id"], "credit": ph["credit"],
                    "permit": list(ph["permit"])} for ph in photos],
        "pieces": snap_pieces,
        "rows": rows,
        "total_exposed": sum(r["exposed"] for r in rows),
        "total_unlicensed": total_unlicensed,
        "clean": total_unlicensed == 0,
    }
