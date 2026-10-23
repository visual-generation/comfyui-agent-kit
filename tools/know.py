#!/usr/bin/env python3
"""ComfyUI で「何ができるか・何が変わったか・自分の環境で動くか」を、その場で公式の情報源から調べる。

手書きの知識は持たない。答えはすべて、実行した時点の次の情報源から取る。
  - 公式テンプレート一覧（github.com/Comfy-Org/workflow_templates の templates/index.json）
  - 動いている ComfyUI（/system_stats、/object_info、/models、/docs、/templates）
  - Comfy Cloud の公開一覧（cloud.comfy.org/api/experiment/models）
  - 公式の更新履歴（api.comfy.org/releases）とブログ（blog.comfy.org/feed）
  - Hugging Face のモデル情報（huggingface.co/api/models）
  - ComfyUI の不具合報告（GitHub の検索）

標準ライブラリだけで動く（Windows / macOS / Linux）。使い方は AGENTS.md の「道具：tools/know.py」を参照。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

KIT = Path(__file__).resolve().parent.parent
CACHE = KIT / ".cache"
ME = KIT / "me"  # この利用者のもの（Git に入れない）
USER_DECIDED = ("works_dir", "notes")  # 利用者が決めて書き足す欄。--save で上書きしない
RAW = "https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates"
CLOUD = "https://cloud.comfy.org"
RELEASES = "https://api.comfy.org/releases?project=comfyui"
BLOG_FEED = "https://blog.comfy.org/feed"
CANDIDATE_PORTS = (8188, 8000, 8189)
INDEX_MAX_AGE = 24 * 60 * 60
FRONTEND_ONLY = {"Note", "MarkdownNote", "Reroute", "PrimitiveNode"}  # 画面だけの箱。実行には送られない
WEIGHT_FORMATS = re.compile(r"(fp8\w*|fp16|bf16|fp32|int8\w*|nvfp4\w*|mxfp8|gguf|q\d_k\w*)", re.I)


class Stop(Exception):
    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code


def fetch(url: str, timeout: float = 20, as_json: bool = True):
    req = urllib.request.Request(url, headers={"User-Agent": "comfyui-agent-kit/know"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return json.loads(data.decode("utf-8")) if as_json else data.decode("utf-8", "replace")


def try_fetch(url: str, **kw):
    try:
        return fetch(url, **kw)
    except Exception:
        return None


def find_server(explicit: str | None) -> str | None:
    candidates = [explicit] if explicit else []
    if os.environ.get("COMFY_URL"):
        candidates.append(os.environ["COMFY_URL"])
    candidates += [f"http://127.0.0.1:{p}" for p in CANDIDATE_PORTS]
    for base in candidates:
        base = base.rstrip("/")
        if urllib.parse.urlparse(base).hostname not in ("127.0.0.1", "localhost", "::1"):
            raise Stop(f"自分のパソコン以外の接続先は使いません: {base}")
        if try_fetch(base + "/system_stats", timeout=3):
            return base
    return None


def need_server(args) -> str:
    base = find_server(args.server)
    if not base:
        raise Stop("自分のパソコンで動いている ComfyUI が見つかりません。起動するか、Comfy Cloud なら --cloud を付けてください。")
    return base


# ---- テンプレート一覧 -------------------------------------------------------

def remote_index(refresh: bool = False) -> tuple[list, str]:
    """最新の公式テンプレート一覧。1日だけ手元に控える。取れなければ控えを使う。"""
    CACHE.mkdir(exist_ok=True)
    path = CACHE / "index.json"
    fresh = path.exists() and time.time() - path.stat().st_mtime < INDEX_MAX_AGE
    if refresh or not fresh:
        data = try_fetch(f"{RAW}/index.json", timeout=30)
        if data:
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    if not path.exists():
        raise Stop("公式テンプレート一覧を取れませんでした（ネットにつながっているか確かめてください）。")
    taken = dt.datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    return json.loads(path.read_text(encoding="utf-8")), taken


def flat(index: list) -> list[dict]:
    rows = []
    for cat in index:
        for t in cat.get("templates", []):
            rows.append({**t, "_category": cat.get("title", "")})
    return rows


def input_kind(t: dict) -> str:
    io = t.get("io")
    if not io:
        return "unknown"
    kinds = sorted({i.get("mediaType", "?") for i in io.get("inputs", [])})
    return "+".join(kinds) if kinds else "none"


def is_paid(t: dict) -> bool:
    return "API" in t.get("tags", []) or t.get("openSource") is False


def version_tuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3])


def gb(n) -> str:
    return f"{n / 1e9:.1f}GB" if isinstance(n, (int, float)) and n else "?"


def describe(t: dict, local_names: set | None, local_ver: str | None) -> list[str]:
    flags = ["有料API" if is_paid(t) else "手元で動かせるモデル（利用条件は model で確かめる）"]
    if t.get("size"):
        flags.append(f"ダウンロード約{gb(t['size'])}")
    if t.get("requiresCustomNodes"):
        flags.append("拡張機能が要る: " + ", ".join(t["requiresCustomNodes"]))
    need = t.get("minComfyUIVersion")
    if need and local_ver and version_tuple(need) > version_tuple(local_ver):
        flags.append(f"ComfyUI {need} 以上が要る（手元は {local_ver}）")
    if local_names is not None and t["name"] not in local_names:
        flags.append("手元の ComfyUI の一覧にはまだ無い")
    lines = [f"- {t['name']}  「{t.get('title', '')}」  {t.get('date', '')}",
             f"    札: {', '.join(t.get('tags', []))} / モデル: {', '.join(t.get('models', []) or ['?'])}",
             f"    {' / '.join(flags)}"]
    desc = (t.get("description") or "").strip()
    if desc:
        lines.append("    " + (desc[:220] + "…" if len(desc) > 220 else desc))
    if t.get("tutorialUrl"):
        lines.append(f"    解説: {t['tutorialUrl']}")
    return lines


def local_template_info(base: str | None) -> tuple[set | None, str | None]:
    if not base:
        return None, None
    stats = try_fetch(base + "/system_stats", timeout=5) or {}
    ver = stats.get("system", {}).get("comfyui_version")
    idx = try_fetch(base + "/templates/index.json", timeout=10)
    names = {t["name"] for t in flat(idx)} if idx else None
    return names, ver


def cmd_tags(args):
    index, taken = remote_index(args.refresh)
    rows = flat(index)
    print(f"公式テンプレート {len(rows)} 件（{taken} 取得）\n")
    print("分類:")
    for cat in index:
        print(f"  {cat.get('category', '')} / {cat.get('title', '')}: {len(cat.get('templates', []))}")
    counts: dict[str, int] = {}
    for t in rows:
        for tag in t.get("tags", []):
            counts[tag] = counts.get(tag, 0) + 1
    print("\n札（多い順）:")
    print("  " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])))


def cmd_ways(args):
    index, taken = remote_index(args.refresh)
    base = None if args.cloud else find_server(args.server)
    local_names, local_ver = local_template_info(base)
    words = [w.lower() for w in args.words]
    scored = []
    for t in flat(index):
        if args.type and t.get("mediaType") != args.type and args.type not in t["_category"].lower():
            continue
        if args.tag and args.tag.lower() not in [x.lower() for x in t.get("tags", [])]:
            continue
        if args.free and is_paid(t):
            continue
        fields = [(t.get("title", ""), 3), (" ".join(t.get("tags", [])), 3),
                  (" ".join(t.get("models", []) or []), 2), (t.get("description", ""), 1), (t["name"], 1)]
        hits = [sum(w2 for text, w2 in fields if w in text.lower()) for w in words]
        if words and not all(hits):
            continue
        score = sum(hits)
        scored.append((score, t.get("usage", 0), t))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    groups = {"素材を入力に取る（いまの絵・動画を加工する／参考にする）": [],
              "文章だけから作る（もう一度1から作る）": [],
              "入力の記載なし（中身を開いて確かめる）": []}
    keys = list(groups)
    for _, _, t in scored[: args.limit]:
        kind = input_kind(t)
        groups[keys[2] if kind == "unknown" else keys[1] if kind == "none" else keys[0]].append(t)
    where = "Comfy Cloud" if args.cloud else (f"手元の ComfyUI {local_ver}" if local_ver else "手元の ComfyUI なし")
    print(f"公式テンプレート {len(scored)} 件が該当（{taken} 取得、上位 {min(args.limit, len(scored))} 件を表示、{where}）")
    for title, items in groups.items():
        if not items:
            continue
        print(f"\n## {title}")
        for t in items:
            kind = input_kind(t)
            for line in describe(t, local_names, local_ver):
                print(line)
            if kind not in ("none", "unknown"):
                print(f"    入力: {kind}")
    if not scored:
        print("該当なし。英語の別の言い方か、`tags` の札で探し直す。")


# ---- テンプレート1件の点検 ---------------------------------------------------

def template_models(graph: dict) -> list[dict]:
    found, seen = [], set()

    def walk(nodes):
        for n in nodes or []:
            for m in (n.get("properties") or {}).get("models") or []:
                key = (m.get("directory"), m.get("name"))
                if key not in seen:
                    seen.add(key)
                    found.append(m)

    walk(graph.get("nodes"))
    for sg in (graph.get("definitions") or {}).get("subgraphs") or []:
        walk(sg.get("nodes"))
    return found


def template_node_types(graph: dict) -> set[str]:
    types = {n.get("type") for n in graph.get("nodes") or []}
    sub_ids = set()
    for sg in (graph.get("definitions") or {}).get("subgraphs") or []:
        sub_ids.add(sg.get("id"))
        types |= {n.get("type") for n in sg.get("nodes") or []}
    return {t for t in types if t and t not in sub_ids and t not in FRONTEND_ONLY}


def model_list(base: str | None, folder: str, cloud: bool) -> set[str] | None:
    url = f"{CLOUD}/api/experiment/models/{folder}" if cloud else f"{base}/models/{folder}"
    data = try_fetch(url, timeout=15)
    if data is None:
        return None
    return {x["name"] if isinstance(x, dict) else x for x in data}


def cmd_check(args):
    graph = try_fetch(f"{RAW}/{urllib.parse.quote(args.name)}.json", timeout=30)
    if not graph:
        raise Stop(f"テンプレート {args.name} を取れませんでした。名前は `ways` の結果からそのまま写してください。")
    base = None if args.cloud else need_server(args)
    stats = {} if args.cloud else (try_fetch(base + "/system_stats") or {})
    devices = [d.get("type", "") for d in stats.get("devices", [])]
    print(f"{args.name}（{'Comfy Cloud' if args.cloud else base}）")
    print("\nモデルのファイル:")
    for m in template_models(graph):
        have = model_list(base, m.get("directory", ""), args.cloud)
        state = "不明" if have is None else ("ある" if m.get("name") in have else "無い")
        fmt = ", ".join(sorted({x.lower() for x in WEIGHT_FORMATS.findall(m.get("name", ""))})) or "記載なし"
        print(f"  [{state}] {m.get('directory')}/{m.get('name')}  形式: {fmt}")
        if state == "無い" and not args.cloud:
            print(f"        入手先: {m.get('url')}")
    types = template_node_types(graph)
    if args.cloud:
        print(f"\n使う箱 {len(types)} 種（Cloud の箱の一覧は API キーが要るため、ここでは確かめない）")
    else:
        info = try_fetch(base + "/object_info", timeout=60) or {}
        missing = sorted(t for t in types if t not in info)
        paid = sorted(t for t in types if info.get(t, {}).get("api_node"))
        print(f"\n使う箱 {len(types)} 種: 手元に無い {len(missing)}、有料APIの箱 {len(paid)}")
        for t in missing:
            print(f"  [無い] {t}（拡張機能か、新しい版の ComfyUI が要る）")
        for t in paid:
            print(f"  [有料] {t}")
    if "mps" in devices:
        print("\nこの PC は Mac の GPU（MPS）で動いている。上のファイルの形式が Mac で動くかは、"
              "`issues \"<モデル名> mps\"` で不具合報告を確かめる。")


# ---- 環境・箱・モデル --------------------------------------------------------

def disk_free(path: str | Path) -> str:
    try:
        import shutil
        return gb(shutil.disk_usage(path).free)
    except Exception:
        return "?"


def cmd_env(args):
    env: dict = {"checked": dt.datetime.now().isoformat(timespec="minutes"), "where": "cloud" if args.cloud else "local"}
    latest = try_fetch(RELEASES, timeout=15)
    newest = latest[0] if isinstance(latest, list) and latest else {}
    if newest:
        env["comfyui_latest"] = newest.get("version")
        print(f"ComfyUI の最新版: {newest.get('version')}（{(newest.get('published_at') or '')[:10]}、api.comfy.org）")
    index, taken = remote_index(args.refresh)
    print(f"公式テンプレート: {len(flat(index))} 件（{taken} 取得）")
    env["kit_disk_free"] = disk_free(KIT)
    print(f"このフォルダのドライブの空き: {env['kit_disk_free']}")
    if args.cloud:
        folders = try_fetch(f"{CLOUD}/api/experiment/models", timeout=15)
        print(f"Comfy Cloud: モデルの棚 {len(folders) if folders else '?'} 個を公開一覧から確認。実行には有料プランが要る。")
    else:
        base = find_server(args.server)
        if not base:
            env["where"] = "none"
            print("自分のパソコンで動いている ComfyUI: 見つからない（起動するか、Comfy Cloud を使う）")
        else:
            stats = try_fetch(base + "/system_stats") or {}
            s = stats.get("system", {})
            devices = [{"name": d.get("name"), "type": d.get("type"), "vram": gb(d.get("vram_total"))}
                       for d in stats.get("devices", [])]
            env.update({"server": base, "comfyui": s.get("comfyui_version"), "os": s.get("os"),
                        "ram": gb(s.get("ram_total")), "devices": devices})
            print(f"手元の ComfyUI: {s.get('comfyui_version')}（{base}）、OS {s.get('os')}、"
                  f"Python {str(s.get('python_version', '')).split()[0]}")
            print(f"パソコンのメモリ: {env['ram']}")
            for d in devices:
                print(f"GPU: {d['name']}（種類 {d['type']}、メモリ {d['vram']}）")
            if any(d["type"] == "mps" for d in devices):
                print("→ Mac の GPU（MPS）。テンプレートを使う前に `check` と `issues` で、Mac で動くかを確かめる。")
            folders = try_fetch(base + "/api/experiment/models", timeout=10) or []
            model_dirs = next((f.get("folders") for f in folders if f.get("name") == "diffusion_models"), None)
            model_dirs = [d for d in model_dirs or [] if Path(d).is_dir()]
            if model_dirs:
                env["model_dirs"] = model_dirs
                env["model_disk_free"] = disk_free(model_dirs[0])
                print(f"モデルの棚（diffusion_models）: {', '.join(model_dirs)}（空き {env['model_disk_free']}）")
            names, _ = local_template_info(base)
            if names is not None:
                env["templates_local"] = len(names)
                print(f"手元のテンプレート: {len(names)} 件（最新の一覧との差 {len(flat(index)) - len(names)} 件）")
    if args.save:
        ME.mkdir(exist_ok=True)
        path = ME / "環境.json"
        old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        kept = {k: v for k, v in old.items() if k in USER_DECIDED}
        path.write_text(json.dumps({**env, **kept}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n{path.relative_to(KIT)} に書いた（利用者が決めた {', '.join(kept) or 'なし'} はそのまま）")


def cmd_node(args):
    base = need_server(args)
    info = try_fetch(f"{base}/object_info/{urllib.parse.quote(args.name)}") or {}
    node = info.get(args.name)
    if not node:
        raise Stop(f"箱 {args.name} は手元の ComfyUI にありません。")
    print(f"{args.name}「{node.get('display_name', '')}」 分類 {node.get('category')}"
          f"{' / 有料APIの箱' if node.get('api_node') else ''} / {node.get('python_module')}")
    if node.get("description"):
        print(node["description"])
    for part in ("required", "optional"):
        for name, spec in (node.get("input", {}).get(part) or {}).items():
            kind = spec[0] if spec else "?"
            opts = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
            if isinstance(kind, list):
                kind = f"選択肢 {len(kind)} 個: " + ", ".join(map(str, kind[:6])) + ("…" if len(kind) > 6 else "")
            extra = {k: opts[k] for k in ("default", "min", "max") if k in opts}
            print(f"  {'必須' if part == 'required' else '任意'} {name}: {kind} {extra if extra else ''}")
    doc = None
    for url in (f"{base}/docs/{args.name}/{args.lang}.md", f"{base}/docs/{args.name}/en.md",
                f"https://docs.comfy.org/built-in-nodes/{args.name}.md"):
        doc = try_fetch(url, as_json=False)
        if doc:
            print(f"\n説明（{url}）:\n")
            print("\n".join(doc.splitlines()[: args.lines]))
            break
    if not doc:
        print("\n説明の文書は見つからなかった。")


def cmd_model(args):
    data = try_fetch(f"https://huggingface.co/api/models/{args.repo}", timeout=20)
    if not data:
        raise Stop(f"Hugging Face で {args.repo} を取れませんでした（名前の綴りか、非公開か）。")
    card = data.get("cardData") or {}
    lic = card.get("license") or next((t.split(":", 1)[1] for t in data.get("tags", []) if t.startswith("license:")), "?")
    print(f"{args.repo}  利用条件の札: {lic}"
          f"{' / ' + card['license_name'] if card.get('license_name') else ''}"
          f" / 同意が要る: {data.get('gated') or 'いいえ'} / 更新 {str(data.get('lastModified', ''))[:10]}"
          f" / ダウンロード {data.get('downloads', '?')}")
    if card.get("license_link"):
        print(f"利用条件の本文: {card['license_link']}")
    print(f"利用条件の本文はモデルのページ https://huggingface.co/{args.repo} の License から開く。決めるのは本文。")
    files = [s["rfilename"] for s in data.get("siblings", []) if s["rfilename"].endswith((".safetensors", ".gguf"))]
    print(f"\n重みのファイル {len(files)} 個:")
    for f in files[: args.limit]:
        fmt = ", ".join(sorted({x.lower() for x in WEIGHT_FORMATS.findall(f)})) or "記載なし"
        print(f"  {f}  形式: {fmt}")


def find_comfy_cli() -> str | None:
    import shutil
    for cand in (os.environ.get("COMFY_CLI"), KIT / ".venv" / "Scripts" / "comfy.exe", KIT / ".venv" / "bin" / "comfy"):
        if cand and Path(cand).is_file():
            return str(cand)
    return shutil.which("comfy")


def cmd_make(args):
    """公式テンプレートを取ってきて、comfy_run.py で送れる API 用レシピにする（変換は公式の comfy-cli が行う）。"""
    import subprocess
    cli = find_comfy_cli()
    if not cli:
        raise Stop("公式の comfy-cli が見つかりません。入れるかは利用者が決める（AGENTS.md 2章）。"
                   "入れない場合は、ComfyUI の画面でテンプレートを開き「ファイル → エクスポート (API)」で保存する。")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    ui = out.with_name(out.stem + "_画面用.json")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    if args.server:
        env["COMFY_URL"] = args.server
    step = subprocess.run([cli, "--json", "templates", "fetch", args.name, "-o", str(ui)],
                          capture_output=True, text=True, encoding="utf-8", env=env)
    if step.returncode != 0:
        raise Stop(f"テンプレート {args.name} を取れませんでした: {step.stdout[-400:]}{step.stderr[-400:]}")
    step = subprocess.run([cli, "--json", "--where", "local", "run", "--workflow", str(ui), "--print-prompt"],
                          capture_output=True, text=True, encoding="utf-8", env=env)
    try:
        prompt = json.loads(step.stdout)["data"]["prompt"]
    except Exception:
        raise Stop(f"API 用に変換できませんでした: {step.stdout[-400:]}{step.stderr[-400:]}")
    out.write_text(json.dumps(prompt, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{out}（API 用。comfy_run.py で送る）と {ui.name}（画面用。ComfyUI に放り込むと箱と線で開く）を作った。")
    print(f"変換: {Path(cli).name} {step.stdout and json.loads(step.stdout).get('version', '')}。次に `know.py recipe {out}` で欄を確かめる。")


def cmd_recipe(args):
    graph = json.loads(Path(args.path).read_text(encoding="utf-8"))
    if "nodes" in graph and "links" in graph:
        raise Stop("画面用のレシピです。ComfyUI の「ファイル → エクスポート (API)」で保存したものを渡してください。")
    print(f"{args.path} の欄（--set ノード番号.欄=値 で差し替える。線でつながった欄は省く）")
    for nid, node in sorted(graph.items(), key=lambda kv: [int(x) if x.isdigit() else 0 for x in kv[0].split(":")]):
        values = {k: v for k, v in node.get("inputs", {}).items() if not isinstance(v, list)}
        title = (node.get("_meta") or {}).get("title", "")
        shown = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)[:60]}" for k, v in values.items())
        print(f"  {nid} {node.get('class_type')}{'「' + title + '」' if title else ''}: {shown or '（値の欄なし）'}")


# ---- 変わったこと --------------------------------------------------------

def cmd_changes(args):
    CACHE.mkdir(exist_ok=True)
    snap_path = CACHE / "snapshot.json"
    index, taken = remote_index(refresh=True)
    now = {t["name"]: {"title": t.get("title", ""), "date": t.get("date", ""), "tags": t.get("tags", [])}
           for t in flat(index)}
    snap = json.loads(snap_path.read_text(encoding="utf-8")) if snap_path.exists() else None
    since = args.since or (snap or {}).get("taken", "")[:10]
    if not since:
        since = (dt.date.today() - dt.timedelta(days=30)).isoformat()
    print(f"{since} からの変化（{taken} 取得）")

    print("\n## テンプレート")
    if snap:
        before = snap["templates"]
        added = [n for n in now if n not in before]
        gone = [n for n in before if n not in now]
        changed = [n for n in now if n in before and now[n]["date"] != before[n]["date"]]
        print(f"前回（{snap['taken'][:16]}）から 追加 {len(added)}・削除 {len(gone)}・日付が変わった {len(changed)}")
        lists = (("追加", added), ("削除", gone), ("更新", changed))
    else:
        dated = [n for n, v in now.items() if v["date"] >= since]
        print(f"前回の控えが無いため、日付が {since} 以降のものを示す（{len(dated)} 件）")
        lists = (("新しい日付", dated),)
    for label, names in lists:
        for n in sorted(names, key=lambda n: now.get(n, {}).get("date", ""), reverse=True)[: args.limit]:
            v = now.get(n) or snap["templates"].get(n, {})
            print(f"  [{label}] {n}「{v.get('title', '')}」 {v.get('date', '')} 札: {', '.join(v.get('tags', []))}")

    print("\n## ComfyUI の更新（api.comfy.org/releases）")
    for r in try_fetch(RELEASES, timeout=15) or []:
        day = (r.get("published_at") or "")[:10]
        if day < since:
            continue
        heads = [l.strip("*# ").strip() for l in (r.get("content") or "").splitlines()
                 if l.startswith("**") or l.startswith("* [**")]
        print(f"  {r.get('version')}（{day}）: " + "; ".join(re.sub(r"\[\*\*(.+?)\*\*\]\(.+?\)", r"\1", h) for h in heads)[:400])

    print("\n## 公式ブログ（blog.comfy.org）")
    feed = try_fetch(BLOG_FEED, as_json=False, timeout=20)
    if feed:
        for item in ET.fromstring(feed.encode("utf-8")).iter("item"):
            day = item.findtext("pubDate") or ""
            try:
                iso = dt.datetime.strptime(day[:16], "%a, %d %b %Y").date().isoformat()
            except ValueError:
                iso = ""
            if iso and iso < since:
                continue
            print(f"  {iso} {item.findtext('title')}  {item.findtext('link')}")
    if not args.no_save:
        snap_path.write_text(json.dumps({"taken": dt.datetime.now().isoformat(timespec="minutes"),
                                         "templates": now}, ensure_ascii=False), encoding="utf-8")
        print("\n今回の一覧を控えた。次回はここからの変化を示す。")


def cmd_issues(args):
    q = " ".join(args.words) + f" repo:{args.repo}"
    url = "https://api.github.com/search/issues?" + urllib.parse.urlencode(
        {"q": q, "sort": "updated", "order": "desc", "per_page": args.limit})
    data = try_fetch(url, timeout=20)
    if not data:
        raise Stop("GitHub の検索ができませんでした（回数の制限なら1分ほど待つ）。")
    print(f"{args.repo} で「{' '.join(args.words)}」: {data.get('total_count')} 件（新しく更新された順）")
    for it in data.get("items", []):
        print(f"  [{it.get('state')}] {it.get('updated_at', '')[:10]} {it.get('title')}  {it.get('html_url')}")


def main() -> int:
    p = argparse.ArgumentParser(description="ComfyUI の手段・変化・環境を公式の情報源から調べる")
    p.add_argument("--server", help="手元の ComfyUI の接続先（省略時は 127.0.0.1 を探す）")
    p.add_argument("--refresh", action="store_true", help="テンプレート一覧を取り直す")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("env", help="手元（または Cloud）の環境と、最新との差")
    s.add_argument("--cloud", action="store_true")
    s.add_argument("--save", action="store_true", help="me/環境.json に書く")
    s.set_defaults(func=cmd_env)

    s = sub.add_parser("tags", help="公式テンプレートの分類と札の一覧")
    s.set_defaults(func=cmd_tags)

    s = sub.add_parser("ways", help="やりたいことに合うテンプレートを、加工と作り直しに分けて並べる")
    s.add_argument("words", nargs="*", help="英語の言葉（例 upscale image）")
    s.add_argument("--type", help="image / video / audio / 3d")
    s.add_argument("--tag", help="札で絞る（例 'Image Upscale'）")
    s.add_argument("--free", action="store_true", help="有料APIを除く")
    s.add_argument("--cloud", action="store_true")
    s.add_argument("--limit", type=int, default=15)
    s.set_defaults(func=cmd_ways)

    s = sub.add_parser("check", help="テンプレート1件のファイル・箱が手元（または Cloud）にあるか")
    s.add_argument("name")
    s.add_argument("--cloud", action="store_true")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("node", help="箱の入力欄と公式の説明")
    s.add_argument("name")
    s.add_argument("--lang", default="ja")
    s.add_argument("--lines", type=int, default=60)
    s.set_defaults(func=cmd_node)

    s = sub.add_parser("model", help="Hugging Face のモデルの利用条件・重みの形式")
    s.add_argument("repo", help="配布者/名前（例 Comfy-Org/SeedVR2）")
    s.add_argument("--limit", type=int, default=30)
    s.set_defaults(func=cmd_model)

    s = sub.add_parser("make", help="公式テンプレートから API 用レシピを作る（comfy-cli を使う）")
    s.add_argument("name", help="テンプレート名（ways の結果から写す）")
    s.add_argument("out", help="出力先（例 <作品>/レシピ/upscale.json）")
    s.set_defaults(func=cmd_make)

    s = sub.add_parser("recipe", help="API用レシピの、差し替えられる欄の一覧")
    s.add_argument("path")
    s.set_defaults(func=cmd_recipe)

    s = sub.add_parser("changes", help="前回から増えた・変わったテンプレートと、公式の更新・ブログ")
    s.add_argument("--since", help="この日から（YYYY-MM-DD）")
    s.add_argument("--limit", type=int, default=40)
    s.add_argument("--no-save", action="store_true", help="今回の一覧を控えない")
    s.set_defaults(func=cmd_changes)

    s = sub.add_parser("issues", help="ComfyUI の不具合報告を検索（Mac の対応の確認など）")
    s.add_argument("words", nargs="+")
    s.add_argument("--repo", default="Comfy-Org/ComfyUI")
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(func=cmd_issues)

    args = p.parse_args()
    try:
        args.func(args)
        return 0
    except Stop as e:
        print(e, file=sys.stderr)
        return e.code
    except urllib.error.URLError as e:
        print(f"通信できませんでした: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
