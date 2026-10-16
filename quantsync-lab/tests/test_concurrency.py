"""Real server, parallel signed-in requests: the per-request DB connection must work across threadpool threads."""
import threading
import time
import urllib.request

import uvicorn

from lab import api, db, service


def test_parallel_requests_real_server(cfg):
    db.migrate(cfg)
    con = db.connect(cfg)
    sid = service.create_strategy(con, cfg, "cc1", "Parallel", "1D", {"v": 1, "all": [{"l": {"f": "rsi_14"}, "op": ">", "r": {"c": 55}}]}, None)["id"]
    srv = uvicorn.Server(uvicorn.Config(api.create_app(cfg), port=8766, log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started:
        time.sleep(0.05)
    errs = []

    def hit(path):
        for _ in range(10):
            try:
                urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8766/lab/v1" + path, headers={"X-Dev-Uid": "cc1"}), timeout=30).read()
            except Exception as e:  # noqa: BLE001
                errs.append((path, getattr(e, "code", e)))
    ts = [threading.Thread(target=hit, args=(p,)) for p in ["/strategies?scope=mine", f"/strategies/{sid}", f"/strategies/{sid}/backtest"] * 3]
    [t.start() for t in ts]; [t.join() for t in ts]
    srv.should_exit = True
    assert errs == []
