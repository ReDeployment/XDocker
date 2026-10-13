#!/usr/bin/env python3
"""Prepare a GitHub-only candidate from HEAD plus selected local edits; no git writes or Docker."""
import argparse
import ast
import difflib
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
COPY = [
    '.env.example', '.github/workflows/publish-admin.yml', '.github/workflows/publish-github-webhook.yml',
    'certbot/certificates.json', 'readme.md', 'scripts/admin.sh', 'scripts/hosting.sh', 'scripts/github-watch.py',
    'scripts/github-webhook.sh', 'services/admin/compose.yml', 'services/admin/.env.example',
    'services/admin/github_watch.py', 'services/admin/github_webhooks.py', 'services/admin/static/github.js',
    'services/admin/static/index.html', 'tests/admin_http_smoke.py', 'tests/test_hosting.py',
    'tests/test_certificates.py', 'tests/test_github_watch.py', 'tests/test_github_webhook.py',
    'tests/github_webhook_smoke.py', 'docs/guides/admin/github-watch.md', 'docs/guides/admin/github-webhook.md',
    'docs/guides/admin/preflight.md', 'docs/guides/certificates/README.md',
    'nginx/webhook-http.conf.template', 'nginx/webhook-https.conf.template',
    'sites/github-webhook/.env.example', 'sites/github-webhook/site.conf',
]


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT)


def section(text, start, end):
    if text.count(start) != 1 or text.count(end) != 1:
        raise ValueError('Source boundary changed: ' + start)
    return text[text.index(start):text.index(end)]


def line(text, prefix):
    matches = [s for s in text.splitlines(keepends=True) if s.startswith(prefix)]
    if len(matches) != 1: raise ValueError('Source line changed: ' + prefix)
    return matches[0]


