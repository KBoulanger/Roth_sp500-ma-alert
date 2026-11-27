"""Read-only midday MOC preview using the identical verified post-close engine."""
from pathlib import Path
import hashlib,sys
EXPECTED_MONITOR_SHA256 = '8404edd80bbed61312e1917bcb5e2df0dee48fff9d6c07612371842f092e954d'
path=Path(__file__).with_name('monitor.py')
if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest()!=EXPECTED_MONITOR_SHA256:
    print('DATA NOT READY: monitor.py integrity mismatch; no trade instruction.');sys.exit(2)
from monitor import main
if __name__=='__main__':
    try:sys.exit(main('moc'))
    except Exception as exc:print('DATA NOT READY: '+str(exc)+'; no trade instruction.');sys.exit(2)
