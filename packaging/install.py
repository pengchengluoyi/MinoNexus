"""Mino native installer: downloads GitHub Actions binaries from GitHub Releases."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
import certifi

REPOS = {'nexus':'pengchengluoyi/MinoNexus', 'workbench':'pengchengluoyi/MinoWorkbench'}
PLATFORMS = {('darwin','arm64'), ('darwin','x64'), ('linux','x64'), ('win32','x64')}

def prompt(label, choices):
    print(label, flush=True)
    for key, value in choices.items(): print(f'  {key}. {value[0]}', flush=True)
    if sys.stdin.isatty(): answer = input('> ').strip()
    elif os.name != 'nt':
        try:
            with open('/dev/tty') as tty: answer = tty.readline().strip()
        except OSError: raise SystemExit('Non-interactive session: pass --components')
    else: raise SystemExit('Non-interactive session: pass --components')
    if answer not in choices: raise SystemExit('Invalid selection; no installation performed')
    return choices[answer][1]

def exe(path):
    return Path(str(path)+'.exe') if os.name == 'nt' else Path(path)

def run(command):
    result = subprocess.run(list(map(str,command)))
    if result.returncode: raise RuntimeError(f'{Path(str(command[0])).name} failed ({result.returncode}); existing files preserved')

def download(url, target, maximum):
    request = urllib.request.Request(url, headers={'User-Agent':'MinoInstaller/1'})
    context = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(request, timeout=120, context=context) as response, target.open('xb') as output:
        if not response.url.startswith('https://'): raise ValueError('Non-HTTPS download rejected')
        size = 0
        while True:
            block = response.read(1024*1024)
            if not block: break
            size += len(block)
            if size > maximum: raise ValueError('Download exceeds declared size')
            output.write(block)

def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''): digest.update(block)
    return digest.hexdigest()

def prepare(component, version, local, osn, arch):
    repo = REPOS[component]
    if version and not re.fullmatch(r'v?\d+\.\d+\.\d+', version): raise ValueError('Invalid stable version')
    if local:
        folder = local.expanduser().resolve(strict=True)
    else:
        cache = Path(os.environ.get('LOCALAPPDATA', Path.home()/'.cache'))/'Mino/downloads'
        cache.mkdir(parents=True, exist_ok=True)
        folder = Path(tempfile.mkdtemp(prefix=component+'-', dir=cache))
        suffix = 'download/v'+version.lstrip('v') if version else 'latest/download'
        print(f'Downloading {component} release manifest; cache: {folder}', flush=True)
        download(f'https://github.com/{repo}/releases/{suffix}/manifest.json',folder/'manifest.json',4*1024*1024)
    doc = json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    if doc.get('component') != component or doc.get('manifest_schema_version') != 1: raise ValueError('Manifest component/schema mismatch')
    v = doc.get('version','')
    if not re.fullmatch(r'\d+\.\d+\.\d+',v) or (version and v!=version.lstrip('v')): raise ValueError('Release version mismatch')
    matches = [x for x in doc.get('items',[]) if (x.get('os'),x.get('arch')) == (osn,arch)]
    if len(matches)!=1: raise ValueError(f'Release does not provide exactly one {osn}-{arch} package')
    item = matches[0]
    if any(item.get(k)!=doc.get(k) for k in ('component','version','source_commit','build_id')): raise ValueError('Manifest identity mismatch')
    if item.get('dirty') is not False or item.get('payload')!='binary': raise ValueError('First installation requires a clean complete binary package')
    name = item.get('filename','')
    if not re.fullmatch(r'Mino[A-Za-z0-9._-]+\.zip',name): raise ValueError('Unsafe package name')
    url = f'https://github.com/{repo}/releases/download/v{v}/{name}'
    if item.get('url')!=url: raise ValueError('Package URL must belong to this fixed repository release')
    if type(item.get('bytes')) is not int or not 0<item['bytes']<=4*1024**3: raise ValueError('Invalid package size')
    if not re.fullmatch(r'[0-9a-f]{64}',str(item.get('sha256',''))): raise ValueError('Invalid package SHA256')
    package=folder/name
    if not local:
        print(f'Downloading GitHub-built binary: {name}',flush=True)
        download(url,package,item['bytes'])
    if package.stat().st_size!=item['bytes'] or sha(package)!=item['sha256']: raise ValueError('Package size/SHA256 mismatch')
    with zipfile.ZipFile(package) as archive:
        identity=json.loads(archive.read('release.json'))
        if any(identity.get(k)!=item.get(k) for k in ('component','version','source_commit','build_id','dirty','os','arch')): raise ValueError('ZIP identity mismatch')
        seen=set(); size=0
        for row in archive.infolist():
            path=PurePosixPath(row.filename); size+=row.file_size
            if row.filename in seen or path.is_absolute() or '..' in path.parts or '\\' in row.filename or ':' in row.filename or stat.S_ISLNK(row.external_attr>>16): raise ValueError('Unsafe ZIP member')
            if size>12*1024**3 or len(seen)>200000: raise ValueError('Archive expansion limit exceeded')
            seen.add(row.filename)
    print(f'Verified {component} {v}: {item["build_id"]}',flush=True)
    return folder,package,item

def extract_helper(folder,package):
    target=Path(tempfile.mkdtemp(prefix='launcher-',dir=folder))
    with zipfile.ZipFile(package) as archive:
        for row in archive.infolist():
            if not row.filename.startswith('launcher/') or row.is_dir(): continue
            path=target/row.filename;path.parent.mkdir(parents=True,exist_ok=True)
            with path.open('xb') as stream:stream.write(archive.read(row))
            path.chmod((row.external_attr>>16)&0o777 or 0o644)
    return exe(target/'launcher/mino-maintain')

def install(component,root,prepared,nexus_url):
    folder,package,item=prepared
    port=50104 if component=='nexus' else 60104
    root.parent.mkdir(parents=True,exist_ok=True);root=root.parent.resolve()/root.name
    helper=extract_helper(folder,package)
    if component=='nexus':
        run([helper,'d2','install','--root',root,'--data-dir',root/'data','--host','127.0.0.1','--port',port,'--manifest',folder/'manifest.json','--package',package])
        helper=exe(root/'launcher/mino-maintain')
        run([helper,'d2','provision-probe','--root',root]);run([helper,'d2','register','--root',root]);run([helper,'d2','probe','--root',root])
    else:
        run([helper,'install','--root',root,'--host','127.0.0.1','--port',port,'--nexus-url',nexus_url,'--label','local.mino.workbench','--package',package,'--sha256',item['sha256']])
        helper=exe(root/'launcher/mino-maintain')
        run([helper,'register','--root',root,'--service-file',root/'config/service.plist']);run([helper,'start','--root',root])
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    route='/sys/server_info' if component=='nexus' else '/health'
    for attempt in range(60):
        try:
            with opener.open(f'http://127.0.0.1:{port}{route}',timeout=2) as response: info=json.load(response)
            info=info.get('data',info)
            if info.get('build_id')!=item['build_id']: raise ValueError('Running version does not match downloaded package')
            break
        except OSError:
            if attempt==59: raise RuntimeError('Service did not become healthy; preserve installation directory for diagnosis')
            time.sleep(1)
    if component=='workbench':
        sibling=root.parent/'nexus';cfg=sibling/'config/mino-deployment.json';nh=exe(sibling/'launcher/mino-maintain')
        if cfg.is_file() and nh.is_file() and json.loads(cfg.read_text()).get('probe_url')==nexus_url.rstrip('/'):
            run([nh,'d2','bind-workbench','--root',sibling,'--workbench-root',root])
    print(f'Installed {component}: {root}\nURL: http://127.0.0.1:{port}\nMaintenance: {helper}',flush=True)

def main():
    ap=argparse.ArgumentParser(description='Mino installer — GitHub Actions binaries from GitHub Releases')
    ap.add_argument('--components',choices=['nexus','workbench','both'])
    ap.add_argument('--base-dir',type=Path)
    ap.add_argument('--root',type=Path,help='Single-component installation root')
    ap.add_argument('--version',help='Single-component fixed release version')
    ap.add_argument('--nexus-version');ap.add_argument('--workbench-version')
    ap.add_argument('--nexus-url')
    ap.add_argument('--package-dir',type=Path,help='Offline manifest/ZIP directory; both uses nexus/ and workbench/ subdirectories')
    ap.add_argument('--download-only',action='store_true')
    args=ap.parse_args()
    osn=sys.platform;arch={'arm64':'arm64','aarch64':'arm64','amd64':'x64','x86_64':'x64'}.get(platform.machine().lower(),'unsupported')
    if (osn,arch) not in PLATFORMS:ap.error(f'Unsupported platform: {osn}-{arch}')
    selected=args.components or prompt('Select components / 选择安装组件',{'1':('Nexus','nexus'),'2':('Workbench','workbench'),'3':('Nexus + Workbench','both')})
    if selected=='both' and (args.root or args.version):ap.error('Use --base-dir and component-specific version flags for both components')
    components=['nexus','workbench'] if selected=='both' else [selected]
    base=(args.base_dir or Path.home()/'Mino').expanduser().absolute()
    roots={c:(args.root.expanduser().absolute() if args.root else base/c) for c in components}
    for c,root in roots.items():
        if not args.download_only:
            if root.exists() or root.is_symlink():ap.error(f'Existing installation preserved: {root}; use maintenance commands, do not reinstall')
            port=50104 if c=='nexus' else 60104
            with socket.socket() as listener:
                try:listener.bind(('127.0.0.1',port))
                except OSError:ap.error(f'Port {port} is occupied; existing service will not be stopped or moved')
    # Prepare ALL requested packages before changing an installation.
    packages={}
    for c in components:
        version=args.version or getattr(args,c+'_version')
        local=(args.package_dir/c if selected=='both' else args.package_dir) if args.package_dir else None
        packages[c]=prepare(c,version,local,osn,arch)
    if args.download_only:
        for c,(folder,_,_) in packages.items():print(f'{c} download: {folder}')
        return 0
    nexus_url=args.nexus_url or 'http://127.0.0.1:50104'
    for c in components:install(c,roots[c],packages[c],nexus_url)
    print('Production installation complete.')
    return 0

if __name__=='__main__':
    try:sys.exit(main())
    except (Exception,KeyboardInterrupt) as exc:
        print(f'Installation stopped: {exc}\nDownloaded files and any installed component are preserved.',file=sys.stderr)
        sys.exit(1)
