#!/usr/bin/env python3
"""City-block 3D asset pipeline — observability dashboard generator.

Reads pipeline data (schematics, models, pipeline state, live status) and
generates a pure static HTML site for GitHub Pages. Idempotent: safe to run
every 5 minutes; only writes files whose content changed, and exits early
when the source fingerprint is unchanged.

Usage: python3 build_dashboard.py
"""

import os
import re
import json
import shutil
import hashlib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# ---------------------------------------------------------------- paths ---

HOME = '/home/hatch'
PACK = f'{HOME}/workspace/your_files/city-block-asset-pack'
OUT = f'{HOME}/workspace/city-block-dashboard'
HIDDEN = f'{HOME}/workspace/goals/unified-low-poly-3d-asset-library/hidden_files'

VIEWS = ['front', 'back', 'left', 'right', 'top', 'bottom',
         'angle-front-right', 'angle-back-left']
VIEW_LABELS = {
    'front': 'Front', 'back': 'Back', 'left': 'Left', 'right': 'Right',
    'top': 'Top', 'bottom': 'Bottom',
    'angle-front-right': 'Angle FR', 'angle-back-left': 'Angle BL',
}
LA = ZoneInfo('America/Los_Angeles')
MAX_FILE_BYTES = 50 * 1024 * 1024  # skip anything bigger

# -------------------------------------------------------------- helpers ---

def fmt_ts(mtime):
    """Readable Pacific timestamp from epoch seconds."""
    if not mtime:
        return '—'
    dt = datetime.fromtimestamp(mtime, tz=timezone.utc).astimezone(LA)
    return dt.strftime('%b %d, %H:%M')


def fmt_ts_long(mtime):
    if not mtime:
        return '—'
    dt = datetime.fromtimestamp(mtime, tz=timezone.utc).astimezone(LA)
    return dt.strftime('%Y-%m-%d %H:%M %Z')


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def mtime_of(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def copy_if_newer(src, dst):
    """Copy src->dst only if src is newer/different. Returns True if copied."""
    if not os.path.exists(src):
        return False
    if os.path.getsize(src) > MAX_FILE_BYTES:
        return False
    if os.path.exists(dst):
        st_src, st_dst = os.stat(src), os.stat(dst)
        if st_src.st_size == st_dst.st_size and st_src.st_mtime <= st_dst.st_mtime + 1:
            return False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    return True


def make_thumb(src, dst, width=360):
    """Generate a JPEG thumbnail; skip if up to date. Returns True if written."""
    if not HAS_PIL or not os.path.exists(src):
        return False
    if os.path.exists(dst) and os.path.getmtime(dst) >= os.path.getmtime(src):
        return False
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        im = Image.open(src).convert('RGB')
        w, h = im.size
        if w > width:
            im = im.resize((width, int(h * width / w)), Image.LANCZOS)
        im.save(dst, 'JPEG', quality=70)
        return True
    except Exception:
        return False


def write_if_changed(path, content):
    """Write only when content differs. Returns True if written."""
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            if f.read() == content:
                return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    return True


def esc(s):
    return (str(s).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))


# ------------------------------------------------------------ collectors ---

def collect_schematics():
    """Return list of schematic items with metadata."""
    base = os.path.join(PACK, 'schematics')
    items = []
    for sid in sorted(os.listdir(base)):
        sdir = os.path.join(base, sid)
        if not os.path.isdir(sdir):
            continue
        views = {}
        for v in VIEWS:
            p = os.path.join(sdir, v + '.jpg')
            if os.path.exists(p):
                views[v] = p
        meta_path = os.path.join(sdir, 'sheet-meta.json')
        meta = {}
        if os.path.exists(meta_path):
            try:
                meta = json.load(open(meta_path))
            except Exception:
                pass
        cs = os.path.join(sdir, 'contact-sheet.png')
        mtimes = [mtime_of(p) for p in views.values()]
        mtimes += [mtime_of(meta_path), mtime_of(cs)]
        items.append({
            'id': sid,
            'title': meta.get('title', sid),
            'views': views,                       # view -> source path
            'scores': meta.get('scores', {}),
            'specs': meta.get('specs', []),
            'style': meta.get('style', ''),
            'complete': len(views) == 8,
            'mtime': max(mtimes) if mtimes else 0,
            'has_contact_sheet': os.path.exists(cs),
            'contact_sheet_src': cs if os.path.exists(cs) else None,
        })
    return items


