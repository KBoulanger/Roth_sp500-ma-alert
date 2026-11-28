"""Read-only midday MOC preview using the identical verified post-close engine."""
from pathlib import Path
import hashlib,sys
EXPECTED_MONITOR_SHA256 = '359ca0e170c1d261cad3207fc09c0d5565859b281c65c00aea828180fb52829f'
path=Path(__file__).with_name('monitor.py')
if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest()!=EXPECTED_MONITOR_SHA256:
    print('DATA NOT READY: monitor.py integrity mismatch; no trade instruction.');sys.exit(2)
from monitor import main
if __name__=='__main__':
    try:sys.exit(main('moc'))
    except Exception as exc:print('DATA NOT READY: '+str(exc)+'; no trade instruction.');sys.exit(2)
