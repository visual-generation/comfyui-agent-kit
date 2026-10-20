#!/usr/bin/env python3
"""ComfyUI に API 用レシピを送り、終わるのを待ち、できたファイルを作品フォルダへ保存して記録する。

標準ライブラリだけで動く（Windows / macOS / Linux）。
使い方は AGENTS.md の「道具：tools/comfy_run.py」を参照。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import mimetypes
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

CANDIDATE_PORTS = (8188, 8000, 8189)
OPEN_STATES = {"sending", "sent", "unknown"}  # 結果を確かめ終えていない記録


class Stop(Exception):
    """送らずに止まる理由。終了コード付き。"""

    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def http_json(url: str, data: dict | None = None, timeout: float = 30):
    body = None if data is None else json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"} if body else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def find_server(explicit: str | None) -> str:
    import os

    candidates = [explicit] if explicit else []
    if os.environ.get("COMFY_URL"):
        candidates.append(os.environ["COMFY_URL"])
    candidates += [f"http://127.0.0.1:{p}" for p in CANDIDATE_PORTS]
    for base in candidates:
        base = base.rstrip("/")
        host = urllib.parse.urlparse(base).hostname
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise Stop(f"自分のパソコン以外の接続先は使いません: {base}", 2)
        try:
            stats = http_json(base + "/system_stats", timeout=3)
            ver = stats.get("system", {}).get("comfyui_version", "?")
            print(f"接続先: {base}（ComfyUI {ver}）")
            return base
        except Exception:
            continue
    raise Stop("ComfyUI が見つかりません。ComfyUI を起動してから、もう一度実行してください。", 2)


def load_recipe(path: Path) -> dict:
    graph = json.loads(path.read_text(encoding="utf-8"))
    if "nodes" in graph and "links" in graph:
        raise Stop("これは画面用のレシピです。ComfyUI の「ファイル → エクスポート (API)」で保存し直してください。", 2)
    return graph


def apply_sets(graph: dict, sets: list[str]) -> None:
    for item in sets:
        key, _, raw = item.partition("=")
        node, _, field = key.partition(".")
        if node not in graph or not field:
            raise Stop(f"差し替え先が見つかりません: {item}", 2)
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        graph[node]["inputs"][field] = value


def options_of(spec) -> list | None:
    """入力の定義から選択肢の一覧を取り出す（旧形式 [[...]] と新形式 ["COMBO", {"options": [...]}] の両方）。"""
    if not isinstance(spec, list) or not spec:
        return None
    if isinstance(spec[0], list):
        return spec[0]
    if spec[0] == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict):
        return spec[1].get("options")
    return None


def check_nodes(base: str, graph: dict, skip_files: set[str] = frozenset()) -> None:
    paid, unknown, missing = [], [], []
    for node_id, node in graph.items():
        ctype = node.get("class_type")
        try:
            info = http_json(base + "/object_info/" + urllib.parse.quote(ctype), timeout=10)
        except Exception:
            info = {}
        if ctype not in info:
            unknown.append(f"{node_id}:{ctype}")
            continue
        if info[ctype].get("api_node"):
            paid.append(f"{node_id}:{ctype}")
        if node_id in skip_files:
            continue
        spec_all = {**info[ctype].get("input", {}).get("required", {}), **info[ctype].get("input", {}).get("optional", {})}
        for field, value in node.get("inputs", {}).items():
            opts = options_of(spec_all.get(field))
            if isinstance(value, str) and opts is not None and value not in opts:
                missing.append(f"{node_id}:{ctype}.{field}={value}")
    if paid:
        raise Stop("有料のAPIを呼ぶ箱が入っているので送りません: " + ", ".join(paid), 4)
    if unknown:
        raise Stop("ComfyUI が知らない箱があるので送りません（拡張機能が無いか、名前の違い）: " + ", ".join(unknown), 4)
    if missing:
        raise Stop("ComfyUI に無いファイル・選べない値があるので送りません（モデルが未導入か、名前の違い）:\n  " + "\n  ".join(missing), 7)
    print("点検: 有料の箱・知らない箱・足りないファイルはありません")


def upload_image(base: str, path: Path) -> str:
    boundary = uuid.uuid4().hex
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    name = f"{dt.datetime.now():%Y%m%d-%H%M%S}_{path.name}"
    head = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{name}\"\r\n"
        f"Content-Type: {ctype}\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\nfalse\r\n--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(
        base + "/upload/image",
        data=head + path.read_bytes() + tail,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        res = json.loads(r.read().decode("utf-8"))
    return (res.get("subfolder") + "/" if res.get("subfolder") else "") + res["name"]


def open_records(runs_dir: Path) -> list[Path]:
    out = []
    for p in sorted(runs_dir.glob("*.json")):
        try:
            if json.loads(p.read_text(encoding="utf-8")).get("status") in OPEN_STATES:
                out.append(p)
        except Exception:
            out.append(p)
    return out


def save_record(path: Path, rec: dict) -> None:
    path.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")


def collect(base: str, rec: dict, record_path: Path, out_dir: Path, wait: float) -> int:
    pid = rec.get("prompt_id")
    if not pid:
        print("送ったかどうか分かりません（受付番号がありません）。ComfyUI の履歴を人が確かめてください。")
        return 3
    deadline = time.time() + wait
    while True:
        try:
            hist = http_json(base + "/history/" + pid, timeout=10)
        except Exception as e:
            print(f"履歴を読めませんでした: {e}")
            return 3
        if pid in hist:
            break
        if time.time() > deadline:
            print(f"まだ終わっていません（受付番号 {pid}）。あとで --recover {record_path} で受け取れます。")
            return 5
        time.sleep(2)
    entry = hist[pid]
    status = entry.get("status", {})
    files = []
    out_dir.mkdir(parents=True, exist_ok=True)
    for node_out in entry.get("outputs", {}).values():
        for kind in ("images", "videos", "gifs", "audio"):
            for f in node_out.get(kind, []) or []:
                if f.get("type") != "output":
                    continue
                q = urllib.parse.urlencode({"filename": f["filename"], "subfolder": f.get("subfolder", ""), "type": "output"})
                with urllib.request.urlopen(base + "/view?" + q, timeout=120) as r:
                    data = r.read()
                dest = out_dir / f"{record_path.stem}_{f['filename']}"
                dest.write_bytes(data)
                files.append(str(dest))
    rec.update(
        status="done" if status.get("status_str") == "success" else "failed",
        finished=now(),
        outputs=files,
        comfy_status=status.get("status_str"),
    )
    if rec.get("sent_at"):
        t0 = dt.datetime.fromisoformat(rec["sent_at"])
        rec["seconds"] = round((dt.datetime.now().astimezone() - t0).total_seconds(), 1)
    save_record(record_path, rec)
    print(f"結果: {rec['status']}")
    for f in files:
        print(f"保存: {f}")
    return 0 if rec["status"] == "done" else 6


def main() -> int:
    ap = argparse.ArgumentParser(description="ComfyUI へレシピを送り、結果を作品フォルダへ保存して記録する")
    ap.add_argument("recipe", nargs="?", help="API用レシピ（エクスポート (API) で保存したJSON）")
    ap.add_argument("--work", required=True, help="作品フォルダ（例 works/mug）")
    ap.add_argument("--set", action="append", default=[], metavar="ノード.欄=値", help='差し替え。例 --set 6.text="a cat" --set 5.noise_seed=7')
    ap.add_argument("--image", action="append", default=[], metavar="ノード=画像", help="画像を ComfyUI へ渡し、そのノードの image 欄に入れる")
    ap.add_argument("--server", help="接続先（例 http://127.0.0.1:8188）。省略時は自分のパソコンの中で探す")
    ap.add_argument("--note", default="", help="この1回のメモ（何を変えたか）")
    ap.add_argument("--check-only", action="store_true", help="送らずに点検だけする")
    ap.add_argument("--recover", metavar="記録", help="結果を確かめていない記録の結果を受け取る（送り直さない）")
    ap.add_argument("--wait", type=float, default=1800, help="終わるのを待つ秒数（既定 1800）")
    a = ap.parse_args()

    work = Path(a.work)
    runs_dir, out_dir = work / "runs", work / "outputs"
    if not a.check_only:
        runs_dir.mkdir(parents=True, exist_ok=True)
    try:
        base = find_server(a.server)
        if a.recover:
            rp = Path(a.recover)
            return collect(base, json.loads(rp.read_text(encoding="utf-8")), rp, out_dir, a.wait)
        if not a.recipe:
            raise Stop("レシピを指定してください。", 2)
        pending = open_records(runs_dir)
        if pending:
            raise Stop("結果を確かめていない記録があるので送りません。先に --recover で確かめてください: "
                       + ", ".join(str(p) for p in pending), 3)
        graph = load_recipe(Path(a.recipe))
        apply_sets(graph, a.set)
        check_nodes(base, graph, skip_files={i.partition("=")[0] for i in a.image}
                    | {k for k, v in graph.items() if v.get("class_type") == "LoadImage" and a.check_only})
        if a.check_only:
            return 0
        for item in a.image:
            node, _, img = item.partition("=")
            if node not in graph:
                raise Stop(f"画像の差し先が見つかりません: {item}", 2)
            graph[node]["inputs"]["image"] = upload_image(base, Path(img))
            print(f"画像を渡しました: {img} → ノード {node}")
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        record_path = runs_dir / f"{stamp}.json"
        rec = {"status": "sending", "recipe": a.recipe, "note": a.note, "sets": a.set, "images": a.image,
               "server": base, "created": now(), "graph": graph}
        save_record(record_path, rec)  # 送る前に残す
        try:
            res = http_json(base + "/prompt", {"prompt": graph, "client_id": uuid.uuid4().hex}, timeout=60)
        except urllib.error.HTTPError as e:
            rec.update(status="rejected", error=e.read().decode("utf-8", "replace")[:2000], finished=now())
            save_record(record_path, rec)
            print("ComfyUI が受け付けませんでした（送れていません）:\n" + rec["error"])
            return 6
        except Exception as e:
            rec.update(status="unknown", error=str(e))
            save_record(record_path, rec)
            print(f"送ったかどうか分かりません: {e}\n送り直さずに、ComfyUI の履歴を確かめてください。記録: {record_path}")
            return 3
        rec.update(status="sent", prompt_id=res.get("prompt_id"), sent_at=now())
        save_record(record_path, rec)
        print(f"送りました（受付番号 {rec['prompt_id']}）。記録: {record_path}")
        return collect(base, rec, record_path, out_dir, a.wait)
    except Stop as s:
        print(str(s))
        return s.code


if __name__ == "__main__":
    sys.exit(main())
