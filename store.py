"""送印稿登记与反查。

每份送印稿登记时由 geometry.build_snapshot 生成各裁片的露出矩形
（展板坐标），反查只使用这些矩形做相交判断，不建立像素画布，
内存与落盘体积只随露出矩形数量增长，与展板像素尺寸无关。

并发约定：
- 快照生成在锁外完成，期间旧稿照常可查、其他展板照常登记。
- 提交在锁内完成：校验页面所见稿次 → 原子写盘 → 同时换入内存。
  写盘失败则内存不变，旧稿继续生效；新稿次与露出数据一起生效。
- 查询在锁内取一份不可变的登记表副本，之后在锁外计算，
  因此结果始终对应同一时刻已生效的各份稿次。
"""

import base64
import datetime
import json
import os
import sys
import tempfile
import threading

from geometry import build_snapshot, intersect

MAX_BOARD_ID_LEN = 64


class StaleDraft(ValueError):
    """页面所见稿次已过期（或不存在时被要求覆盖登记）。"""

    def __init__(self, current_version):
        self.current_version = current_version
        super().__init__(f"页面所见稿次已过期，当前稿次为 {current_version}")


class SaveFailed(RuntimeError):
    """落盘失败，当前稿保持不变。"""


def validate_board_id(raw):
    if not isinstance(raw, str):
        raise ValueError("展板编号必须是字符串")
    bid = raw.strip()
    if not bid:
        raise ValueError("展板编号不能为空")
    if len(bid) > MAX_BOARD_ID_LEN:
        raise ValueError(f"展板编号最长 {MAX_BOARD_ID_LEN} 个字符")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in bid):
        raise ValueError("展板编号不能包含控制字符")
    return bid


def _filename(board_id):
    enc = base64.urlsafe_b64encode(board_id.encode("utf-8")).decode("ascii")
    return "board-" + enc.rstrip("=") + ".json"


def _atomic_write(path, text):
    """原子落盘：写临时文件 → fsync → rename → fsync 目录。"""
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def _photo_index(pieces):
    by_photo = {}
    for piece in pieces:
        by_photo.setdefault(piece["photo"], []).append(piece)
    return by_photo


def _write_record(path, record):
    """把一份稿次写入目标路径（先写临时文件再改名的原子替换）。"""
    text = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    _atomic_write(path, text)