VIEW_ABBR = {'angle-bl': 'angle-back-left', 'angle-fr': 'angle-front-right',
             'angle-back-left': 'angle-back-left', 'angle-front-right': 'angle-front-right'}


def parse_scorecard(text):
    """Extract {view: (PASS/FAIL, score)} from Scorecard lines in a QA log."""
    scores = {}
    lines = text.splitlines()
    in_card = False
    for line in lines:
        if 'Scorecard' in line:
            in_card = True
        elif in_card and not re.search(r'PASS|FAIL', line):
            # allow one blank-ish continuation, else stop
            if line.strip() and not line.startswith((' ', '-', '\t')):
                in_card = False
                continue
        if in_card:
            for m in re.finditer(r'([A-Za-z][\w-]*)\s+(PASS|FAIL)\s+([\d.]+)', line):
                name = m.group(1).lower()
                name = VIEW_ABBR.get(name, name)
                if name in VIEWS and name not in scores:
                    scores[name] = (m.group(2), float(m.group(3)))
    return scores


def collect_models(queue_status):
    """Return list of model items with iterations."""
    base = os.path.join(PACK, 'models')
    items = []
    if not os.path.isdir(base):
        return items
    for mid in sorted(os.listdir(base)):
        mdir = os.path.join(base, mid)
        if not os.path.isdir(mdir):
            continue
        # iterations = render rev folders
        rdir = os.path.join(mdir, 'renders')
        revs = []
        if os.path.isdir(rdir):
            for rev in sorted(os.listdir(rdir)):
                revp = os.path.join(rdir, rev)
                if not os.path.isdir(revp):
                    continue
                pngs = {}
                for v in VIEWS:
                    p = os.path.join(revp, v + '.png')
                    if os.path.exists(p):
                        pngs[v] = p
                if not pngs:
                    # any pngs at all?
                    for f in os.listdir(revp):
                        if f.endswith('.png'):
                            pngs[f[:-4]] = os.path.join(revp, f)
                rev_mtime = max([mtime_of(p) for p in pngs.values()] or [0])
                revs.append({'rev': rev, 'renders': pngs, 'mtime': rev_mtime})
        revs.sort(key=lambda r: r['mtime'])
        # GLBs
        glbs = []
        for f in sorted(os.listdir(mdir)):
            if f.endswith('.glb'):
                p = os.path.join(mdir, f)
                glbs.append({'file': f, 'mtime': mtime_of(p), 'size': os.path.getsize(p)})
        glbs.sort(key=lambda g: g['mtime'])
        # QA logs + judge scores
        scores = {}
        qa_logs = []
        for f in sorted(os.listdir(mdir)):
            if f.endswith('.md') and ('qa' in f.lower() or 'log' in f.lower()):
                p = os.path.join(mdir, f)
                qa_logs.append({'file': f, 'mtime': mtime_of(p)})
                try:
                    parsed = parse_scorecard(open(p, encoding='utf-8', errors='replace').read())
                    for k, v in parsed.items():
                        scores.setdefault(k, v)
                except Exception:
                    pass
        # mesh stats (latest)
        mesh = {}
        for f in sorted(os.listdir(mdir)):
            if f.startswith('mesh-stats') and f.endswith('.json'):
                try:
                    mesh = json.load(open(os.path.join(mdir, f)))
                except Exception:
                    pass
        # build scripts
        builds = [f for f in sorted(os.listdir(mdir))
                  if f.endswith('.py') and f.startswith('build')]
        mtimes = [mtime_of(os.path.join(mdir, f)) for f in os.listdir(mdir)]
        mtimes += [r['mtime'] for r in revs] + [g['mtime'] for g in glbs]
        has_build_activity = bool(builds) or bool(revs)
        # A model is only 'finished' when it has a GLB AND a model-meta.json
        # with 3 passing judges AND no unresolved Felix-flagged defects.
        # A GLB alone just means a build exists — it may be mid-judging.
        meta_path = os.path.join(mdir, 'model-meta.json')
        meta_ok = False
        if os.path.exists(meta_path):
            try:
                meta = json.load(open(meta_path))
                judges = meta.get('judges', [])
                meta_ok = (len(judges) == 3 and
                           all(j.get('pass') and j.get('score', 0) >= 9.0
                               for j in judges))
            except Exception:
                pass
        felix_blocked = False
        for qf in qa_logs:
            try:
                qfile = qf.get('file') if isinstance(qf, dict) else qf
                content = open(os.path.join(mdir, qfile)).read()
                if 'Felix-flagged defect' in content:
                    # check if there's a resolution note after the last flag
                    last_flag = content.rfind('Felix-flagged defect')
                    after = content[last_flag:]
                    if 'resolved' not in after.lower() and 'fixed in' not in after.lower():
                        felix_blocked = True
            except Exception:
                pass
        # Status truth: pipeline queue first, then files.
        # - queue says building/judging/fixing -> in_progress
        # - queue says complete -> finished
        # - not in queue but has GLB: finished UNLESS Felix-flagged defect
        #   is unresolved (then in_progress — needs a fix rev)
        # - v2 model-meta.json with 3 passing judges also means finished
        # A model with a GLB is finished UNLESS:
        #  - the pipeline queue has it as actively being worked, or
        #  - there's an unresolved Felix-flagged defect against it.
        # Build scripts/rev folders alone are history, not activity.
        qs = queue_status.get(mid, '')
        if qs in ('building', 'judging', 'fixing', 'rendering', 'in_progress'):
            status = 'in_progress'
        elif felix_blocked:
            status = 'in_progress'
        elif qs == 'complete' or (glbs and meta_ok) or glbs:
            status = 'finished'
        elif has_build_activity:
            status = 'in_progress'
        else:
            status = qs or 'pending'
        all_pass = (scores and all(v[0] == 'PASS' for v in scores.values())
                    and len(scores) == 8)
        items.append({
            'id': mid,
            'revs': revs,
            'glbs': glbs,
            'latest_glb': glbs[-1] if glbs else None,
            'scores': scores,
            'all_judges_pass': bool(all_pass),
            'mesh': mesh,
            'qa_logs': qa_logs,
            'builds': builds,
            'status': status,
            'queue_status': queue_status.get(mid, '—'),
            'mtime': max(mtimes) if mtimes else 0,
            'n_revs': len(revs),
        })
    return items


