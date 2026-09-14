#!/usr/bin/env python3
"""City-block 3D asset pipeline — observability + review dashboard generator.

Generates a pure static HTML site for GitHub Pages. Idempotent: safe to run
every 5 minutes; skips when the source fingerprint is unchanged.

Layout (revamped 2026-09-13 per Felix):
- index.html: ONE vertical scrollable list of entries (one per asset id).
  Each entry shows its pipeline stages: Schematics -> 3D modeling -> Accepted.
- entries/<id>.html: detail page per asset:
  - 3D GLB viewport (no auto-rotate)
  - Review mode: per view (front/back/...) the latest 3D render side-by-side
    with the matching schematic; right-click (or long-press) either image to
    drop a pin at that pixel and leave a comment. Pins persist in the
    browser's localStorage.
- "Copy feedback" button on the index: dumps all localStorage review notes
  into the clipboard as paste-ready text for Kit, then clears local storage.

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
    """Return {id: schematic item} with metadata."""
    base = os.path.join(PACK, 'schematics')
    items = {}
    if not os.path.isdir(base):
        return items
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
        items[sid] = {
            'id': sid,
            'title': meta.get('title', sid),
            'views': views,                       # view -> source path
            'scores': meta.get('scores', {}),
            'specs': meta.get('specs', []),
            'complete': len(views) == 8,
            'mtime': max(mtimes) if mtimes else 0,
        }
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


def feedback_states(text):
    """Order-aware tracking for Felix-flagged AND Judge-flagged defects
    (mirrors model_audit._feedback_states).

    Returns (outstanding, resolved), both newest-first: outstanding
    [(name, rev, full, source)], resolved [(name, resolved_in_rev, source)].
    """
    events = []
    rawlines = text.splitlines()
    for i, line in enumerate(rawlines):
        m = re.search(r'((?:Felix|Judge)-flagged defect):\s*(.+)', line)
        if m:
            fsrc = 'felix' if m.group(1).startswith('Felix') else 'judge'
            d = m.group(2).strip()
            name = re.split(r'\s+—\s+', d)[0].split('(')[0].strip().lower()
            rm = re.search(r'\b([a-z]{2}\d+)\b', d)
            if name:
                events.append(('flag', name, rm.group(1) if rm else '', d,
                               fsrc))
            continue
        m = re.search(r'RESOLVED\s*\(((?:Felix|Judge)-flagged defect)\):\s*(.+)',
                      line)
        if m:
            rsrc = 'felix' if m.group(1).startswith('Felix') else 'judge'
            r = m.group(2).strip()
            if (i + 1 < len(rawlines)
                    and not re.search(r'fixed in\s+[a-z]{2}\d+', r, re.I)):
                r += ' ' + rawlines[i + 1].strip()
            if '<name>' in r:
                continue  # instruction template, not a real resolution
            rname = re.split(r'\s+—\s+', r)[0].split('(')[0].strip().lower()
            if rname:
                events.append(('resolve', rname, '', r, rsrc))
    state, seq = {}, 0
    for kind, name, rev, full, fsrc in events:
        seq += 1
        key = (fsrc, name)
        if kind == 'flag':
            if key not in state or state[key][0] != 'flag':
                state[key] = ['flag', rev, full, '', seq, fsrc]
            elif rev and not state[key][1]:
                state[key][1] = rev
                state[key][4] = seq
        else:
            for (s, n) in list(state):
                ent = state[(s, n)]
                if ent[0] == 'flag' and s == fsrc and (
                        n == name or n in name or name in n):
                    rm = re.search(r'fixed in\s+([a-z]{2}\d+)', full, re.I)
                    state[(s, n)] = ['resolved', ent[1], ent[2],
                                     rm.group(1) if rm else '', seq, fsrc]
    by_newest = sorted(state, key=lambda k: state[k][4], reverse=True)
    outstanding, resolved = [], []
    for (s, n) in by_newest:
        ent = state[(s, n)]
        if ent[0] == 'flag':
            outstanding.append([n, ent[1], ent[2], ent[5]])
        else:
            resolved.append([n, ent[3], ent[5]])
    return outstanding, resolved


def outstanding_feedback(mdir):
    """Unresolved Felix/Judge-flagged defects + recently resolved ones.

    Returns (outstanding, resolved): outstanding is newest-first [{'name',
    'rev', 'src'}]; resolved is newest-first [{'name', 'rev', 'src'}]
    (rev = fix rev, src = 'felix' or 'judge').
    """
    texts = []
    try:
        files = sorted(os.listdir(mdir))
    except OSError:
        return [], []
    for f in files:
        if not (f == 'model-qa-log.md'
                or re.match(r'(model-qa-log|qa-log)-.+\.md$', f)):
            continue
        try:
            texts.append(open(os.path.join(mdir, f),
                              encoding='utf-8', errors='replace').read())
        except OSError:
            continue
    outstanding, resolved = feedback_states('\n'.join(texts))
    return ([{'name': n, 'rev': r, 'src': s, 'full': f}
             for n, r, f, s in outstanding],
            [{'name': n, 'rev': r, 'src': s} for n, r, s in resolved])


def collect_models(queue_status):
    """Return {id: model item} with iterations."""
    base = os.path.join(PACK, 'models')
    items = {}
    live_model, live_phase = '', ''
    try:
        live = json.load(open(os.path.join(HIDDEN, 'modeling-live-status.json')))
        live_model = live.get('model', '')
        live_phase = live.get('phase', '')
    except Exception:
        pass
    if not os.path.isdir(base):
        return items
    for mid in sorted(os.listdir(base)):
        mdir = os.path.join(base, mid)
        if not os.path.isdir(mdir):
            continue
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
                    for f in os.listdir(revp):
                        if f.endswith('.png'):
                            pngs[f[:-4]] = os.path.join(revp, f)
                rev_mtime = max([mtime_of(p) for p in pngs.values()] or [0])
                revs.append({'rev': rev, 'renders': pngs, 'mtime': rev_mtime})
        revs.sort(key=lambda r: r['mtime'])
        # Featured revision = latest rev with a COMPLETE render set (all 8 views).
        # GLB export happens once per completed build, NOT per rendered rev, so
        # the GLB can lag the featured renders (e.g. a model mid-fix with revs
        # bs10-bs13 rendered but the GLB still from bs9). Renders and GLB are a
        # self-consistent pair only when the GLB's rev matches the featured rev;
        # otherwise the viewer section gets an explicit rev label + mismatch
        # note (Felix 2026-09-14: unlabeled revision mixing on one page is a bug).
        # A newer partial rev (crashed/interrupted render) is reported separately
        # as pending — never mixed with the finished GLB.
        complete_revs = [r for r in revs if len(r['renders']) >= len(VIEWS)]
        featured = complete_revs[-1] if complete_revs else (revs[-1] if revs else None)
        pending_rev = (revs[-1]['rev'] if revs and (not featured or revs[-1]['rev'] != featured['rev']) else None)
        glbs = []
        for f in sorted(os.listdir(mdir)):
            if f.endswith('.glb'):
                p = os.path.join(mdir, f)
                glbs.append({'file': f, 'mtime': mtime_of(p), 'size': os.path.getsize(p)})
        glbs.sort(key=lambda g: g['mtime'])
        # GLB->rev pairing by disk truth (see comment at featured-rev above):
        # the GLB belongs to the newest rev fully rendered before its export.
        glb_rev = None
        if glbs and revs:
            gmt = glbs[-1]['mtime']
            cands = [r for r in revs if r['mtime'] <= gmt]
            if cands:
                glb_rev = max(cands, key=lambda r: r['mtime'])['rev']
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
        mesh = {}
        for f in sorted(os.listdir(mdir)):
            if f.startswith('mesh-stats') and f.endswith('.json'):
                try:
                    mesh = json.load(open(os.path.join(mdir, f)))
                except Exception:
                    pass
        builds = [f for f in sorted(os.listdir(mdir))
                  if f.endswith('.py') and f.startswith('build')]
        mtimes = [mtime_of(os.path.join(mdir, f)) for f in os.listdir(mdir)]
        mtimes += [r['mtime'] for r in revs] + [g['mtime'] for g in glbs]
        has_build_activity = bool(builds) or bool(revs)
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
        # GLB->rev pairing by DISK TRUTH: the GLB belongs to the newest rev
        # whose render set was complete (max render mtime) at or before the
        # GLB export. model-meta 'rev' is NOT used — it goes stale after
        # post-completion tweaks (e.g. 02-apartment meta said bs14 while the
        # exported GLB was bs15's; 01-cafe bs11 tweak re-exported the GLB
        # without touching meta).
        felix_blocked = False
        for qf in qa_logs:
            try:
                content = open(os.path.join(mdir, qf['file'])).read()
                if 'Felix-flagged defect' in content:
                    last_flag = content.rfind('Felix-flagged defect')
                    after = content[last_flag:]
                    if 'resolved' not in after.lower() and 'fixed in' not in after.lower():
                        felix_blocked = True
            except Exception:
                pass
        qs = queue_status.get(mid, '')
        live_active = (live_model == mid and live_phase not in ('', 'idle'))
        if live_active or qs in ('building', 'judging', 'fixing', 'rendering', 'in_progress'):
            status = 'in_progress'
        elif felix_blocked:
            status = 'in_progress'
        elif qs == 'complete' or (glbs and meta_ok):
            status = 'finished'
        elif has_build_activity:
            status = 'in_progress'
        else:
            status = qs or 'pending'
        all_pass = (scores and all(v[0] == 'PASS' for v in scores.values())
                    and len(scores) == 8)
        out_fb, res_fb = outstanding_feedback(mdir)
        items[mid] = {
            'id': mid,
            'revs': revs,
            'latest_rev': featured['rev'] if featured else None,
            'pending_rev': pending_rev,
            'glbs': glbs,
            'latest_glb': glbs[-1] if glbs else None,
            'glb_rev': glb_rev,  # rev the exported GLB belongs to (None if unknown)
            'scores': scores,
            'all_judges_pass': bool(all_pass),
            'mesh': mesh,
            'builds': builds,
            'status': status,
            'queue_status': queue_status.get(mid, '—'),
            'felix_blocked': felix_blocked,
            'outstanding': out_fb,
            'recently_resolved': res_fb,
            'live_phase': live_phase if live_model == mid else '',
            'live_active': live_active,
            'mtime': max(mtimes) if mtimes else 0,
            'n_revs': len(revs),
        }
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
    """Hash of source mtimes+sizes+state AND this script, so template
    changes force regeneration."""
    h = hashlib.sha256()

    def feed(s):
        h.update(str(s).encode())
    try:
        feed(open(__file__, 'r', encoding='utf-8').read())
    except Exception:
        pass
    for sid, it in schematics.items():
        for v, p in it['views'].items():
            st = os.stat(p)
            feed((p, st.st_mtime, st.st_size))
        feed((sid, it['mtime']))
    for mid, it in models.items():
        for r in it['revs']:
            for v, p in r['renders'].items():
                st = os.stat(p)
                feed((p, st.st_mtime, st.st_size))
        for g in it['glbs']:
            feed((g['file'], g['mtime'], g['size']))
        feed((mid, it['status'], it['mtime'], it['felix_blocked']))
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
    for sid, it in schematics.items():
        for v, src in it['views'].items():
            dst = os.path.join(OUT, 'assets', 'schematics', sid, v + '.jpg')
            thumb = os.path.join(OUT, 'assets', 'schematics', sid, 'thumb_' + v + '.jpg')
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
    for mid, it in models.items():
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
header.top{border-bottom:1px solid var(--border);padding:16px;margin-bottom:16px;
display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
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
.outstanding{background:#3a2b12;border:1px solid #8a6d2f;border-radius:10px;padding:12px 14px;margin:12px 0}
.otitle{font-weight:700;color:#f0c96a;margin-bottom:6px}
.outstanding ul{margin:6px 0 6px 18px;padding:0}
.outstanding li{margin:4px 0;font-size:14px}
.orev{font-size:12px;color:var(--muted);border:1px solid var(--muted);border-radius:4px;padding:0 5px;margin-left:6px}
.ohint{font-size:12px;color:var(--muted);margin-top:6px}
.outstanding.clear{border-color:#3f6b3f;background:#1c2b1c}
.oclear{font-size:14px;color:#9fd69f;margin-top:6px}
.srcchip{display:inline-block;font-size:11px;font-weight:700;border-radius:4px;padding:1px 6px;margin-right:6px;vertical-align:1px}
.srcchip.felix{background:#3a2c12;color:#e8b84b;border:1px solid #8a6a2a}
.srcchip.judge{background:#1c2c4a;color:#8fb8ff;border:1px solid #3a5a8a}
details.fb summary{cursor:pointer;list-style:none}
details.fb summary::-webkit-details-marker{display:none}
details.fb summary::before{content:'▸ ';color:#8a8a8a;font-size:12px}
details.fb[open] summary::before{content:'▾ '}
.fbdetail{font-size:13px;color:#d8d0bd;margin:6px 0 4px 18px;line-height:1.45}
.resolvedbox{background:#1c2b1c;border:1px solid #3f6b3f;border-radius:10px;padding:12px 14px;margin:12px 0}
.rtitle{font-weight:700;color:#9fd69f;margin-bottom:6px}
.resolvedbox ul{margin:6px 0 6px 18px;padding:0}
.resolvedbox li{margin:4px 0;font-size:14px;color:var(--text)}
/* entry list */
.entry{display:flex;gap:14px;background:var(--card);border:1px solid var(--border);
border-radius:12px;padding:12px;margin:10px 0;text-decoration:none;color:var(--text)}
.entry:hover{border-color:var(--blue)}
.entry img{width:120px;height:100px;object-fit:cover;border-radius:8px;background:#000;flex:none}
.entry .info{flex:1;min-width:0}
.entry .id{font-weight:700;font-size:16px}
.entry .meta{font-size:12px;color:var(--muted);margin-top:2px}
.stages{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.stage{font-size:11px;padding:3px 10px;border-radius:12px;border:1px solid var(--border)}
.stage.ok{background:rgba(63,185,80,.15);color:var(--green);border-color:transparent}
.stage.work{background:rgba(210,153,34,.15);color:var(--yellow);border-color:transparent}
.stage.idle{color:var(--muted)}
.badge{display:inline-block;font-size:11px;padding:2px 8px;border-radius:12px;margin-top:4px}
.badge.ok{background:rgba(63,185,80,.15);color:var(--green)}
.badge.work{background:rgba(210,153,34,.15);color:var(--yellow)}
.badge.idle{background:rgba(139,148,158,.15);color:var(--muted)}
/* feedback button */
.fbbox{background:var(--card);border:1px solid var(--border);border-radius:10px;
padding:12px;margin:12px 0;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.btn{background:var(--blue);color:#fff;border:none;border-radius:8px;padding:10px 18px;
font-size:15px;font-weight:700;cursor:pointer}
.btn:disabled{background:var(--border);color:var(--muted);cursor:default}
.btn.ghost{background:transparent;border:1px solid var(--border);color:var(--text)}
.count{background:var(--yellow);color:#000;font-weight:700;border-radius:12px;
padding:2px 10px;font-size:13px}
/* detail page */
.back{display:inline-block;margin:12px 0;color:var(--blue);text-decoration:none}
#viewer{width:100%;height:420px;background:#000;border-radius:10px;border:1px solid var(--border)}
.viewtabs{display:flex;gap:6px;flex-wrap:wrap;margin:12px 0}
.viewtab{background:var(--card);border:1px solid var(--border);color:var(--text);
border-radius:8px;padding:8px 14px;font-size:14px;cursor:pointer}
.viewtab.active{background:var(--blue);border-color:var(--blue);color:#fff}
.compare{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:700px){.compare{grid-template-columns:1fr}}
.imgwrap{position:relative;background:#000;border-radius:10px;overflow:hidden;
border:1px solid var(--border);touch-action:pan-y}
.imgwrap img{width:100%;display:block;user-select:none;-webkit-user-select:none}
.imglbl{position:absolute;top:8px;left:8px;background:rgba(0,0,0,.65);color:#fff;
font-size:12px;padding:3px 10px;border-radius:10px;pointer-events:none}
.pin{position:absolute;width:26px;height:26px;border-radius:50%;background:var(--red);
color:#fff;font-size:13px;font-weight:700;display:flex;align-items:center;
justify-content:center;transform:translate(-50%,-50%);cursor:pointer;
border:2px solid #fff;box-shadow:0 1px 6px rgba(0,0,0,.6);z-index:5}
.pinedit{position:absolute;z-index:20;background:var(--card);border:1px solid var(--blue);
border-radius:10px;padding:10px;width:240px;box-shadow:0 4px 20px rgba(0,0,0,.6)}
.pinedit textarea{width:100%;height:70px;background:var(--bg);color:var(--text);
border:1px solid var(--border);border-radius:6px;padding:6px;font-size:13px;resize:vertical}
.pinedit .row{display:flex;gap:8px;margin-top:8px}
.pinedit button{flex:1;padding:6px;border-radius:6px;border:1px solid var(--border);
background:var(--bg);color:var(--text);cursor:pointer;font-size:13px}
.pinedit button.save{background:var(--blue);border-color:var(--blue);color:#fff}
.pinedit button.del{background:transparent;color:var(--red);border-color:var(--red)}
.reviewbar{display:flex;gap:10px;align-items:center;margin:12px 0;flex-wrap:wrap}
.toggle{display:flex;align-items:center;gap:8px;font-size:14px;cursor:pointer}
.toggle .pill{width:44px;height:24px;border-radius:12px;background:var(--border);position:relative}
.toggle.on .pill{background:var(--green)}
.toggle .pill::after{content:'';position:absolute;width:18px;height:18px;border-radius:50%;
background:#fff;top:3px;left:3px;transition:left .15s}
.toggle.on .pill::after{left:23px}
.hint{font-size:13px;color:var(--muted)}
.notes{margin:16px 0}
.noteitem{background:var(--card);border:1px solid var(--border);border-radius:10px;
padding:10px 12px;margin:8px 0;font-size:14px}
.noteitem .nmeta{font-size:12px;color:var(--muted);margin-bottom:4px}
.noteitem .nmeta a{color:var(--blue)}
.noteitem button{background:transparent;border:1px solid var(--red);color:var(--red);
border-radius:6px;padding:2px 10px;font-size:12px;cursor:pointer;float:right}
table.stats{border-collapse:collapse;font-size:13px;margin:8px 0}
table.stats td{padding:3px 12px 3px 0;color:var(--muted)}
table.stats td:first-child{color:var(--text)}
.note{font-size:12px;color:var(--muted)}
#dumpbox{width:100%;height:180px;background:var(--bg);color:var(--text);
border:1px solid var(--border);border-radius:8px;padding:8px;font-size:12px;display:none}
"""