class BoardStore:
    def __init__(self, data_dir, clock=None):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self._clock = clock or (lambda: datetime.datetime.now(
            datetime.timezone.utc).isoformat(timespec="seconds"))
        self._lock = threading.RLock()
        self._states = {}  # board_id -> {"record": ..., "by_photo": ...}
        self._recover()
        self._load()

    # ---------- 启动恢复 ----------

    def _recover(self):
        """清掉上次中断留下的半成品临时文件（正式文件均为原子替换）。"""
        for name in sorted(os.listdir(self.data_dir)):
            if name.startswith(".tmp-") and name.endswith(".json"):
                try:
                    os.unlink(os.path.join(self.data_dir, name))
                except OSError as e:
                    print(f"无法清理临时文件 {name}：{e}", file=sys.stderr)

    def _load(self):
        for name in sorted(os.listdir(self.data_dir)):
            if not (name.startswith("board-") and name.endswith(".json")):
                continue
            path = os.path.join(self.data_dir, name)
            try:
                with open(path, encoding="utf-8") as f:
                    record = json.load(f)
                board_id = record["board"]
                pieces = record["snapshot"]["pieces"]
                version = record["version"]
                if not isinstance(board_id, str) or not isinstance(version, int):
                    raise ValueError("字段缺失")
                if _filename(board_id) != name:
                    raise ValueError("文件与展板编号不符")
            except (OSError, ValueError, KeyError, TypeError) as e:
                print(f"跳过无法读取的登记文件 {name}：{e}", file=sys.stderr)
                continue
            self._states[board_id] = {
                "record": record,
                "by_photo": _photo_index(pieces),
            }

    # ---------- 读取 ----------

    def summaries(self):
        """按展板编号排序的登记摘要。"""
        with self._lock:
            states = list(self._states.items())
        out = []
        for board_id, state in sorted(states, key=lambda kv: kv[0]):
            snap = state["record"]["snapshot"]
            out.append({
                "board": board_id,
                "version": state["record"]["version"],
                "pieces": len(snap["pieces"]),
                "photos": len(snap["photos"]),
                "exposed": snap["total_exposed"],
                "updated_at": state["record"]["updated_at"],
            })
        return out

    def get(self, board_id):
        """当前稿次摘要；未登记返回 None。"""
        with self._lock:
            state = self._states.get(board_id)
            if state is None:
                return None
            snap = state["record"]["snapshot"]
            return {
                "board": board_id,
                "version": state["record"]["version"],
                "board_size": (snap["board"]["width"], snap["board"]["height"]),
                "pieces": len(snap["pieces"]),
                "photos": len(snap["photos"]),
                "exposed": snap["total_exposed"],
                "updated_at": state["record"]["updated_at"],
            }

    def photo_ids(self):
        with self._lock:
            states = list(self._states.values())
        ids = set()
        for state in states:
            ids.update(state["by_photo"])
        return sorted(ids)

    def snapshot_states(self):
        """同一时刻生效的全部登记（不可变副本）。"""
        with self._lock:
            return dict(self._states)

    def commit(self, board_id, expected_version, data):
        """校验并登记新稿次。

        expected_version 为页面所见稿次：新登记传 0；
        若与当前稿次不一致则抛 StaleDraft，当前稿保持不变。
        快照生成在锁外，期间查询与其他登记不受影响。
        """
        board_id = validate_board_id(board_id)
        # 先快速比对一次页面所见稿次，过期就不必做昂贵的露出计算；
        # 真正的提交仍会在锁内再校验一遍。
        with self._lock:
            current = self._states.get(board_id)
            current_version = current["record"]["version"] if current else 0
        if expected_version != current_version:
            raise StaleDraft(current_version)

        snapshot = build_snapshot(data)  # 可能较慢，锁外完成
        if not snapshot["clean"]:
            raise ValueError(
                "送印稿未通过刊用核查：存在未获许可的露出，"
                f"缺许可面积 {snapshot['total_unlicensed']} 像素²，"
                "请先在核查页修正后再登记")

        record = {
            "board": board_id,
            "version": None,
            "updated_at": None,
            "snapshot": snapshot,
        }
        with self._lock:
            current = self._states.get(board_id)
            current_version = current["record"]["version"] if current else 0
            if expected_version != current_version:
                raise StaleDraft(current_version)

            record["version"] = current_version + 1
            record["updated_at"] = self._clock()
            by_photo = _photo_index(snapshot["pieces"])
            path = os.path.join(self.data_dir, _filename(board_id))
            try:
                _write_record(path, record)
            except OSError as e:
                raise SaveFailed(f"登记文件写入失败：{e}") from e
            # 写盘成功后才换入内存，新旧稿次不会出现半生效状态
            self._states[board_id] = {"record": record, "by_photo": by_photo}
        return record["version"]


def lookup(states, photo_id, defect_rect):
    """在给定登记快照中反查原片瑕疵。

    返回逐处命中列表，每项含展板编号、稿次、片号与展板内实际露出的落版矩形。
    仅边缘相碰（交集面积为 0）的略过。
    """
    x0, y0, x1, y1 = defect_rect
    hits = []
    for board_id in sorted(states):
        state = states[board_id]
        record = state["record"]
        for piece in state["by_photo"].get(photo_id, ()):
            cx0, cy0 = piece["crop"][0], piece["crop"][1]
            ax, ay = piece["at"]
            ox, oy = ax - cx0, ay - cy0  # 原片坐标 → 展板坐标
            mapped = (x0 + ox, y0 + oy, x1 + ox, y1 + oy)
            for exp in piece["exposed"]:
                r = intersect(mapped, exp)
                if r is None:
                    continue
                hits.append({
                    "board": board_id,
                    "version": record["version"],
                    "piece_no": piece["no"],
                    "photo": photo_id,
                    "credit": piece["credit"],
                    "rect": r,
                })
    hits.sort(key=lambda h: (h["board"], h["piece_no"],
                             h["rect"][0], h["rect"][1]))
    return hits