def collect_pipelines():
    """Pipeline health: lease liveness + live status for both pipelines."""
    out = {}
    for name, state_f, live_f in [
        ('modeling',
         os.path.join(HIDDEN, 'modeling-pipeline-state.json'),
         os.path.join(HIDDEN, 'modeling-live-status.json')),
        ('schematic',
         os.path.join(HIDDEN, 'schematic-pipeline-state.json'),
         os.path.join(HIDDEN, 'schematic-live-status.json')),
    ]:
        info = {'name': name, 'lease_held': False, 'worker': None,
                'heartbeat': None, 'pid_alive': False, 'live': None}
        try:
            st = json.load(open(state_f))
            lease = st.get('lease') or {}
            info['worker'] = lease.get('worker')
            info['heartbeat'] = lease.get('heartbeat_at')
            info['lease_held'] = bool(lease.get('worker'))
            info['pid_alive'] = pid_alive(lease.get('pid')) if lease.get('pid') else False
        except Exception:
            pass
        try:
            info['live'] = json.load(open(live_f))
        except Exception:
            pass
        out[name] = info
    return out


def fingerprint(schematics, models, pipelines):
    """Hash of source mtimes+sizes+state so we can skip no-op runs."""
    h = hashlib.sha256()
    def feed(s):
        h.update(str(s).encode())
    for it in schematics:
        for v, p in it['views'].items():
            st = os.stat(p)
            feed((p, st.st_mtime, st.st_size))
        feed((it['id'], it['mtime']))
    for it in models:
        for r in it['revs']:
            for v, p in r['renders'].items():
                st = os.stat(p)
                feed((p, st.st_mtime, st.st_size))
        for g in it['glbs']:
            feed((g['file'], g['mtime'], g['size']))
        feed((it['id'], it['status'], it['mtime']))
    for name, p in pipelines.items():
        feed((name, p['worker'], p['heartbeat'], p['pid_alive'],
              json.dumps(p['live'], sort_keys=True) if p['live'] else None))
    for f in ['modeling-pipeline-state.json', 'schematic-pipeline-state.json']:
        p = os.path.join(HIDDEN, f)
        if os.path.exists(p):
            st = os.stat(p)
            feed((f, st.st_mtime, st.st_size))
    return h.hexdigest()

