"""Register user-scoped service commands without replacing unrelated commands."""
import os
from pathlib import Path
import shlex
import sys


def command_dir():
    if os.name == 'nt':
        return Path(os.environ.get('LOCALAPPDATA', Path.home()/'AppData/Local'))/'Mino/bin'
    return Path.home()/'.local/bin'


def command_file(folder, component):
    return folder/('mino-'+component+('.cmd' if os.name == 'nt' else ''))


def check(folder, components):
    folder = folder.expanduser().absolute()
    if folder.resolve() != folder: raise ValueError('Command directory ancestors must not be symbolic links')
    if folder.exists() and not folder.is_dir(): raise ValueError('Command directory is not a directory')
    if folder.is_symlink(): raise ValueError('Command directory must not be a symbolic link')
    for component in components:
        path = command_file(folder, component)
        if path.exists() or path.is_symlink():
            raise ValueError(f'Existing command preserved: {path}; choose --command-dir or inspect the previous installation')


def register(folder, component, root):
    folder=folder.expanduser().absolute();folder.mkdir(parents=True,exist_ok=True)
    helper=root/'launcher'/('mino-maintain.exe' if os.name=='nt' else 'mino-maintain')
    prefix='d2 ' if component=='nexus' else ''
    status='service-status' if component=='nexus' else 'status'
    name='mino-'+component
    if os.name=='nt':
        # Percent expansion must not interpret characters from installation paths.
        helper=str(helper).replace('%','%%');install_root=str(root).replace('%','%%')
        text=f'''@echo off
setlocal
if not "%~2"=="" goto usage
if "%~1"=="" goto start
if "%~1"=="start" goto start
if "%~1"=="stop" goto stop
if "%~1"=="status" goto status
if "%~1"=="--help" goto help
if "%~1"=="help" goto help
goto usage
:start
"{helper}" {prefix}start --root "{install_root}"
exit /b %errorlevel%
:stop
"{helper}" {prefix}stop --root "{install_root}"
exit /b %errorlevel%
:status
"{helper}" {prefix}{status} --root "{install_root}"
exit /b %errorlevel%
:help
echo {name} [start^|stop^|status] - default: start
exit /b 0
:usage
echo Usage: {name} [start^|stop^|status]
exit /b 2
'''
    else:
        text=f'''#!/bin/sh
# Mino installed service command; source development uses its own virtual environment.
if [ "$#" -gt 1 ]; then echo 'Usage: {name} [start|stop|status]' >&2; exit 2; fi
case "${{1:-start}}" in
  start|stop) action="${{1:-start}}" ;;
  status) action={status} ;;
  help|--help|-h) echo '{name} [start|stop|status] - default: start'; exit 0 ;;
  *) echo 'Usage: {name} [start|stop|status]' >&2; exit 2 ;;
esac
exec {shlex.quote(str(helper))} {prefix}"$action" --root {shlex.quote(str(root))}
'''
    target=command_file(folder,component)
    with target.open('x',encoding='utf-8',newline='\n') as out:out.write(text)
    target.chmod(0o755)
    return target


def add_path(folder):
    folder=folder.expanduser().absolute()
    if os.name=='nt':
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,'Environment') as key:
            try: value,kind=winreg.QueryValueEx(key,'Path')
            except FileNotFoundError:value,kind='',winreg.REG_EXPAND_SZ
            if str(folder).casefold() not in [os.path.expandvars(p).rstrip('\\').casefold() for p in value.split(';')]:
                winreg.SetValueEx(key,'Path',0,kind,str(folder)+(';' + value if value else ''))
        import ctypes
        result=ctypes.c_size_t()
        ctypes.windll.user32.SendMessageTimeoutW(0xffff,0x1a,0,ctypes.c_wchar_p('Environment'),2,1000,ctypes.byref(result))
        print('Commands registered in user PATH. Open a NEW terminal to use them.')
        return
    shell=Path(os.environ.get('SHELL','/bin/sh')).name
    if shell=='zsh': profiles=[Path.home()/'.zshrc']
    elif shell=='bash': profiles=[Path.home()/'.bashrc',Path.home()/'.bash_profile']
    elif shell in {'sh','dash','ksh'}:profiles=[Path.home()/'.profile']
    else:
        print(f'Add this command directory to your shell PATH: {folder}')
        return
    quoted=shlex.quote(str(folder))
    block=f'\n# Mino installed service commands\ncase ":$PATH:" in *:{quoted}:*) ;; *) export PATH={quoted}:"$PATH" ;; esac\n'
    for profile in profiles:
        if profile.is_symlink() or (profile.exists() and profile.stat().st_nlink!=1):
            print(f'Shell profile link preserved; add {folder} to PATH manually: {profile}')
            continue
        previous=profile.read_text(encoding='utf-8') if profile.exists() else ''
        if block.strip() not in previous:
            with profile.open('a',encoding='utf-8') as out:out.write(block)
    print(f'Commands: {folder}. Open a NEW terminal, or run: export PATH={quoted}:"$PATH"')