INDEX_TMPL = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>City-Block Asset Pipeline</title><style>%%CSS%%</style></head>
<body><div class="wrap">
<header class="top"><div><h1>\U0001F3D9\uFE0F City-Block Asset Pipeline</h1>
<div class="sub">Live observability &mdash; generated %%GEN_TIME%%</div></div></header>
<div class="pipes">%%PIPES%%</div>
<div class="fbbox">
<button class="btn" id="copyFeedback">\U0001F4CB Copy feedback</button>
<span class="count" id="fbCount">0</span>
<span class="hint">Review notes you pinned on entry pages are saved in this browser.
Copy them to your clipboard, then paste into chat with Kit &mdash; each note becomes a targeted fix.</span>
</div>
<textarea id="dumpbox" readonly></textarea>
<h2>Assets <span class="sub">%%N%% entries</span></h2>
<div id="entries">%%ENTRIES%%</div>
<p class="note">All times Pacific. Click an entry for the 3D viewer and side-by-side review mode.</p>
</div>
<script>
var KEY='cb_feedback_v1';
function loadFB(){try{return JSON.parse(localStorage.getItem(KEY))||[]}catch(e){return[]}}
document.getElementById('fbCount').textContent=loadFB().length;
document.getElementById('copyFeedback').addEventListener('click',function(){
  var notes=loadFB();
  if(!notes.length){alert('No review notes saved yet. Open an entry, use review mode, right-click an image to pin a note.');return;}
  var now=new Date();
  var pad=function(n){return (n<10?'0':'')+n;};
  var head='CITY-BLOCK FEEDBACK \\u2014 '+now.getFullYear()+'-'+pad(now.getMonth()+1)+'-'+pad(now.getDate())+
    ' '+pad(now.getHours())+':'+pad(now.getMinutes())+' \\u2014 '+notes.length+' notes (paste into chat with Kit)';
  var parts=[head];
  notes.forEach(function(n){
    parts.push('['+n.asset+' | rev '+(n.rev||'\\u2014')+' | '+n.view+' | '+n.side+
      ' @ x='+Math.round(n.x*100)+'% y='+Math.round(n.y*100)+'% | '+n.ts+']');
    parts.push(n.text);
    parts.push('---');
  });
  var txt=parts.join('\\n');
  function done(ok){
    var msg=document.querySelector('.fbbox .hint');
    if(ok){localStorage.removeItem(KEY);document.getElementById('fbCount').textContent='0';
      msg.textContent='\\u2713 Copied '+notes.length+' notes to clipboard \\u2014 local feedback cleared. Paste into chat with Kit.';}
    else{var box=document.getElementById('dumpbox');box.style.display='block';box.value=txt;
      box.select();msg.textContent='Clipboard blocked \\u2014 copy the text above manually, then clear with the button below.';}
  }
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(txt).then(function(){done(true)},function(){done(false)});
  }else{done(false);}
});
</script>
</body></html>"""


def pipe_card(name, p):
    hb_fresh = False
    if p.get('heartbeat'):
        try:
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


def entry_stages(sch, mod):
    """Stage badges for one asset: Schematics -> 3D modeling -> Accepted."""
    stages = []
    if sch and sch['complete']:
        stages.append(('Schematics ✓', 'ok'))
    elif sch:
        stages.append((f'Schematics {len(sch["views"])}/8', 'work'))
    else:
        stages.append(('No schematics', 'idle'))
    if mod:
        st = mod['status']
        if st == 'finished':
            stages.append(('3D modeling ✓', 'ok'))
        elif st == 'in_progress':
            lbl = f'3D modeling · {mod["n_revs"]} revs'
            # Prefer the live worker phase over the (possibly stale) queue
            # status: "fixing" from the queue while the worker is judging is
            # exactly the contradiction Felix flagged 2026-09-13.
            phase = mod.get('live_phase') or mod['queue_status']
            if mod.get('live_active') and mod.get('live_phase'):
                phase = mod['live_phase']
            elif phase in ('—', 'in_progress'):
                phase = ''
            if phase:
                lbl += f' ({phase})'
            stages.append((lbl, 'work'))
        else:
            stages.append((f'3D {st}', 'idle'))
    else:
        stages.append(('3D not started', 'idle'))
    if mod and mod['status'] == 'finished' and not mod['felix_blocked']:
        stages.append(('Accepted ✓', 'ok'))
    elif mod and mod['felix_blocked']:
        stages.append(('Felix review pending', 'work'))
    return stages


def entry_card(aid, sch, mod):
    thumb = None
    if mod and mod['latest_rev']:
        tp = f'assets/models/{aid}/renders/{mod["latest_rev"]}/thumb_front.jpg'
        if os.path.exists(os.path.join(OUT, tp)):
            thumb = tp
    if not thumb and sch:
        tp = f'assets/schematics/{aid}/thumb_front.jpg'
        if os.path.exists(os.path.join(OUT, tp)):
            thumb = tp
    img = f'<img loading="lazy" src="{thumb}" alt="">' if thumb else \
        '<img alt="" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7">'
    title = sch['title'] if sch and sch.get('title') else aid
    stages = ''.join(f'<span class="stage {cls}">{esc(lbl)}</span>'
                     for lbl, cls in entry_stages(sch, mod))
    meta_bits = []
    if sch and sch['scores']:
        avg = sum(sch['scores'].values()) / len(sch['scores'])
        meta_bits.append(f'schematics avg {avg:.2f}')
    if mod and mod['latest_rev']:
        meta_bits.append(f'rev {esc(mod["latest_rev"])}')
    if mod and mod['latest_glb']:
        meta_bits.append(esc(mod['latest_glb']['file']))
    mtime = max(sch['mtime'] if sch else 0, mod['mtime'] if mod else 0)
    meta_bits.append(f'upd {fmt_ts(mtime)}')
    return (f'<a class="entry" href="entries/{aid}.html">{img}'
            f'<div class="info"><div class="id">{esc(aid)}</div>'
            f'<div class="meta">{esc(title[:60])}</div>'
            f'<div class="meta">{" · ".join(meta_bits)}</div>'
            f'<div class="stages">{stages}</div></div></a>')


# -------------------------------------------------------- detail pages ---

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

# Review-mode JS: pin notes on images, persisted to localStorage.
REVIEW_JS = """
(function(){
var KEY='cb_feedback_v1';
var ASSET=document.body.dataset.asset, REV=document.body.dataset.rev;
function loadFB(){try{return JSON.parse(localStorage.getItem(KEY))||[]}catch(e){return[]}}
function saveFB(a){try{localStorage.setItem(KEY,JSON.stringify(a))}catch(e){}}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
var view='front', reviewOn=true, wraps={};
function $(id){return document.getElementById(id)}
function setView(v){
  view=v;
  document.querySelectorAll('.viewtab').forEach(function(b){b.classList.toggle('active',b.dataset.view===v)});
  var d=VIEWS_DATA[v]||{};
  setImg('render',d.render); setImg('schematic',d.schematic);
  renderPins(); renderNotes();
}
function setImg(side,src){
  var w=wraps[side], img=w.querySelector('img'), none=w.querySelector('.nonimg');
  w.querySelectorAll('.pin,.pinedit').forEach(function(e){e.remove()});
  if(src){img.style.display='block';img.src=src;if(none)none.style.display='none';}
  else{img.style.display='none';if(none)none.style.display='flex';}
}
function pinsFor(v){return loadFB().filter(function(n){return n.asset===ASSET&&n.view===v})}
function renderPins(){
  ['render','schematic'].forEach(function(side){
    var w=wraps[side];
    w.querySelectorAll('.pin,.pinedit').forEach(function(e){e.remove()});
    pinsFor(view).filter(function(n){return n.side===side}).forEach(function(n,i){
      var p=document.createElement('div');p.className='pin';p.textContent=(i+1);
      p.style.left=(n.x*100)+'%';p.style.top=(n.y*100)+'%';p.title=n.text;
      p.addEventListener('click',function(ev){ev.stopPropagation();openEditor(w,n);});
      w.appendChild(p);
    });
  });
}
function openEditor(w,existing){
  w.querySelectorAll('.pinedit').forEach(function(e){e.remove()});
  var d=document.createElement('div');d.className='pinedit';
  var r=w.getBoundingClientRect();
  d.style.left='50%';d.style.top='50%';d.style.transform='translate(-50%,-50%)';
  var ta=document.createElement('textarea');
  ta.placeholder='What should change here?';ta.value=existing?existing.text:'';
  var row=document.createElement('div');row.className='row';
  var sv=document.createElement('button');sv.className='save';sv.textContent=existing?'Update':'Save note';
  var del=document.createElement('button');del.className='del';del.textContent='Delete';
  var ca=document.createElement('button');ca.textContent='Cancel';
  sv.onclick=function(){
    var t=ta.value.trim();if(!t){ta.focus();return;}
    var all=loadFB();
    if(existing){all.forEach(function(n){if(n.id===existing.id)n.text=t;});}
    else{all.push({id:'n'+Date.now().toString(36)+Math.floor(Math.random()*1e4),
      asset:ASSET,rev:REV,view:view,side:w.dataset.side,
      x:pending.x,y:pending.y,text:t,
      ts:new Date().toISOString().slice(0,16).replace('T',' ')});}
    saveFB(all);d.remove();renderPins();renderNotes();
  };
  del.onclick=function(){
    if(existing){saveFB(loadFB().filter(function(n){return n.id!==existing.id}));}
    d.remove();renderPins();renderNotes();
  };
  ca.onclick=function(){d.remove()};
  row.appendChild(sv);if(existing)row.appendChild(del);row.appendChild(ca);
  d.appendChild(ta);d.appendChild(row);w.appendChild(d);ta.focus();
}
var pending=null, pressTimer=null;
function armWrap(side){
  var w=$('wrap-'+side);wraps[side]=w;w.dataset.side=side;
  function dropAt(clientX,clientY){
    if(!reviewOn)return;
    var img=w.querySelector('img');if(!img||img.style.display==='none')return;
    var r=img.getBoundingClientRect();
    var x=(clientX-r.left)/r.width, y=(clientY-r.top)/r.height;
    if(x<0||x>1||y<0||y>1)return;
    pending={x:x,y:y};openEditor(w,null);
  }
  w.addEventListener('contextmenu',function(e){e.preventDefault();dropAt(e.clientX,e.clientY);});
  w.addEventListener('touchstart',function(e){
    pressTimer=setTimeout(function(){
      var t=e.touches[0];dropAt(t.clientX,t.clientY);pressTimer=null;
    },600);
  },{passive:true});
  ['touchend','touchmove'].forEach(function(ev){w.addEventListener(ev,function(){
    if(pressTimer){clearTimeout(pressTimer);pressTimer=null;}
  },{passive:true});});
}
function renderNotes(){
  var list=$('notesList');var ns=loadFB().filter(function(n){return n.asset===ASSET});
  $('notesCount').textContent=ns.length;
  if(!ns.length){list.innerHTML='<p class="hint">No notes yet. Turn on review mode and right-click (or long-press) any image to pin a note.</p>';return;}
  ns.sort(function(a,b){return (a.ts<b.ts?-1:1)});
  list.innerHTML='';
  ns.forEach(function(n,i){
    var d=document.createElement('div');d.className='noteitem';
    var del=document.createElement('button');del.textContent='Delete';
    del.onclick=function(){saveFB(loadFB().filter(function(x){return x.id!==n.id}));renderPins();renderNotes();};
    var meta=document.createElement('div');meta.className='nmeta';
    var a=document.createElement('a');a.href='#';a.textContent=n.view+' · '+n.side;
    a.onclick=function(e){e.preventDefault();setView(n.view);window.scrollTo(0,$('review').offsetTop-10);};
    meta.appendChild(a);
    meta.insertAdjacentHTML('beforeend',' · rev '+esc(n.rev||'—')+' · x='+Math.round(n.x*100)+'% y='+Math.round(n.y*100)+'% · '+esc(n.ts));
    var body=document.createElement('div');body.textContent=(i+1)+'. '+n.text;
    d.appendChild(del);d.appendChild(meta);d.appendChild(body);list.appendChild(d);
  });
}
document.querySelectorAll('.viewtab').forEach(function(b){
  b.addEventListener('click',function(){setView(b.dataset.view)});
});
var tg=$('reviewToggle');
tg.addEventListener('click',function(){reviewOn=!reviewOn;tg.classList.toggle('on',reviewOn);
  $('reviewHint').textContent=reviewOn?
   'Review mode ON — right-click (or long-press) either image to drop a pin and leave a note.':
   'Review mode OFF — right-click works normally again.';});
armWrap('render');armWrap('schematic');
setView('front');
})();
"""

ENTRY_TMPL = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%%ID%% &mdash; asset review</title><style>%%CSS%%</style></head>
<body data-asset="%%ID%%" data-rev="%%REV%%"><div class="wrap">
<a class="back" href="../index.html">&larr; all assets</a>
<h1>%%ID%%</h1><div class="sub">%%TITLE%%</div>
<div class="stages" style="margin:10px 0">%%STAGES%%</div>
%%PENDING%%
<div class="kv">last updated <b>%%MTIME%%</b> &middot; <span id="notesCount">0</span> review notes on this asset</div>
%%OUTSTANDING%%
%%VIEWER%%
<h2 id="review">Review &mdash; render vs schematic</h2>
<div class="reviewbar">
<div class="toggle on" id="reviewToggle"><div class="pill"></div><span>Review mode</span></div>
<span class="hint" id="reviewHint">Review mode ON &mdash; right-click (or long-press) either image to drop a pin and leave a note.</span>
</div>
<div class="viewtabs">%%TABS%%</div>
<div class="compare">
<div class="imgwrap" id="wrap-render"><img alt="3D render"><div class="imglbl">3D render &middot; %%REV%%</div>
<div class="nonimg" style="display:none;min-height:200px;align-items:center;justify-content:center;color:var(--muted)">no render for this view yet</div></div>
<div class="imgwrap" id="wrap-schematic"><img alt="schematic"><div class="imglbl">schematic</div>
<div class="nonimg" style="display:none;min-height:200px;align-items:center;justify-content:center;color:var(--muted)">no schematic for this view</div></div>
</div>
<h2>Notes on this asset</h2>
<div class="notes" id="notesList"></div>
</div>
<script>var VIEWS_DATA=%%VIEWS_JSON%%;</script>
<script>%%REVIEW_JS%%</script>
</body></html>"""