# ------------------------------------------------------------ asset sync ---

def sync_assets(schematics, models):
    """Copy needed assets + thumbnails into OUT/assets. Returns files copied."""
    copied = 0
    # schematics: 8 display JPGs (max 900px) + thumbs + contact sheet
    for it in schematics:
        sid = it['id']
        for v, src in it['views'].items():
            dst = os.path.join(OUT, 'assets', 'schematics', sid, v + '.jpg')
            thumb = os.path.join(OUT, 'assets', 'schematics', sid, 'thumb_' + v + '.jpg')
            # display copy: downscale big originals to max 900px wide
            if HAS_PIL:
                if (not os.path.exists(dst)
                        or os.path.getmtime(dst) < os.path.getmtime(src)):
                    try:
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        im = Image.open(src).convert('RGB')
                        w, h = im.size
                        if w > 900:
                            im = im.resize((900, int(h * 900 / w)), Image.LANCZOS)
                        im.save(dst, 'JPEG', quality=78)
                        copied += 1
                    except Exception:
                        if copy_if_newer(src, dst):
                            copied += 1
            else:
                if copy_if_newer(src, dst):
                    copied += 1
            if make_thumb(dst if os.path.exists(dst) else src, thumb):
                copied += 1
        if it['contact_sheet_src']:
            if copy_if_newer(it['contact_sheet_src'],
                             os.path.join(OUT, 'assets', 'schematics', sid,
                                          'contact-sheet.png')):
                copied += 1
    # models: render PNGs + thumbs + GLBs
    for it in models:
        mid = it['id']
        mdir = os.path.join(PACK, 'models', mid)
        for r in it['revs']:
            for v, src in r['renders'].items():
                rel = os.path.relpath(src, mdir)
                dst = os.path.join(OUT, 'assets', 'models', mid, rel)
                if copy_if_newer(src, dst):
                    copied += 1
                thumb = os.path.join(OUT, 'assets', 'models', mid,
                                     os.path.dirname(rel),
                                     'thumb_' + os.path.basename(rel).replace('.png', '.jpg'))
                if make_thumb(src, thumb):
                    copied += 1
        for g in it['glbs']:
            src = os.path.join(mdir, g['file'])
            if copy_if_newer(src, os.path.join(OUT, 'assets', 'models', mid, g['file'])):
                copied += 1
    return copied


# ------------------------------------------------------------------ css ---

