"""Build and verify a clean Windows portable ZIP; never package user state."""
import hashlib,json,os,shutil,subprocess,sys,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
os.chdir(ROOT)
version=json.loads((ROOT/'version.json').read_text(encoding='utf-8'))['version']
release=ROOT/'release';release.mkdir(exist_ok=True)
subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--clean','Muse2API_Studio.spec'],check=True)
portable=ROOT/'dist'/'Muse2API_Studio'
for pattern in ('*.html','*.js','*.txt','*.md','version.json','muse2api-extension.zip'):
    for f in ROOT.glob(pattern):
        if f.is_file():shutil.copy2(f,portable/f.name)
shutil.copytree(ROOT/'muse2api-extension',portable/'muse2api-extension',dirs_exist_ok=True)
# Native runtime must match the Qt wheel used by this build.
import PySide6
qt_root=Path(PySide6.__file__).parent
for pattern in ('msvcp140*.dll','vcruntime140*.dll'):
    for f in qt_root.rglob(pattern):shutil.copy2(f,portable/'_internal'/f.name)
launchers={
 '05_MO_GUI.bat':'start "" "%~dp0Muse2API_Studio.exe" --gui',
 '02_CHAY_MUSE2API.bat':'"%~dp0Muse2API_Studio.exe" --console\r\npause',
 '03_KIEM_TRA.bat':'"%~dp0Muse2API_Studio.exe" --diagnose\r\nnotepad "%~dp0data\\diagnostics.txt"',
 '04_MO_CAU_HINH.bat':'"%~dp0Muse2API_Studio.exe" --configure\r\nnotepad "%~dp0.env"',
}
for name,body in launchers.items():
    (portable/name).write_bytes(('@echo off\r\nsetlocal\r\ncd /d "%~dp0"\r\nset "PYTHONUTF8=1"\r\n'+body+'\r\n').encode('ascii'))
# Exercise the actual frozen GUI, API child and shutdown on Windows.
exe=portable/'Muse2API_Studio.exe'
try:
    subprocess.run([str(exe),'--self-test'],check=True,timeout=180)
except Exception:
    for name in ('portable_error.log','desktop_service.log'):
        f=portable/'data'/name
        if f.exists():print(f.read_text(encoding='utf-8',errors='replace')[-12000:])
    raise
qa=json.loads((portable/'data'/'portable_self_test.json').read_text(encoding='utf-8'))
assert qa['frozen'] and qa['ports_released'] and qa['admin_served']
qa['version']=version;qa['source_commit']=os.environ.get('GITHUB_SHA','local')
qa['tested']='Frozen Windows GUI/API startup/shutdown plus offline regression tests; no live Muse account'
(release/f'QA_PORTABLE_{version}.json').write_text(json.dumps(qa,ensure_ascii=False,indent=2),encoding='utf-8')
# Self-test created a disposable local API key and state. Remove before packaging.
shutil.rmtree(portable/'data',ignore_errors=True)
(portable/'data').mkdir();(portable/'data'/'.gitkeep').touch()
(portable/'.env').unlink(missing_ok=True)
(portable/'HUONG_DAN_PORTABLE.txt').write_text(
 'Muse2API Studio '+version+' — Windows x64\n'
 'Giai nen TOAN BO ZIP vao thu muc moi. Mo Muse2API_Studio.exe hoac 05_MO_GUI.bat.\n'
 'Khong can cai Python. Can Chrome/Edge va Internet. Giu thu muc _internal ben canh EXE.\n'
 'Nang cap: tat GUI/dich vu cu; sao luu roi chep .env va data tu ban cu sang thu muc moi.\n'
 'Khong chay hai ban cung cong. Reload extension cookie 1.0.2 va nhap lai phien Muse neu can.\n'
 'Da kiem tra khoi dong GUI/API tren Windows; chua kiem tra tao noi dung bang tai khoan Muse that.\n',encoding='utf-8')
archive=release/f'Muse2API_VI_Portable_{version}.zip'
with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
    for f in sorted(portable.rglob('*')):
        if f.is_file():
            rel=f.relative_to(portable)
            assert rel.as_posix()!='.env'
            assert not (rel.parts[0]=='data' and rel.as_posix()!='data/.gitkeep'),rel
            z.write(f,Path(f'Muse2API_VI_Portable_{version}')/rel)
with zipfile.ZipFile(archive) as z:assert z.testzip() is None
for f in (archive,release/f'QA_PORTABLE_{version}.json'):
    digest=hashlib.sha256(f.read_bytes()).hexdigest()
    with (release/'SHA256SUMS.txt').open('a',encoding='ascii') as out:out.write(digest+'  '+f.name+'\n')
print('Built and verified',archive)
