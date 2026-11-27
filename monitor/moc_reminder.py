"""Read-only midday MOC preview using the identical verified post-close engine."""
from pathlib import Path
import hashlib,sys
EXPECTED_MONITOR_SHA256 = '5f0f90fe0b04579c08690e62f1d715451e0521dcc2f06e41a134f267acdc1a74'
path=Path(__file__).with_name('monitor.py')
if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest()!=EXPECTED_MONITOR_SHA256:
    print('DATA NOT READY: monitor.py integrity mismatch; no trade instruction.');sys.exit(2)
from monitor import main
if __name__=='__main__':
    try:sys.exit(main('moc'))
    except Exception as exc:print('DATA NOT READY: '+str(exc)+'; no trade instruction.');sys.exit(2)
