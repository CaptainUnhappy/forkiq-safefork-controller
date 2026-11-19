"""Linux container fault probe: exercise GitStore's real process-group cleanup.

No network or credentials. Run beside the Web worker, not as container PID 1.
--observe is solely for the isolated negative control without Docker --init.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from unittest.mock import patch
from app.gitstore import GitStore, TransientGitRead, RemoteReadUnavailable


def zombies():
    found = []
    for status in Path('/proc').glob('[0-9]*/stat'):
        try:
            # comm can contain spaces; state follows the last closing parenthesis.
            if status.read_text().rsplit(')', 1)[1].split()[0] == 'Z':
                found.append(int(status.parent.name))
        except (FileNotFoundError, ProcessLookupError):
            pass
    return found


def process_state(path):
    try:
        return path.read_text().rsplit(')',1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        # procfs may report ESRCH as the task disappears, even during stat().
        return None


def check(cycles=100, observe=False):
    before = set(zombies())
    assert os.getpid() != 1 and not before, 'Probe requires a fresh isolated container'
    real_popen = subprocess.Popen
    peak_zombies = 0
    with tempfile.TemporaryDirectory(dir='/var/lib/forkiq') as root:
        root = Path(root)
        executable = root/'git'
        ready = root/'ready'
        executable.write_text('''#!/usr/local/bin/python
import os,sys,time
from pathlib import Path
child=os.fork()
if child==0:
    time.sleep(60 if os.environ['SF_PROBE_MODE']=='timeout' else 0.03)
    os._exit(0)
ready=Path(os.environ['SF_PROBE_READY'])
temporary=ready.with_suffix('.tmp')
temporary.write_text(str(child))
temporary.replace(ready)
if os.environ['SF_PROBE_MODE']=='failure':
    sys.stderr.write('connection reset by peer\\n')
    sys.exit(128)
time.sleep(60)
''')
        executable.chmod(0o700)
        with GitStore(root,'fixture/source','fixture/target') as store:
            for index in range(cycles):
                if ready.exists():ready.unlink()
                mode = 'timeout' if index % 2 == 0 else 'failure'
                operation = 'ls-remote' if index % 4 == 0 else 'fetch'
                def launch(*args, **kwargs):
                    proc = real_popen(*args, **kwargs)
                    original = proc.wait
                    def wait(timeout=None):
                        if mode == 'timeout' and timeout is not None:
                            deadline=time.monotonic()+5
                            while not ready.exists() and time.monotonic()<deadline:time.sleep(0.005)
                            assert ready.exists(), 'Fixture did not fork a child'
                            raise subprocess.TimeoutExpired(proc.args, timeout)
                        return original(timeout=timeout)
                    proc.wait=wait
                    return proc
                with patch.dict(os.environ, PATH=str(root)+os.pathsep+os.environ['PATH'],
                                SF_PROBE_MODE=mode,SF_PROBE_READY=str(ready)), \
                        patch('app.gitstore.subprocess.Popen', side_effect=launch):
                    try:
                        store.run(operation,'https://fixture.invalid/source.git',side='source')
                    except (TransientGitRead, RemoteReadUnavailable):
                        pass
                    else:
                        raise AssertionError('Fault did not exercise Git read failure')
                # Check the exact fixture child, including live orphans, not only Zs.
                child=int(ready.read_text())
                child_stat=Path('/proc')/str(child)/'stat'
                deadline=time.monotonic()+2
                while time.monotonic()<deadline:
                    state=process_state(child_stat)
                    if state is None or (observe and state=='Z'):break
                    time.sleep(0.01)
                if observe:
                    assert process_state(child_stat)=='Z', 'Negative control did not retain the orphan'
                else:
                    assert process_state(child_stat) is None, f'Fixture child {child} was not reaped'
                current=len(zombies());peak_zombies=max(peak_zombies,current)
                if not observe:assert current==0, f'Unreaped child after cycle {index+1}'
            if not observe:
                store.run('fsck','--connectivity-only','--no-dangling')
                store.run('for-each-ref','--format=%(refname)')
    report=dict(cycles=cycles,observed_without_init=observe,zombies_before=len(before),
                zombies_after=len(zombies()),peak_settled_zombies=peak_zombies,
                fixture_children_reaped=0 if observe else cycles,
                post_failure_git='not_run' if observe else 'passed')
    print(json.dumps(report),flush=True)
    return report


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--cycles',type=int,default=100)
    parser.add_argument('--observe',action='store_true')
    args=parser.parse_args()
    assert 1<=args.cycles<=200 and (not args.observe or args.cycles<=12)
    check(args.cycles,args.observe)
