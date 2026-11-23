"""登记与反查的测试：python3 -I -m unittest test_store -v"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import geometry
from store import BoardStore, SaveFailed, StaleDraft, lookup


def make(photos, pieces, board=(100, 100)):
    return {
        "board": {"width": board[0], "height": board[1]},
        "photos": photos,
        "pieces": pieces,
    }


def photo(pid, permit, credit="某"):
    return {"id": pid, "credit": credit,
            "permit": {"x": permit[0], "y": permit[1],
                       "w": permit[2] - permit[0], "h": permit[3] - permit[1]}}


def piece(pid, crop, at):
    return {"photo": pid,
            "crop": {"x": crop[0], "y": crop[1],
                     "w": crop[2] - crop[0], "h": crop[3] - crop[1]},
            "at": {"x": at[0], "y": at[1]}}


def clean(key="P1", credit="甲"):
    """一块 20x20 的送印稿，原片可刊矩形足够大，通过刊用核查。"""
    return make([photo(key, (0, 0, 100, 100), credit)],
                [piece(key, (0, 0, 20, 20), (10, 10))])


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.store = BoardStore(self.dir)

    def tearDown(self):
        self.tmp.cleanup()

    def test_register_and_lookup(self):
        v = self.store.commit("B-1", 0, clean())
        self.assertEqual(v, 1)
        states = self.store.snapshot_states()
        hits = lookup(states, "P1", (0, 0, 5, 5))
        self.assertEqual(len(hits), 1)
        h = hits[0]
        self.assertEqual((h["board"], h["version"], h["piece_no"]), ("B-1", 1, 1))
        # 原片 (0,0)-(5,5) 落在展板 (10,10)-(15,15)
        self.assertEqual(h["rect"], (10, 10, 15, 15))

    def test_edge_touch_ignored(self):
        self.store.commit("B-1", 0, clean())
        # 瑕疵与露出区域只在 x=20 处相碰，没有面积
        self.assertEqual(lookup(self.store.snapshot_states(), "P1",
                                (20, 10, 25, 15)), [])
        # 往外一点但仍只碰边
        self.assertEqual(lookup(self.store.snapshot_states(), "P1",
                                (20, 20, 40, 40)), [])

    def test_multiple_exposures_all_listed(self):
        # 同一原片两块裁片，瑕疵同时落在两处
        data = make([photo("P1", (0, 0, 200, 200))],
                    [piece("P1", (0, 0, 10, 10), (0, 0)),
                     piece("P1", (100, 100, 110, 110), (50, 50))])
        self.store.commit("B-1", 0, data)
        # 原片 (0,0)-(10,10) 与第二片的裁框不重叠 → 只命中第一处
        self.assertEqual(len(lookup(self.store.snapshot_states(), "P1",
                                    (0, 0, 10, 10))), 1)
        # 一个覆盖两处裁框的大瑕疵 → 两处都列
        data = make([photo("P1", (0, 0, 200, 200))],
                    [piece("P1", (0, 0, 10, 10), (0, 0)),
                     piece("P1", (100, 100, 110, 110), (50, 50))])
        self.store.commit("B-1", 1, data)
        hits = lookup(self.store.snapshot_states(), "P1", (0, 0, 110, 110))
        self.assertEqual(len(hits), 2)
        self.assertEqual({h["rect"] for h in hits},
                         {(0, 0, 10, 10), (50, 50, 60, 60)})

    def test_occluded_part_not_listed(self):
        data = make([photo("P1", (0, 0, 100, 100)), photo("P2", (0, 0, 100, 100))],
                    [piece("P1", (0, 0, 20, 20), (0, 0)),
                     piece("P2", (0, 0, 10, 10), (5, 5))])
        self.store.commit("B-1", 0, data)
        # P1 露出为 (0,0)-(20,20) 挖去 (5,5)-(15,15)
        hits = lookup(self.store.snapshot_states(), "P1", (0, 0, 20, 20))
        rects = [h["rect"] for h in hits]
        self.assertNotIn((5, 5, 15, 15), rects)
        covered = sum((r[2] - r[0]) * (r[3] - r[1]) for r in rects)
        self.assertEqual(covered, 400 - 100)

    def test_stale_draft_rejected_and_old_kept(self):
        self.store.commit("B-1", 0, clean())
        # 模拟另一人换稿
        self.store.commit("B-1", 1, clean())
        with self.assertRaises(StaleDraft):
            self.store.commit("B-1", 1, clean())
        self.assertEqual(self.store.get("B-1")["version"], 2)
        # 旧稿的露出数据仍可查，且稿次为当前生效稿次
        hits = lookup(self.store.snapshot_states(), "P1", (0, 0, 5, 5))
        self.assertEqual(hits[0]["version"], 2)

    def test_new_draft_with_wrong_version_rejected(self):
        with self.assertRaises(StaleDraft):
            self.store.commit("B-1", 3, clean())
        self.assertIsNone(self.store.get("B-1"))

    def test_unlicensed_rejected(self):
        bad = make([photo("P1", (0, 0, 10, 10))],
                   [piece("P1", (0, 0, 20, 20), (0, 0))])
        with self.assertRaises(ValueError):
            self.store.commit("B-1", 0, bad)
        self.assertIsNone(self.store.get("B-1"))

    def test_restart_keeps_new_draft(self):
        self.store.commit("B-1", 0, clean())
        self.store.commit("B-1", 1, make(
            [photo("P1", (0, 0, 100, 100), "乙")],
            [piece("P1", (0, 0, 30, 30), (40, 40))]))
        reloaded = BoardStore(self.dir)
        self.assertEqual(reloaded.get("B-1")["version"], 2)
        hits = lookup(reloaded.snapshot_states(), "P1", (0, 0, 5, 5))
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["rect"], (40, 40, 45, 45))
        self.assertEqual(hits[0]["credit"], "乙")

    def test_save_failure_keeps_old(self):
        self.store.commit("B-1", 0, clean())
        # 让目录不可写，模拟保存失败
        os.chmod(self.dir, 0o500)
        try:
            if os.access(self.dir, os.W_OK):
                self.skipTest("当前用户无视目录权限，无法模拟写失败")
            with self.assertRaises(SaveFailed):
                self.store.commit("B-1", 1, clean())
        finally:
            os.chmod(self.dir, 0o700)
        self.assertEqual(self.store.get("B-1")["version"], 1)
        hits = lookup(self.store.snapshot_states(), "P1", (0, 0, 5, 5))
        self.assertEqual(hits[0]["version"], 1)

    def test_snapshot_stable_during_lookup(self):
        self.store.commit("B-1", 0, clean())
        states = self.store.snapshot_states()
        self.store.commit("B-1", 1, make(
            [photo("P1", (0, 0, 100, 100))],
            [piece("P1", (0, 0, 20, 20), (50, 50))]))
        # 查询用的是取出的快照，仍是稿次 1 的位置
        hits = lookup(states, "P1", (0, 0, 5, 5))
        self.assertEqual(hits[0]["version"], 1)
        self.assertEqual(hits[0]["rect"], (10, 10, 15, 15))

    def test_board_ids_isolated(self):
        self.store.commit("B-1", 0, clean())
        self.store.commit("B-2", 0, make(
            [photo("P9", (0, 0, 100, 100))],
            [piece("P9", (0, 0, 20, 20), (0, 0))]))
        self.assertEqual(len(lookup(self.store.snapshot_states(), "P1",
                                    (0, 0, 5, 5))), 1)
        self.assertEqual(len(lookup(self.store.snapshot_states(), "P9",
                                    (0, 0, 5, 5))), 1)

    def test_large_canvas_no_pixel_memory(self):
        big = make([photo("P1", (0, 0, 100000, 100000))],
                   [piece("P1", (0, 0, 50000, 50000), (50000, 50000))],
                   board=(100000, 100000))
        self.store.commit("B-1", 0, big)
        hits = lookup(self.store.snapshot_states(), "P1", (0, 0, 10, 10))
        self.assertEqual(hits[0]["rect"], (50000, 50000, 50010, 50010))
        path = os.path.join(self.dir, os.listdir(self.dir)[0])
        self.assertLess(os.path.getsize(path), 4096)

    def test_snapshot_limits(self):
        with self.assertRaises(geometry.IndexTooLarge):
            geometry.build_snapshot(clean(), max_pieces=0)

    def test_unknown_photo(self):
        self.store.commit("B-1", 0, clean())
        self.assertEqual(lookup(self.store.snapshot_states(), "NOPE",
                                (0, 0, 5, 5)), [])

    def test_concurrent_commit_and_lookup(self):
        """登记新稿的同时反查：结果要么全为旧稿，要么全为新稿，不混用。"""
        import threading

        self.store.commit("B-1", 0, clean())
        self.store.commit("B-2", 0, clean())
        new_a = make([photo("P1", (0, 0, 100, 100))],
                     [piece("P1", (0, 0, 20, 20), (40, 40))])
        new_b = make([photo("P1", (0, 0, 100, 100))],
                     [piece("P1", (0, 0, 20, 20), (60, 60))])
        stop = threading.Event()

        def writer():
            while not stop.is_set():
                try:
                    self.store.commit("B-1", self.store.get("B-1")["version"],
                                      new_a)
                    self.store.commit("B-2", self.store.get("B-2")["version"],
                                      new_b)
                except StaleDraft:
                    pass

        t = threading.Thread(target=writer)
        t.start()
        try:
            for _ in range(50):
                hits = lookup(self.store.snapshot_states(), "P1", (0, 0, 5, 5))
                boards = {h["board"]: h["version"] for h in hits}
                rects = {(h["board"], h["rect"]) for h in hits}
                old = {("B-1", (10, 10, 15, 15)), ("B-2", (10, 10, 15, 15))}
                new = {("B-1", (40, 40, 45, 45)), ("B-2", (60, 60, 65, 65))}
                # 每次查询只应看到一份一致的状态：要么两块都是旧稿，
                # 要么各自是新稿（B-1/B-2 各自独立换稿，但同一次快照内
                # 每块展板只出现一个稿次的露出）
                self.assertEqual(len(boards), 2)
                for board, rect in rects:
                    if board == "B-1":
                        self.assertIn(rect, {(10, 10, 15, 15),
                                             (40, 40, 45, 45)})
                    else:
                        self.assertIn(rect, {(10, 10, 15, 15),
                                             (60, 60, 65, 65)})
                self.assertTrue(rects <= old | new)
        finally:
            stop.set()
            t.join()


if __name__ == "__main__":
    unittest.main()