def replace_once(text, old, new):
    if text.count(old) != 1: raise ValueError('Base boundary changed: ' + old[:100])
    return text.replace(old, new, 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='data/release-previews/github-watch-20261013')
    args = parser.parse_args()
    out = ROOT / args.output
    if out.is_absolute() and not out.resolve().is_relative_to(ROOT / 'data/release-previews'):
        raise SystemExit('Output must be inside ignored data/release-previews.')
    if any(p.is_symlink() for p in (out, *out.parents)) or out.exists():
        raise SystemExit('Refusing symlink or existing output; choose a new preview directory.')
    base = git('rev-parse', 'HEAD').decode().strip()
    files = {}
    archive = git('archive', '--format=tar', base)
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        for member in bundle:
            if not member.isfile():
                if member.isdir(): continue
                raise ValueError('Baseline contains a non-regular file')
            path = Path(member.name)
            if path.name == '.env' or path.parts[0] == 'data' or path.suffix in ('.key', '.pem', '.sqlite3'):
                raise ValueError('Private state unexpectedly tracked in baseline')
            if path.is_absolute() or '..' in path.parts: raise ValueError('Unsafe archive path')
            files[path.as_posix()] = bundle.extractfile(member).read()
    original = dict(files)
    if b'from github_watch import' in files['services/admin/app.py']:
        raise ValueError('Baseline already contains this GitHub feature; use a new release plan')
    for folder in ('services/github-webhook',):
        COPY_PATHS = [p.relative_to(ROOT).as_posix() for p in (ROOT/folder).iterdir() if p.is_file() and p.name != '.env']
        # No caches, .env, data, credentials, generated state or private outputs.
        expected = {'Dockerfile', '.dockerignore', '.env.example', 'compose.yml', 'requirements.txt', 'receiver.py', 'projects.json'}
        COPY_PATHS = [p for p in COPY_PATHS if Path(p).name in expected]
        if {Path(p).name for p in COPY_PATHS} != expected: raise ValueError('Receiver source incomplete')
        for name in COPY_PATHS: files[name] = (ROOT/name).read_bytes()
    for name in COPY:
        if (ROOT/name).is_symlink(): raise ValueError('Refusing symlink source')
        files[name] = (ROOT/name).read_bytes()

    def read(name): return files[name].decode()
    def write(name, text): files[name] = text.encode()
    def current(name): return (ROOT/name).read_text()

    name = 'services/admin/app.py'; src = current(name); dst = read(name)
    dst = replace_once(dst, 'from image_versions import ImageVersions, VersionCheckBusy\n',
                       'from image_versions import ImageVersions, VersionCheckBusy\nfrom github_watch import GitHubWatch, WatchError\nfrom github_webhooks import WebhookInbox\n')
    config = section(src, '                      GITHUB_WATCH_ENABLED=', "                      TRUSTED_HOSTS=")
    dst = replace_once(dst, "                      TRUSTED_HOSTS=", config + "                      TRUSTED_HOSTS=")
    init = section(src, '    try:watch_interval=', '    def db():')
    dst = replace_once(dst, '    def db():', init + '    def db():')
    routes = section(src, "    @app.get('/api/github')", '    def backup_instances():')
    dst = replace_once(dst, '    def backup_instances():', routes + '    def backup_instances():')
    dst = replace_once(dst, '    return app\n', "    if watch.enabled and not app.config.get('TESTING'):watch.start()\n    if hooks.enabled and not app.config.get('TESTING'):hooks.start()\n    return app\n")
    helper=section(src,'def webhook_probe(', 'def create_app(')
    dst=replace_once(dst,'def create_app(',helper+'def create_app(')
    probe=section(src,"    @app.post('/api/sites/probe')", "    @app.get('/api/jobs')")
    dst=replace_once(dst,section(dst,"    @app.post('/api/sites/probe')", "    @app.get('/api/jobs')"),probe)
    ast.parse(dst); write(name, dst)

    name = 'services/admin/static/app.js'; src = current(name); dst = read(name)
    dst = replace_once(dst, "'image-versions':'镜像版本'};", "'image-versions':'镜像版本', github:'研发事件'};")
    dst = replace_once(dst, "const instanceModules=", line(src, 'const githubData=') + line(src, 'let githubEpoch=') + 'const instanceModules=')
    dst = replace_once(dst, "  const planControl=path.startsWith('backup-plans/')&&planWritable();\n",
                       "  const planControl=path.startsWith('backup-plans/')&&planWritable();\n  const githubControl=path==='github/action'&&githubWritable();\n")
    dst = replace_once(dst, '&&!planControl)throw', '&&!planControl&&!githubControl)throw')
    for prefix in ('function showLogin()', 'async function showShell()'):
        replacement = line(src, prefix).replace('upgradeData.reachable=false;', '').replace("if(current==='upgrades'&&loggedIn)await loadUpgradeTasks();", '')
        dst = replace_once(dst, line(dst, prefix), replacement)
    prefix=line(dst, "  if(current==='preflight'){$('content')")
    dst = replace_once(dst, prefix, "  if(current==='github'){$('content').innerHTML=renderGithub();return;}\n" + prefix)
    click = line(src, "document.addEventListener('click',")
    click = re.sub(r',?\[data-upgrade-[^]]+\]', '', click)
    click = re.sub(r'&&!d\.upgrade[A-Za-z]+', '', click)
    dst = replace_once(dst, line(dst, "document.addEventListener('click',"), click)
    tab = line(src, '  if(d.tab)').replace("if(current==='upgrades')await loadUpgradeTasks();", '')
    github_actions = section(src, '  else if(d.githubRefresh)', '  else if(d.upgradeRefresh)')
    dst = replace_once(dst, line(dst, '  if(d.tab)'), tab + github_actions)
    for prefix in ("window.addEventListener('hashchange',", 'setInterval('):
        replacement = line(src, prefix).replace("if(current==='upgrades'&&loggedIn)await loadUpgradeTasks();", '').replace("if(current==='upgrades')await loadUpgradeTasks();", '')
        replacement = replacement.replace("if(current==='upgrades')loadUpgradeTasks();", "").replace("if(current==='upgrades'){render();if(loggedIn)loadUpgradeTasks();return;}", "")
        dst = replace_once(dst, line(dst, prefix), replacement)
    if re.search(r'upgradeData|loadUpgradeTasks|data-upgrade-|/api/upgrades', dst): raise ValueError('Upgrade UI leaked into candidate')
    dst=dst.replace("s.kind==='frp_http'?'FRP 穿透':s.kind==='app'?'动态应用':'静态站点'", "s.kind==='github_webhook'?'Webhook 接收':s.kind==='frp_http'?'FRP 穿透':s.kind==='app'?'动态应用':'静态站点'")
    write(name, dst)
    write('services/admin/static/index.html', re.sub(r'<button data-tab="upgrades">.*?</button>', '', read('services/admin/static/index.html')))
    write('tests/admin_http_smoke.py', '\n'.join(s for s in read('tests/admin_http_smoke.py').replace(",'/api/upgrades'", '').splitlines() if not s.strip().startswith(('upgrade=json.', "assert request('/api/upgrades/"))) + '\n')

    name='tests/admin_connection_ui.cjs';src=current(name);dst=read(name)
    states=section(src,'  state.githubProjects=','  const configured=')
    dst=replace_once(dst,'  const configured=',states+'  const configured=')
    routes=section(src,"      if(path==='github/summary')", "      if(path==='upgrades')")
    dst=replace_once(dst,"      if(path==='image-versions')",routes+"      if(path==='image-versions')")
    extra=line(src,'  vm.runInContext(fs.readFileSync(')
    dst=replace_once(dst,'  vm.runInContext(source,context);\n','  vm.runInContext(source,context);\n'+extra)
    checks=section(src,'  const z=fixture();',"  console.log('Readonly preview UI passed:")
    dst=replace_once(dst,"  console.log('Admin UI connection checks passed:", checks+"  console.log('Admin UI connection checks passed:")
    write(name,dst)

    name='services/admin/Dockerfile';write(name, read(name).replace('image_versions.py ./','image_versions.py github_watch.py github_webhooks.py ./'))
    name='services/admin/.dockerignore';write(name,read(name)+'\n!github_watch.py\n!github_webhooks.py\n!static/github.js\n')
    name='tests/admin_build_context_smoke.py';src=current(name)
    src=re.sub(r", 'upgrade_tasks.py', 'upgrade_contract.py', 'upgrade_preview.py'",'',src)
    src=re.sub(r"\n          'contracts/powerx-upgrade-manifest.schema.json', 'contracts/powerx-upgrade-result.schema.json',",'',src)
    write(name,src)
    name='docs/guides/admin/README.md';dst=read(name)
    dst=dst.replace("'image_versions.py',", "'image_versions.py', 'github_watch.py', 'github_webhooks.py',").replace("'static/app.js',", "'static/app.js', 'static/github.js',")
    dst+='\n“研发事件”覆盖六项目的 PR、CI 与评论，自动采集和 Webhook 导入默认关闭。见 [GitHub 观察](github-watch.md) 与 [Webhook 接收配置](github-webhook.md)。\n'
    write(name,dst)
    name='docs/tasks/README.md';write(name,read(name)+'\nGitHub 观察/Webhook 独立发布准备见 [发布审查](github-webhook-release-20261013.md)。\n')
    name='docs/tasks/github-webhook-release-20261013.md';files[name]=(ROOT/name).read_bytes()
    files['scripts/prepare-github-release.py']=Path(__file__).read_bytes()

    changed = sorted(name for name,content in files.items() if original.get(name) != content)
    if any('upgrade_' in name or name.startswith('services/admin/contracts/') for name in changed):
        raise ValueError('Upgrade file leaked into candidate')
    setting = next(s for s in files['.env.example'].decode().splitlines() if s.startswith('PREFLIGHT_EXPECTED_IPS='))
    if setting.split('=',1)[1].strip() not in ('\"\"', "''", ''):
        raise ValueError('Expected server addresses must remain empty in the public template')
    out.mkdir(parents=True, mode=0o700)
    tree=out/'tree';tree.mkdir(mode=0o700)
    for name,content in files.items():
        target=tree/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(content)
    patch=[]
    for name in changed:
        old=original.get(name,b'').decode();new=files[name].decode()
        patch.append('diff --git a/'+name+' b/'+name+'\n')
        if name not in original:patch.append('new file mode 100644\n')
        patch.extend(difflib.unified_diff(old.splitlines(keepends=True),new.splitlines(keepends=True),
            fromfile='a/'+name if name in original else '/dev/null',tofile='b/'+name))
    (out/'candidate.patch').write_text(''.join(patch))
    manifest={'base_revision':base,'scope':'github-watch-and-webhook-only','files':{n:hashlib.sha256(files[n]).hexdigest() for n in changed},
              'patch_sha256':hashlib.sha256((out/'candidate.patch').read_bytes()).hexdigest(),'validation':'pending',
              'git_writes':False,'docker_used':False,'published':False,'deployed':False}
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print(json.dumps({'candidate':str(out.relative_to(ROOT)),'base_revision':base,'changed_files':len(changed),'git_writes':False}))


if __name__=='__main__':main()