CSS = """
:root{--bg:#0d1117;--card:#161b22;--border:#30363d;--text:#e6edf3;
--muted:#8b949e;--green:#3fb950;--yellow:#d29922;--red:#f85149;--blue:#58a6ff}
*{box-sizing:border-box}body{background:var(--bg);color:var(--text);
font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:0;
padding:0 0 60px;line-height:1.45}
.wrap{max-width:1100px;margin:0 auto;padding:16px}
header.top{border-bottom:1px solid var(--border);padding:16px;margin-bottom:16px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:18px;margin:28px 0 12px;
border-bottom:1px solid var(--border);padding-bottom:6px}
.sub{color:var(--muted);font-size:13px}
.pipes{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:12px 0}
@media(max-width:640px){.pipes{grid-template-columns:1fr}}
.pipe{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px}
.pipe .name{font-weight:700;margin-bottom:6px}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px}
.dot.live{background:var(--green);box-shadow:0 0 6px var(--green)}
.dot.idle{background:var(--muted)}
.kv{font-size:13px;color:var(--muted);margin:2px 0}.kv b{color:var(--text);font-weight:600}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;
overflow:hidden;text-decoration:none;color:var(--text);display:block}
.card img{width:100%;aspect-ratio:1.2;object-fit:cover;display:block;background:#000}
.card .body{padding:8px 10px}.card .id{font-weight:700;font-size:14px}
.card .meta{font-size:12px;color:var(--muted);margin-top:2px}
.badge{display:inline-block;font-size:11px;padding:2px 8px;border-radius:12px;margin-top:4px}
.badge.ok{background:rgba(63,185,80,.15);color:var(--green)}
.badge.work{background:rgba(210,153,34,.15);color:var(--yellow)}
.badge.idle{background:rgba(139,148,158,.15);color:var(--muted)}
.score{font-size:12px;color:var(--muted)}
.viewgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}
.view{background:var(--card);border:1px solid var(--border);border-radius:10px;overflow:hidden}
.view img{width:100%;display:block}.view .lbl{padding:6px 10px;font-size:13px;
display:flex;justify-content:space-between;align-items:center}
.pass{color:var(--green);font-weight:700}.fail{color:var(--red);font-weight:700}
.specs{background:var(--card);border:1px solid var(--border);border-radius:10px;
padding:12px 16px;font-size:14px}.specs li{margin:6px 0}
.back{display:inline-block;margin:12px 0;color:var(--blue);text-decoration:none}
#viewer{width:100%;height:420px;background:#000;border-radius:10px;border:1px solid var(--border)}
.rev{border:1px solid var(--border);border-radius:10px;padding:12px;margin:16px 0;background:var(--card)}
.rev h3{margin:0 0 8px;font-size:16px}
.note{font-size:12px;color:var(--muted)}
table.stats{border-collapse:collapse;font-size:13px;margin:8px 0}
table.stats td{padding:3px 12px 3px 0;color:var(--muted)}
table.stats td:first-child{color:var(--text)}
"""

