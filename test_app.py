"""End-to-end web flow tests: python3 -I -m unittest test_app -v"""
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_tmp = tempfile.mkdtemp(prefix="board-test-")
os.environ["DATA_DIR"] = _tmp

import app as app_module  # noqa: E402

import geometry  # noqa: E402


def make():
    return {
        "board": {"width": 100, "height": 100},
        "photos": [
            {"id": "P1", "credit": "甲", "permit": {"x": 0, "y": 0, "w": 100, "h": 100}},
            {"id": "P2", "credit": "乙", "permit": {"x": 0, "y": 0, "w": 100, "h": 100}},
        ],
        "pieces": [
            {"photo": "P1", "crop": {"x": 0, "y": 0, "w": 20, "h": 20},
             "at": {"x": 10, "y": 10}},
            {"photo": "P2", "crop": {"x": 0, "y": 0, "w": 10, "h": 10},
             "at": {"x": 15, "y": 15}},
        ],
    }


class AppTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="board-case-")
        app_module.store = app_module.BoardStore(self.dir)
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()

    def register(self, board, version=0, data=None):
        payload = json.dumps(data or make()).encode("utf-8")
        return self.client.post("/register", data={
            "board_id": board,
            "expected_version": str(version),
            "file": (io.BytesIO(payload), "draft.json"),
        }, content_type="multipart/form-data",
            follow_redirects=False)

    def lookup(self, photo="P1", rect=(0, 0, 5, 5), board=""):
        return self.client.post("/lookup", data={
            "photo_id": photo,
            "x": rect[0], "y": rect[1], "w": rect[2] - rect[0],
            "h": rect[3] - rect[1], "board_filter": board})

    def test_register_redirect_and_lookup_page(self):
        r = self.register("B-1")
        self.assertEqual(r.status_code, 302)
        self.assertIn("saved=1", r.headers["Location"])
        page = self.lookup().get_data(as_text=True)
        self.assertIn("B-1", page)
        self.assertIn("稿次 1", page)
        self.assertIn("(10, 10, 5, 5)", page)

    def test_stale_page_version_shows_reason_and_keeps_draft(self):
        self.register("B-1")            # 稿次 1
        self.register("B-1", 1)         # 稿次 2，更新落版位置
        stale = json.loads(json.dumps(make()))
        stale["pieces"][0]["at"] = {"x": 40, "y": 40}
        r = self.register("B-1", 1, stale)  # 页面仍以为稿次为 1
        self.assertEqual(r.status_code, 409)
        body = r.get_data(as_text=True)
        self.assertIn("过期", body)
        self.assertIn("保持不变", body)
        self.assertIn("value=\"1\"", body)  # 保留当前稿的表单稿次
        self.assertIn("当前已生效稿次为 <b>2</b>", body)
        page = self.lookup(rect=(0, 0, 5, 5)).get_data(as_text=True)
        self.assertIn("(10, 10, 5, 5)", page)   # 当前稿仍为稿次 2 的旧位置

    def test_page_shows_saved_version_on_reload(self):
        self.register("B-1")
        page = self.client.get("/?board=B-1").get_data(as_text=True)
        self.assertIn('value="1"', page)
        self.assertIn("当前页所见稿次", page)

    def test_lookup_reflects_current_drafts(self):
        self.register("B-1")
        self.register("B-2")
        page = self.lookup(rect=(0, 0, 5, 5)).get_data(as_text=True)
        self.assertIn("B-1", page)
        self.assertIn("B-2", page)
        page = self.lookup(rect=(0, 0, 5, 5), board="B-2").get_data(as_text=True)
        self.assertIn("B-2", page)
        self.assertNotIn("B-1", page)

    def test_lookup_edge_touch_no_hit(self):
        self.register("B-1")
        page = self.lookup(rect=(20, 10, 25, 15)).get_data(as_text=True)
        self.assertIn("无需补印", page)

    def test_unlicensed_draft_rejected(self):
        bad = make()
        bad["photos"] = [{"id": "P1", "credit": "甲",
                          "permit": {"x": 0, "y": 0, "w": 5, "h": 5}},
                         bad["photos"][1]]
        r = self.register("B-1", data=bad)
        self.assertEqual(r.status_code, 400)
        self.assertIn("刊用核查", r.get_data(as_text=True))
        self.assertIsNone(app_module.store.get("B-1"))

    def test_bad_json_reports_reason(self):
        r = self.client.post("/register", data={
            "board_id": "B-1", "expected_version": "0",
            "json_text": "{not json"}, follow_redirects=False)
        self.assertEqual(r.status_code, 400)
        self.assertIn("JSON 解析失败", r.get_data(as_text=True))

    def test_analyze_then_register(self):
        text = json.dumps(make())
        r = self.client.post("/analyze", data={"json_text": text})
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("登记这份送印稿", body)
        self.assertIn("下载署名 CSV", body)

        r2 = self.client.post("/register", data={
            "board_id": "B-9", "expected_version": "0", "json_text": text},
            follow_redirects=False)
        self.assertEqual(r2.status_code, 302)
        page = self.lookup(rect=(0, 0, 5, 5)).get_data(as_text=True)
        self.assertIn("B-9", page)

    def test_restart_survives(self):
        self.register("B-1")
        fresh = app_module.BoardStore(self.dir)
        self.assertEqual(fresh.get("B-1")["version"], 1)
        store = app_module.store
        app_module.store = fresh
        try:
            page = self.lookup(rect=(0, 0, 5, 5)).get_data(as_text=True)
            self.assertIn("(10, 10, 5, 5)", page)
        finally:
            app_module.store = store

    def test_lookup_bad_integer(self):
        r = self.client.post("/lookup", data={
            "photo_id": "P1", "x": "1.5", "y": 0, "w": 5, "h": 5})
        self.assertEqual(r.status_code, 400)
        self.assertIn("必须是整数", r.get_data(as_text=True))

    def test_lookup_large_rect_reports_all_parts(self):
        self.register("B-1")
        page = self.lookup(rect=(0, 0, 100, 100)).get_data(as_text=True)
        # P1 露出中挖去了 P2 的 (15,15)-(25,25)，反查结果不应包含被遮区域
        self.assertNotIn("(15, 15, 10, 10)", page)
        self.assertIn("B-1", page)
        self.assertIn("稿次 1", page)
        # 四块露出矩形面积合计 = 400 - 100
        rects = [(10, 10, 20, 5), (10, 15, 5, 10), (10, 25, 20, 5), (25, 15, 5, 10)]
        for x, y, w, h in rects:
            self.assertIn(f"({x}, {y}, {w}, {h})", page)


if __name__ == "__main__":
    unittest.main()
