"""Real SSH handshake/known-host enforcement against a private loopback sshd only."""
import base64
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import _env
import remotes


@unittest.skipUnless(shutil.which('sshd') and shutil.which('ssh-keygen'), 'private sshd unavailable')
class RemoteSSHTest(unittest.TestCase):
    def test_known_host_and_real_peer_without_server_installation(self):
        with tempfile.TemporaryDirectory(prefix='remote-sshd-') as directory:
            home = Path(directory)
            for name in ('host', 'client'):
                subprocess.run(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(home/name)],check=True)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0)); port = sock.getsockname()[1]
            config = home/'sshd_config'
            config.write_text(f'Port {port}\nListenAddress 127.0.0.1\nHostKey {home}/host\n'
                f'PidFile {home}/sshd.pid\nAuthorizedKeysFile {home}/client.pub\n'
                'StrictModes no\nPasswordAuthentication no\nKbdInteractiveAuthentication no\nUsePAM no\n')
            log = (home/'sshd.log').open('w+')
            try:
                server = subprocess.Popen([shutil.which('sshd'),'-D','-e','-f',str(config)],stdout=log,stderr=log)
                try:
                    deadline = time.time()+5
                    while time.time()<deadline:
                        if server.poll() is not None:
                            self.skipTest('OS refuses an unprivileged private sshd; real SSH needs a Linux integration host')
                        try:
                            with socket.create_connection(('127.0.0.1',port),timeout=.1): break
                        except OSError: time.sleep(.05)
                    else: self.fail('private sshd did not listen')
                    public = (home/'host.pub').read_text().split()
                    (home/'known_hosts').write_text(f'[127.0.0.1]:{port} '+ ' '.join(public[:2])+'\n')
                    client = home/'ssh_config'
                    client.write_text(f'Host test-loopback\n HostName 127.0.0.1\n Port {port}\n'
                        f' User {pwd.getpwuid(os.getuid()).pw_name}\n IdentityFile {home}/client\n'
                        f' UserKnownHostsFile {home}/known_hosts\n GlobalKnownHostsFile /dev/null\n'
                        ' IdentitiesOnly yes\n StrictHostKeyChecking yes\n')
                    original = remotes.ssh
                    def transport(entry, command, **kw):
                        command = ['env','HOME='+str(home),'PATH='+str(Path(sys.executable).parent)+':/usr/bin:/bin',*command]
                        argv = original(entry,command,**kw)
                        return [argv[0],'-F',str(client),*argv[1:]]
                    entry=dict(id='ssh-test',host='test-loopback',project_path=str(home/'projects'),mode='ssh')
                    with mock.patch.object(remotes,'ssh',side_effect=transport):
                        try:
                            result=remotes.request(entry,dict(op='probe'))
                        except remotes.Unavailable:
                            log.flush(); log.seek(0)
                            detail=log.read().replace(pwd.getpwuid(os.getuid()).pw_name,'<test-user>').replace(str(home),'<test-home>')
                            if 'sandbox initialization failed' in detail and sys.platform == 'darwin':
                                self.skipTest('macOS sshd Seatbelt cannot nest inside the mandatory test sandbox; run on Linux')
                            self.fail('Private SSH failed: '+detail[-2000:])
                        self.assertEqual(result['protocol'],1)
                        self.assertFalse(result['service_installed'])
                        self.assertFalse((home/'.piper-server').exists())
                        (home/'projects').mkdir()
                        (home/'projects/repo/.git').mkdir(parents=True)
                        result=remotes.request(entry,dict(op='http',method='GET',path='/api/state',body='',ctype='application/json'))
                        self.assertEqual(result['status'],200)
                        self.assertEqual(json.loads(base64.b64decode(result['body']))['projects'][0]['key'],'repo')
                        (home/'known_hosts').write_text('')
                        with self.assertRaises(remotes.Unavailable):
                            remotes.request(entry,dict(op='probe'))
                finally:
                    if server.poll() is None: server.terminate()
                    server.wait(timeout=5)
            finally: log.close()