def _fb_item(d):
    """One outstanding-feedback <li>: expandable <details> so Felix can tap
    to read the full actionable finding, not just the criterion name."""
    src = d.get('src', 'felix')
    chip = ('<span class="srcchip {}">{}</span>'.format(
        src, 'Felix' if src == 'felix' else 'Judge'))
    rev = (f' <span class="orev">{esc(d["rev"])}</span>'
           if d.get('rev') else '')
    full = (d.get('full') or '').strip()
    name = (d.get('name') or '').strip()
    if full and name and full.lower().startswith(name.lower()):
        full = full[len(name):].lstrip(' —-–:').strip()
    detail = (f'<div class="fbdetail">{esc(full)}</div>' if full else '')
    return (f'<li><details class="fb"><summary>{chip}<b>{esc(d["name"])}'
            f'</b>{rev}</summary>{detail}</details></li>')


def render_entry(aid, sch, mod):
    stages = ''.join(f'<span class="stage {cls}">{esc(lbl)}</span>'
                     for lbl, cls in entry_stages(sch, mod))
    title = sch['title'] if sch and sch.get('title') else aid
    rev = mod['latest_rev'] if mod and mod['latest_rev'] else '—'
    # viewer
    viewer = ''
    if mod and mod['latest_glb']:
        g = mod['latest_glb']
        mesh = ''
        if mod['mesh']:
            m = mod['mesh']
            mesh = ('<table class="stats"><tr><td>objects</td><td>{}</td></tr>'
                    '<tr><td>triangles</td><td>{}</td></tr>'
                    '<tr><td>non-manifold</td><td>{}</td></tr></table>').format(
                m.get('objects', '—'), m.get('tris', '—'), m.get('nonmanifold_edges', '—'))
        # Label the GLB with the rev it actually belongs to (from model-meta).
        # If the featured renders are a NEWER rev than the exported GLB, say so
        # explicitly — the viewer would otherwise silently show older geometry.
        glb_rev = mod.get('glb_rev')
        glb_lbl = f' (rev {esc(glb_rev)})' if glb_rev else ''
        mismatch = ''
        if glb_rev and rev != '—' and glb_rev != rev:
            mismatch = (f'<div class="kv" style="margin-top:6px">Renders above '
                        f'are <b>{esc(rev)}</b>; the 3D viewer loads the last '
                        f'exported GLB (<b>{esc(glb_rev)}</b>).</div>')
        viewer = (f'<h2>3D viewer &mdash; {esc(g["file"])}{glb_lbl}</h2>{mesh}'
                  f'{mismatch}'
                  f'<div id="viewer"></div><div class="kv" id="vstat">loading…</div>'
                  f'<script>const GLB_URL="../assets/models/{aid}/{esc(g["file"])}";</script>'
                  + VIEWER_JS)
    # per-view compare data (relative to entries/ dir)
    views_data = {}
    for v in VIEWS:
        r = s = None
        if mod and mod['latest_rev']:
            rp = f'assets/models/{aid}/renders/{mod["latest_rev"]}/{v}.png'
            if os.path.exists(os.path.join(OUT, rp)):
                r = '../' + rp
        if sch and v in sch['views']:
            sp = f'assets/schematics/{aid}/{v}.jpg'
            if os.path.exists(os.path.join(OUT, sp)):
                s = '../' + sp
        views_data[v] = {'render': r, 'schematic': s}
    tabs = ''.join(
        f'<button class="viewtab" data-view="{v}">{VIEW_LABELS[v]}</button>' for v in VIEWS)
    mtime = max(sch['mtime'] if sch else 0, mod['mtime'] if mod else 0)
    # outstanding Felix feedback (unresolved flagged defects from QA logs)
    # + recently resolved (auto-resolved by targeted judges — Felix audits async)
    # The outstanding box ALWAYS renders: an empty state ("None — all
    # resolved") is information, a missing box is ambiguity. (Felix 2026-09-13:
    # "if it's still in the fixing state, I should be able to see the
    # outstanding ones.")
    items = (mod['outstanding'] if mod and mod.get('outstanding') else [])
    if items:
        lis = ''.join(_fb_item(d) for d in items)
        outstanding = (
            '<div class="outstanding"><div class="otitle">Outstanding feedback'
            f' ({len(items)})</div><ul>{lis}</ul>'
            '<div class="ohint">Flagged, not yet resolved &mdash; every '
            'judging pass re-checks each one; a targeted PASS auto-resolves it. '
            'No waiting on Felix.</div></div>')
    else:
        outstanding = (
            '<div class="outstanding clear"><div class="otitle">Outstanding '
            'feedback (0)</div>'
            '<div class="oclear">None &mdash; every flagged defect is '
            'resolved. If the model is still in progress, it is awaiting '
            'judge verdicts, not fixes.</div></div>')
    resolved = ''
    ritems = (mod['recently_resolved'] if mod and mod.get('recently_resolved')
              else [])
    if ritems:
        rlis = ''.join(
            '<li><span class="srcchip {}">{}</span>{}{}</li>'.format(
                d.get('src', 'felix'),
                'Felix' if d.get('src', 'felix') == 'felix' else 'Judge',
                esc(d['name']),
                f' <span class="orev">fixed in {esc(d["rev"])}</span>'
                if d['rev'] else '')
            for d in ritems[:6])
        more = (f'<div class="ohint">+{len(ritems) - 6} more</div>'
                if len(ritems) > 6 else '')
        resolved = (
            '<div class="resolvedbox"><div class="rtitle">Recently resolved '
            f'({len(ritems)})</div><ul>{rlis}</ul>{more}'
            '<div class="ohint">Auto-resolved by targeted judges; '
            're-flag anytime from review.</div></div>')
    html = ENTRY_TMPL
    html = html.replace('%%CSS%%', CSS)
    html = html.replace('%%ID%%', esc(aid))
    html = html.replace('%%TITLE%%', esc(title[:80]))
    html = html.replace('%%REV%%', esc(rev))
    html = html.replace('%%STAGES%%', stages)
    # Pending-revision banner: a newer rev exists but its render set is incomplete,
    # so the page shows the latest finished rev. Say so instead of mixing revisions.
    pending_html = ''
    if mod and mod.get('pending_rev'):
        pr = next((r for r in mod.get('revs', []) if r['rev'] == mod['pending_rev']), None)
        n = len(pr['renders']) if pr else 0
        state = 'awaiting judges' if n >= len(VIEWS) else 'renders %d/%d' % (n, len(VIEWS))
        pending_html = (f'<div class="kv" style="border:1px solid #6a5a2a;background:#2a2415;'
                        f'border-radius:8px;padding:8px 12px;margin:10px 0">'
                        f'&#9203; <b>{esc(mod["pending_rev"])}</b> in progress ({state}) — '
                        f'page shows the latest finished build.</div>')
    html = html.replace('%%PENDING%%', pending_html)
    html = html.replace('%%MTIME%%', fmt_ts_long(mtime))
    html = html.replace('%%OUTSTANDING%%', outstanding + resolved)
    html = html.replace('%%TABS%%', tabs)
    html = html.replace('%%VIEWS_JSON%%', json.dumps(views_data))
    html = html.replace('%%REVIEW_JS%%', REVIEW_JS)
    # viewer block: insert before review section (VIEWER_JS contains its own
    # <script> tags; ENTRY_TMPL has %%VIEWER%% placeholder)
    html = html.replace('%%VIEWER%%', viewer)
    write_if_changed(os.path.join(OUT, 'entries', aid + '.html'), html)