INDEX_TMPL = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>City-Block Asset Pipeline</title><style>{css}</style></head>
<body><div class="wrap">
<header class="top"><h1>\U0001F3D9\uFE0F City-Block Asset Pipeline</h1>
<div class="sub">Live observability &mdash; generated {gen_time}</div></header>
<div class="pipes">{pipes}</div>
<h2>Schematics <span class="sub">{n_sch} items</span></h2>
<div class="grid">{sch_cards}</div>
<h2>3D Models <span class="sub">{n_mod} items</span></h2>
<div class="grid">{mod_cards}</div>
<p class="note">All times Pacific. Click any card for detail, iterations, and 3D view.</p>
</div></body></html>"""


def pipe_card(name, p):
    # A pipeline is RUNNING if it holds a lease and the worker is alive
    # (pid alive) OR has a fresh heartbeat (<10 min). PIDs go stale when
    # the sleep process dies but workers keep heartbeating via subagents.
    hb_fresh = False
    if p.get('heartbeat'):
        try:
            from datetime import datetime, timezone
            hb = datetime.fromisoformat(p['heartbeat'])
            age = (datetime.now(timezone.utc) - hb).total_seconds()
            hb_fresh = age < 600
        except Exception:
            pass
    live = p['lease_held'] and (p['pid_alive'] or hb_fresh)
    dot = 'live' if live else 'idle'
    label = 'RUNNING' if live else 'idle'
    rows = [f'<div class="name"><span class="dot {dot}"></span>{esc(name)} &mdash; {label}</div>']
    if p['worker']:
        rows.append(f'<div class="kv">worker <b>{esc(p["worker"])}</b></div>')
    if p['heartbeat']:
        hb = datetime.fromisoformat(p['heartbeat']).astimezone(LA).strftime('%b %d, %H:%M')
        rows.append(f'<div class="kv">heartbeat <b>{esc(hb)}</b></div>')
    lv = p['live'] or {}
    if lv:
        what = lv.get('model') or lv.get('concept') or '—'
        rows.append(f'<div class="kv">working on <b>{esc(what)}</b>'
                    f' &middot; phase <b>{esc(lv.get("phase", "?"))}</b>'
                    f' &middot; rev <b>{esc(lv.get("revision") or lv.get("item") or "?")}</b></div>')
        if lv.get('detail'):
            rows.append(f'<div class="kv">{esc(lv["detail"])}</div>')
    else:
        rows.append('<div class="kv">no live status reported</div>')
    return '<div class="pipe">' + ''.join(rows) + '</div>'


def sch_card(it):
    thumb = f'assets/schematics/{it["id"]}/thumb_front.jpg'
    if not os.path.exists(os.path.join(OUT, thumb)):
        thumb = f'assets/schematics/{it["id"]}/contact-sheet.png'
    scores = it['scores']
    avg = (sum(scores.values()) / len(scores)) if scores else None
    score_txt = f'avg {avg:.2f}' if avg else 'no scores'
    badge = '<span class="badge ok">finished</span>' if it['complete'] else '<span class="badge idle">partial</span>'
    return (f'<a class="card" href="schematics/{it["id"]}.html">'
            f'<img loading="lazy" src="{thumb}" alt="{esc(it["id"])}">'
            f'<div class="body"><div class="id">{esc(it["id"])}</div>'
            f'<div class="meta">{esc(it["title"][:40])}</div>'
            f'<div class="score">{score_txt} &middot; upd {fmt_ts(it["mtime"])}</div>'
            f'{badge}</div></a>')


def mod_card(it):
    # thumbnail: latest rev front render thumb, else placeholder
    thumb = None
    if it['revs']:
        r = it['revs'][-1]
        tp = f'assets/models/{it["id"]}/renders/{r["rev"]}/thumb_front.jpg'
        if os.path.exists(os.path.join(OUT, tp)):
            thumb = tp
    img = f'<img loading="lazy" src="{thumb}">' if thumb else '<div style="aspect-ratio:1.2;background:#000"></div>'
    badge_cls = {'finished': 'ok', 'in_progress': 'work', 'pending': 'idle'}.get(it['status'], 'idle')
    badge_txt = {'finished': 'finished', 'in_progress': 'in progress', 'pending': 'pending'}.get(it['status'], it['status'])
    glb = f' &middot; {esc(it["latest_glb"]["file"])}' if it['latest_glb'] else ''
    return (f'<a class="card" href="models/{it["id"]}.html">{img}'
            f'<div class="body"><div class="id">{esc(it["id"])}</div>'
            f'<div class="meta">{it["n_revs"]} revs{glb}</div>'
            f'<div class="score">upd {fmt_ts(it["mtime"])}</div>'
            f'<span class="badge {badge_cls}">{badge_txt}</span></div></a>')

# -------------------------------------------------------- detail pages ---

SCH_TMPL = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{id} &mdash; schematic</title><style>{css}</style></head>
<body><div class="wrap">
<a class="back" href="../index.html">&larr; pipeline overview</a>
<h1>{id}</h1><div class="sub">{title}</div>
<div class="kv">status <b>{status}</b> &middot; last updated <b>{mtime}</b></div>
<div class="kv">scores: <b>{scores}</b></div>
{sheets}
<h2>Canonical views</h2><div class="viewgrid">{views}</div>
<h2>Specs</h2><ul class="specs">{specs}</ul>
</div></body></html>"""


def render_schematic(it):
    sid = it['id']
    vcards = []
    for v in VIEWS:
        if v in it['views']:
            src = f'../assets/schematics/{sid}/{v}.jpg'
            sc = it['scores'].get(v)
            sc_txt = f'<span class="pass">{sc:.2f}</span>' if sc and sc >= 9 else (f'{sc:.2f}' if sc else '—')
            vcards.append(f'<div class="view"><img loading="lazy" src="{src}">'
                          f'<div class="lbl"><span>{VIEW_LABELS[v]}</span><span>{sc_txt}</span></div></div>')
    scores = it['scores']
    score_txt = ', '.join(f'{VIEW_LABELS.get(k, k)} {v:.2f}' for k, v in sorted(scores.items())) or 'none yet'
    specs = ''.join(f'<li>{s}</li>' for s in it['specs']) or '<li>—</li>'
    sheets = ''
    if it['has_contact_sheet']:
        sheets = (f'<h2>Contact sheet</h2><div class="view">'
                  f'<img loading="lazy" src="../assets/schematics/{sid}/contact-sheet.png"></div>')
    html = SCH_TMPL.format(
        css=CSS, id=esc(sid), title=esc(it['title']),
        status='finished (8/8 views)' if it['complete'] else 'partial',
        mtime=fmt_ts_long(it['mtime']), scores=esc(score_txt),
        sheets=sheets, views=''.join(vcards), specs=specs)
    write_if_changed(os.path.join(OUT, 'schematics', sid + '.html'), html)


