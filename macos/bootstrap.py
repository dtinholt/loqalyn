"""Bundle entry point, invoked with isolated Python (-I). No setup downloads."""
import os
from pathlib import Path
import sys

resources=Path(__file__).resolve().parent
sys.path[:0]=[str(resources/'app'),str(resources/'python-packages')]
sys.dont_write_bytecode=True
class BlockLegacySQLite:
    def find_spec(self,fullname,path=None,target=None):
        if fullname in ('sqlite3','_sqlite3') or fullname.startswith('sqlite3.'):
            raise ImportError('Loqalyn uses the patched APSW SQLite library.')
sys.meta_path.insert(0,BlockLegacySQLite())
os.environ['LOQALYN_RESOURCES']=str(resources)
os.environ['SSL_CERT_FILE']=str(resources/'python-packages'/'certifi'/'cacert.pem')

try:
    from loqalyn.identity import profile_directory
    os.environ['LOQALYN_DIR']=str(profile_directory(native=True))
    from loqalyn.desktop import main
    main()
except Exception as exc:
    # Avoid persisting arbitrary request data or credentials in crash reports.
    message='Loqalyn could not start ('+type(exc).__name__+'). '+str(exc)[:300]
    print(message,file=sys.stderr)
    if '--cli' not in sys.argv:
        try:
            from AppKit import NSApplication,NSAlert,NSApplicationActivationPolicyRegular
            app=NSApplication.sharedApplication(); app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
            app.activateIgnoringOtherApps_(True)
            alert=NSAlert.alloc().init(); alert.setMessageText_('Loqalyn could not start')
            alert.setInformativeText_(message); alert.addButtonWithTitle_('Quit'); alert.runModal()
        except Exception: pass
    raise SystemExit(1)