# ----------------------------------------------------------------- main ---

def main():
    t0 = datetime.now(timezone.utc)
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
    all_ids = sorted(set(schematics) | set(models))
    entries = ''.join(entry_card(aid, schematics.get(aid), models.get(aid))
                      for aid in all_ids)
    index = INDEX_TMPL
    index = index.replace('%%CSS%%', CSS)
    index = index.replace('%%GEN_TIME%%',
                          datetime.now(LA).strftime('%Y-%m-%d %H:%M %Z'))
    index = index.replace('%%PIPES%%', pipes)
    index = index.replace('%%N%%', str(len(all_ids)))
    index = index.replace('%%ENTRIES%%', entries)
    write_if_changed(os.path.join(OUT, 'index.html'), index)
    for aid in all_ids:
        render_entry(aid, schematics.get(aid), models.get(aid))

    meta = {
        'generated_at': t0.isoformat(),
        'generated_at_pacific': t0.astimezone(LA).strftime('%Y-%m-%d %H:%M %Z'),
        'fingerprint': fp,
        'n_schematics': len(schematics),
        'n_models': len(models),
        'assets_copied': copied,
    }
    write_if_changed(meta_path, json.dumps(meta, indent=2))
    write_if_changed(os.path.join(OUT, '.nojekyll'), '')
    dt = (datetime.now(timezone.utc) - t0).total_seconds()
    print(f'done: {len(schematics)} schematics, {len(models)} models, '
          f'{len(all_ids)} entries, {copied} assets copied, {dt:.1f}s')


if __name__ == '__main__':
    main()