VIEWER_JS = """
<script type="importmap">
{"imports":{"three":"https://unpkg.com/three@0.160.0/build/three.module.js",
"three/addons/":"https://unpkg.com/three@0.160.0/examples/jsm/"}}
</script>
<script type="module">
import * as THREE from 'three';
import {GLTFLoader} from 'three/addons/loaders/GLTFLoader.js';
import {OrbitControls} from 'three/addons/controls/OrbitControls.js';
const el=document.getElementById('viewer');
const renderer=new THREE.WebGLRenderer({antialias:true});
renderer.setPixelRatio(Math.min(devicePixelRatio,2));
renderer.setSize(el.clientWidth,420);
renderer.outputColorSpace=THREE.SRGBColorSpace;
el.appendChild(renderer.domElement);
const scene=new THREE.Scene();scene.background=new THREE.Color(0x0d1117);
const cam=new THREE.PerspectiveCamera(45,el.clientWidth/420,0.01,100);
cam.position.set(3.2,2.2,3.2);
const ctl=new OrbitControls(cam,renderer.domElement);
ctl.autoRotate=true;ctl.autoRotateSpeed=1.2;
scene.add(new THREE.HemisphereLight(0xffffff,0x334455,1.1));
const d=new THREE.DirectionalLight(0xfff2dd,2.2);d.position.set(4,6,3);scene.add(d);
const d2=new THREE.DirectionalLight(0x88aaff,0.7);d2.position.set(-4,3,-3);scene.add(d2);
new GLTFLoader().load(GLB_URL,g=>{
  const o=g.scene;scene.add(o);
  const b=new THREE.Box3().setFromObject(o),c=b.getCenter(new THREE.Vector3()),s=b.getSize(new THREE.Vector3());
  const m=Math.max(s.x,s.y,s.z);cam.position.set(c.x+m*1.1,c.y+m*0.8,c.z+m*1.1);
  ctl.target.copy(c);ctl.update();
  document.getElementById('vstat').textContent='loaded ✓ drag to orbit · scroll to zoom';
},undefined,e=>{document.getElementById('vstat').textContent='failed to load GLB';});
(function loop(){requestAnimationFrame(loop);ctl.update();renderer.render(scene,cam);})();
addEventListener('resize',()=>{renderer.setSize(el.clientWidth,420);
cam.aspect=el.clientWidth/420;cam.updateProjectionMatrix();});
</script>"""

