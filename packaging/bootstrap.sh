#!/usr/bin/env bash
# Mino GitHub Release bootstrap; no local compilation or GitHub CLI required.
set -euo pipefail
command -v python3 >/dev/null || { echo '需要 python3 解析和校验发布清单。' >&2; exit 1; }
command -v curl >/dev/null || { echo '需要 curl。' >&2; exit 1; }
python3 - nexus "$@" <<'MINO_BOOTSTRAP_PY'
import argparse, hashlib, json, os, pathlib, platform, re, socket, stat, subprocess, sys, tempfile, time, urllib.request, zipfile

component = sys.argv.pop(1)
repo = 'pengchengluoyi/Mino' + component.capitalize()
ap = argparse.ArgumentParser(description='从 GitHub Release 安装 Mino' + component.capitalize())
ap.add_argument('--environment', choices=['test', 'production'], default='production')
ap.add_argument('--version', help='固定版本，例如 0.1.37；默认最新稳定 Release')
ap.add_argument('--root', type=pathlib.Path, help='新的安装目录；已有目录不会覆盖')
ap.add_argument('--nexus-url', help='Workbench 连接的 Nexus；默认本机对应环境')
ap.add_argument('--package-dir', type=pathlib.Path, help='离线安装：包含 manifest.json 和 ZIP 的目录')
ap.add_argument('--download-only', action='store_true', help='只下载并校验，不安装、不启动服务')
args = ap.parse_args()
if sys.platform != 'darwin' or platform.machine() != 'arm64':
    ap.error('当前安装包仅支持 Apple Silicon macOS')
if args.version and not re.fullmatch(r'v?\d+\.\d+\.\d+', args.version):
    ap.error('版本必须是稳定版本号')
port = (50101 if args.environment == 'test' else 50104) if component == 'nexus' else (60101 if args.environment == 'test' else 60104)
nexus_port = 50101 if args.environment == 'test' else 50104
root = (args.root or pathlib.Path.home()/'Mino'/args.environment/component).expanduser().absolute()
if root.exists() or root.is_symlink():
    ap.error(f'已有安装或未完成安装：{root}；原数据保留，请通过维护入口检查，不重复安装')
if not args.download_only:
    with socket.socket() as s:
        try: s.bind(('127.0.0.1', port))
        except OSError: ap.error(f'端口 {port} 已占用；不会停止现有服务或自动更换端口')

def run(command):
    subprocess.run(list(map(str, command)), check=True)

def download(url, target):
    run(['curl', '--fail', '--location', '--show-error', '--proto', '=https', '--proto-redir', '=https',
         '--connect-timeout', '30', '--output', target, url])

if args.package_dir:
    folder = args.package_dir.expanduser().resolve(strict=True)
else:
    cache = pathlib.Path.home()/'Library/Caches/Mino'/component
    cache.mkdir(parents=True, exist_ok=True)
    folder = pathlib.Path(tempfile.mkdtemp(prefix='install-', dir=cache))
    suffix = 'download/v' + args.version.lstrip('v') if args.version else 'latest/download'
    print(f'从 GitHub Release 下载，文件保留在 {folder}', flush=True)
    download(f'https://github.com/{repo}/releases/{suffix}/manifest.json', folder/'manifest.json')
manifest = json.loads((folder/'manifest.json').read_text())
version = manifest.get('version', '')
if manifest.get('component') != component or manifest.get('manifest_schema_version') != 1 or not re.fullmatch(r'\d+\.\d+\.\d+', version):
    raise SystemExit('发布清单的组件、版本或格式无效')
if args.version and version != args.version.lstrip('v'):
    raise SystemExit('发布清单与指定版本不一致')
items = [i for i in manifest.get('items', []) if (i.get('os'), i.get('arch')) == ('darwin', 'arm64')]
if len(items) != 1: raise SystemExit('清单中没有唯一匹配的 macOS arm64 安装包')
item = items[0]
if any(item.get(k) != manifest.get(k) for k in ('component','version','build_id','source_commit')):
    raise SystemExit('安装包身份与清单不一致')