MOD_TMPL = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{id} &mdash; 3D model</title><style>{css}</style></head>
<body><div class="wrap">
<a class="back" href="../index.html">&larr; pipeline overview</a>
<h1>{id}</h1>
<div class="kv">status <b>{status}</b> &middot; queue <b>{qstat}</b> &middot; last updated <b>{mtime}</b></div>
{mesh}{judges}
{viewer}
<h2>Iterations ({n_revs})</h2>{revs}
</div></body></html>"""


def render_model(it):
    mid = it['id']
    mesh = ''
    if it['mesh']:
        m = it['mesh']
        mesh = ('<table class="stats"><tr><td>objects</td><td>{o}</td></tr>'
                '<tr><td>triangles</td><td>{t}</td></tr>'
                '<tr><td>non-manifold</td><td>{n}</td></tr></table>').format(
            o=m.get('objects', '—'), t=m.get('tris', '—'), n=m.get('nonmanifold_edges', '—'))
    judges = ''
    if it['scores']:
        rows = []
        for v in VIEWS:
            if v in it['scores']:
                verdict, sc = it['scores'][v]
                cls = 'pass' if verdict == 'PASS' else 'fail'
                rows.append(f'{VIEW_LABELS[v]} <span class="{cls}">{sc:.2f}</span>')
        judges = f'<div class="kv">judges: <b>{" · ".join(rows)}</b></div>'
    elif it['latest_glb']:
        judges = '<div class="kv">judges: <b>exported GLB (passed per pipeline)</b></div>'
    viewer = ''
    if it['latest_glb']:
        g = it['latest_glb']
        viewer = (f'<h2>3D viewer &mdash; {esc(g["file"])}</h2>'
                  f'<div id="viewer"></div><div class="kv" id="vstat">loading…</div>'
                  f'<script>const GLB_URL="../assets/models/{mid}/{esc(g["file"])}";</script>'
                  + VIEWER_JS)
    rev_html = []
    for r in reversed(it['revs']):
        cards = []
        for v in VIEWS:
            if v in r['renders']:
                src = f'../assets/models/{mid}/renders/{r["rev"]}/{v}.png'
                cards.append(f'<div class="view"><img loading="lazy" src="{src}">'
                             f'<div class="lbl"><span>{VIEW_LABELS[v]}</span></div></div>')
        # also include any non-canonical pngs
        for name, _ in r['renders'].items():
            if name not in VIEWS:
                src = f'../assets/models/{mid}/renders/{r["rev"]}/{name}.png'
                cards.append(f'<div class="view"><img loading="lazy" src="{src}">'
                             f'<div class="lbl"><span>{esc(name)}</span></div></div>')
        rev_html.append(f'<div class="rev"><h3>rev {esc(r["rev"])} '
                        f'<span class="note">&middot; {fmt_ts_long(r["mtime"])}</span></h3>'
                        f'<div class="viewgrid">{"".join(cards)}</div></div>')
    html = MOD_TMPL.format(
        css=CSS, id=esc(mid), status=esc(it['status']),
        qstat=esc(it['queue_status']), mtime=fmt_ts_long(it['mtime']),
        mesh=mesh, judges=judges, viewer=viewer,
        n_revs=it['n_revs'], revs=''.join(rev_html) or '<p class="note">no renders yet</p>')
    write_if_changed(os.path.join(OUT, 'models', mid + '.html'), html)


# ----------------------------------------------------------------- main ---

def main():
    t0 = datetime.now(timezone.utc)
    # queue statuses for models
    queue_status = {}
    try:
        st = json.load(open(os.path.join(
            HIDDEN, 'modeling-pipeline-state.json')))
        for q in st.get('queue', []):
            queue_status[q['id']] = q.get('status', 'queued')
    except Exception:
        pass

    schematics = collect_schematics()
    models = collect_models(queue_status)
    pipelines = collect_pipelines()

    fp = fingerprint(schematics, models, pipelines)
    meta_path = os.path.join(OUT, 'dashboard-meta.json')
    old_meta = {}
    try:
        old_meta = json.load(open(meta_path))
    except Exception:
        pass
    if old_meta.get('fingerprint') == fp and os.path.exists(os.path.join(OUT, 'index.html')):
        print(f'no changes (fingerprint {fp[:12]}), skipping')
        return

    copied = sync_assets(schematics, models)

    pipes = pipe_card('3D modeling pipeline', pipelines['modeling']) + \
            pipe_card('Schematic pipeline', pipelines['schematic'])
    index = INDEX_TMPL.format(
        css=CSS,
        gen_time=datetime.now(LA).strftime('%Y-%m-%d %H:%M %Z'),
        pipes=pipes,
        n_sch=len(schematics), n_mod=len(models),
        sch_cards=''.join(sch_card(it) for it in schematics),
        mod_cards=''.join(mod_card(it) for it in models),
    )
    write_if_changed(os.path.join(OUT, 'index.html'), index)
    for it in schematics:
        render_schematic(it)
    for it in models:
        render_model(it)

    meta = {
        'generated_at': t0.isoformat(),
        'generated_at_pacific': t0.astimezone(LA).strftime('%Y-%m-%d %H:%M %Z'),
        'fingerprint': fp,
        'n_schematics': len(schematics),
        'n_models': len(models),
        'assets_copied': copied,
    }
    write_if_changed(meta_path, json.dumps(meta, indent=2))
    # .nojekyll so GitHub Pages serves _assets etc without Jekyll processing
    write_if_changed(os.path.join(OUT, '.nojekyll'), '')
    dt = (datetime.now(timezone.utc) - t0).total_seconds()
    print(f'done: {len(schematics)} schematics, {len(models)} models, '
          f'{copied} assets copied, {dt:.1f}s')


if __name__ == '__main__':
    main()