if item.get('dirty') is not False or item.get('payload') != 'binary':
    raise SystemExit('首次安装必须使用干净的完整二进制发布包')
name = item.get('filename', '')
if not re.fullmatch(r'Mino[A-Za-z0-9._-]+\.zip', name): raise SystemExit('无效的安装包文件名')
url = f'https://github.com/{repo}/releases/download/v{version}/{name}'
if item.get('url') != url: raise SystemExit('安装包 URL 不属于此仓库的固定版本')
if not re.fullmatch(r'[0-9a-f]{64}', str(item.get('sha256',''))): raise SystemExit('无效的 SHA256')
package = folder/name
if not args.package_dir: download(url, package)
if package.stat().st_size != item.get('bytes'): raise SystemExit('安装包大小校验失败')
with package.open('rb') as f:
    digest = hashlib.sha256()
    for block in iter(lambda:f.read(1024*1024), b''): digest.update(block)
if digest.hexdigest() != item['sha256']: raise SystemExit('安装包 SHA256 校验失败')
with zipfile.ZipFile(package) as z:
    identity = json.loads(z.read('release.json'))
    if any(identity.get(k) != item.get(k) for k in ('component','version','build_id','source_commit','dirty','os','arch')):
        raise SystemExit('ZIP 内身份与发布清单不一致')
    for row in z.infolist():
        p = pathlib.PurePosixPath(row.filename)
        if p.is_absolute() or '..' in p.parts or '\\' in row.filename or stat.S_ISLNK(row.external_attr >> 16):
            raise SystemExit('ZIP 包含不安全路径')
print(f'已校验 Mino{component.capitalize()} {version} ({item["build_id"]})', flush=True)
if args.download_only:
    print(f'下载完成：{folder}'); sys.exit(0)
# Extraction keeps the executable modes carried by the GitHub package.
extract = pathlib.Path(tempfile.mkdtemp(prefix='launcher-', dir=folder))
run(['unzip', '-q', package, 'launcher/*', '-d', extract])
helper = extract/'launcher/mino-maintain'
root.parent.mkdir(parents=True, exist_ok=True)
root = root.parent.resolve()/root.name
if component == 'nexus':
    run([helper, 'd2', 'install', '--root', root, '--data-dir', root/'data', '--environment', args.environment,
         '--host', '127.0.0.1', '--port', port, '--manifest', folder/'manifest.json', '--package', package])
    helper = root/'launcher/mino-maintain'
    run([helper, 'd2', 'provision-probe', '--root', root])
    run([helper, 'd2', 'register', '--root', root])
    run([helper, 'd2', 'probe', '--root', root])
else:
    run([helper, 'install', '--root', root, '--environment', args.environment,
         '--host', '127.0.0.1', '--port', port, '--nexus-url', args.nexus_url or f'http://127.0.0.1:{nexus_port}',
         '--label', 'local.mino.workbench.' + args.environment, '--package', package, '--sha256', item['sha256']])
    helper = root/'launcher/mino-maintain'
    run([helper, 'register', '--root', root, '--service-file', root/'config/service.plist'])
    run([helper, 'start', '--root', root])
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
route = '/sys/server_info' if component == 'nexus' else '/health'
for attempt in range(60):
    try:
        with opener.open(f'http://127.0.0.1:{port}{route}', timeout=2) as response: info = json.load(response)
        info = info.get('data', info)
        if info.get('build_id') != item['build_id']: raise SystemExit('实际运行版本与安装包不一致')
        break
    except OSError:
        if attempt == 59: raise SystemExit('服务未就绪；请保留安装目录检查，不要重复安装')
        time.sleep(1)
print(f'安装完成：{root}\n地址：http://127.0.0.1:{port}\n维护入口：{helper}')

MINO_BOOTSTRAP_PY
