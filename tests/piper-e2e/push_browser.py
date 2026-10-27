"""Optional Web Push gate for the isolated mobile.py harness.

Real GUI/QUIC/VAPID+aes128gcm and Chromium service worker; emulate browser push
subscription and arrival of the decrypted event. Never use live device identities.
"""
import asyncio
import json
import time
from unittest import mock


async def verify(js, until, ab, out, origin, fixture, work):
    import http_ece
    import requests
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from websockets.asyncio.client import connect
    import mobilepush

    await until("!!document.getElementById('mobile-push-welcome')")
    assert await js("!document.getElementById('app').inert && Notification.permission==='default'")
    assert await js("[...document.querySelectorAll('#mobile-push-welcome .fi-bell')].every(i=>getComputedStyle(i).maskImage!=='none')"), 'welcome bell rendered as a square'
    await ab('screenshot', str(out / 'push-welcome-390.png'))
    await ab('set', 'viewport', '320', '568')
    assert await js("document.documentElement.scrollWidth<=innerWidth && document.getElementById('mobile-push-welcome').getBoundingClientRect().height<=innerHeight/2+1 && [...document.querySelectorAll('#mobile-push-welcome button')].every(b=>b.getBoundingClientRect().height>=44)")
    await ab('screenshot', str(out / 'push-welcome-320.png'))
    await ab('find', 'role', 'button', 'click', '--name', 'Not now', '--exact')
    await ab('reload')
    await until("!document.querySelector('.mobile-stage') && !!document.querySelector('#queue [data-sid]')")
    assert await js("localStorage.getItem('piper.mobile.pushOffer')==='1' && !document.getElementById('mobile-push-welcome')"), 'dismissed offer repeated after reload'
    await ab('set', 'viewport', '390', '844')
    cdp_url = (await ab('get', 'cdp-url'))['cdpUrl']
    pending, events = {}, []
    counter = 0
    async with connect(cdp_url, proxy=None) as ws:
        async def reader():
            async for raw in ws:
                value = json.loads(raw)
                if 'id' in value:
                    future = pending.pop(value['id'])
                    if value.get('error'): future.set_exception(AssertionError(value['error']))
                    else: future.set_result(value.get('result', {}))
                else: events.append(value)
        reading = asyncio.create_task(reader())
        async def command(method, params=None, session=None):
            nonlocal counter
            counter += 1; identity = counter
            future = asyncio.get_running_loop().create_future(); pending[identity] = future
            await ws.send(json.dumps({'id': identity, 'method': method, 'params': params or {}, **({'sessionId': session} if session else {})}))
            return await asyncio.wait_for(future, 8)
        try:
            async def active_page():
                marker = 'fixture-' + str(time.monotonic_ns())
                await js('window.fixtureActivePushPage=' + json.dumps(marker))
                targets = (await command('Target.getTargets'))['targetInfos']
                for info in targets:
                    if info['type'] != 'page' or not info['url'].startswith(origin): continue
                    session = (await command('Target.attachToTarget', {'targetId': info['targetId'], 'flatten': True}))['sessionId']
                    value = await command('Runtime.evaluate', {'expression': 'window.fixtureActivePushPage===' + json.dumps(marker), 'returnByValue': True}, session)
                    if value['result'].get('value') is True: return info, session
                raise AssertionError('active test browser page not found')
            target, page = await active_page()
            await command('Browser.grantPermissions', {'origin': origin, 'browserContextId': target['browserContextId'], 'permissions': ['notifications']})
            await command('ServiceWorker.enable', session=page)
            await until("!!(await navigator.serviceWorker.getRegistration('/'))?.active")
            deadline = time.monotonic() + 8
            registration = None
            while registration is None and time.monotonic() < deadline:
                for event in events:
                    for r in event.get('params', {}).get('registrations', []):
                        if r['scopeURL'] == origin + '/': registration = r['registrationId']
                await asyncio.sleep(.05)
            assert registration, 'real service worker registration not reported by Chromium'
            key = ec.generate_private_key(ec.SECP256R1())
            public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
            auth = b'fixture-auth-key'
            subscription = {'endpoint': 'https://web.push.apple.com/' + 'x' * 40,
                'keys': {'p256dh': mobilepush.b64(public), 'auth': mobilepush.b64(auth)}}
            stub = """(()=>{
              window.fixturePushSub=null;window.fixturePushCalls=0;
              const original=Notification.requestPermission.bind(Notification);
              Notification.requestPermission=()=>{fixturePushCalls++;return original()};
              Object.defineProperty(PushManager.prototype,'getSubscription',{configurable:true,value:async()=>fixturePushSub});
              Object.defineProperty(PushManager.prototype,'subscribe',{configurable:true,value:async options=>{
                fixturePushSub={options,toJSON:()=>(SUBSCRIPTION),unsubscribe:async()=>{fixturePushSub=null;return true}};return fixturePushSub;
              }});return true;
            })()""".replace('SUBSCRIPTION', json.dumps(subscription))
            await js(stub)
            # Resolve the fixture's unrelated approval through its actual GUI before screenshots.
            await js("(()=>{document.querySelector('.mobile-paired-toast')?.remove();const deny=[...document.querySelectorAll('.pending button')].find(b=>b.textContent.trim().startsWith('Deny'));deny?.click();return true})()")
            await until("!document.querySelector('.pending')")
            await ab('find', 'role', 'button', 'click', '--name', 'Settings', '--exact')
            await until("!!document.querySelector('[data-nav=\"mobile\"]')")
            await js("document.querySelector('[data-nav=\"mobile\"]').click()")
            await until("document.querySelector('.mobile-push')?.dataset.state==='off'")
            assert await js('fixturePushCalls===0'), 'permission requested before explicit opt-in'
            await ab('screenshot', str(out / 'push-off-390.png'))
            await js("(()=>{Object.defineProperty(navigator,'userAgent',{configurable:true,value:'iPhone'});document.dispatchEvent(new Event('visibilitychange'));return true})()")
            await until("document.querySelector('.mobile-push')?.dataset.state==='install'")
            await ab('screenshot', str(out / 'push-install-390.png'))
            await js("(()=>{delete navigator.userAgent;window.fixturePermission=Object.getOwnPropertyDescriptor(Notification,'permission');Object.defineProperty(Notification,'permission',{configurable:true,get:()=> 'denied'});document.dispatchEvent(new Event('visibilitychange'));return true})()")
            await until("document.querySelector('.mobile-push')?.dataset.state==='blocked'")
            await ab('screenshot', str(out / 'push-blocked-390.png'))
            await js("(()=>{Object.defineProperty(Notification,'permission',fixturePermission);document.dispatchEvent(new Event('visibilitychange'));return true})()")
            await until("document.querySelector('.mobile-push')?.dataset.state==='off'")
            # Emulate a fresh installation's first open after validating persisted dismissal.
            await js("localStorage.removeItem('piper.mobile.pushOffer')")
            await ab('reload')
            await until("!!document.getElementById('mobile-push-welcome')")
            target, page = await active_page()
            await command('Browser.grantPermissions', {'origin': origin, 'browserContextId': target['browserContextId'], 'permissions': ['notifications']})
            await js(stub)
            assert await js('fixturePushCalls===0')
            await ab('click', '#mobile-push-welcome button.primary')
            await until("!document.getElementById('mobile-push-welcome') && fixturePushCalls===1")
            await ab('find', 'role', 'button', 'click', '--name', 'Settings', '--exact')
            await until("!!document.querySelector('[data-nav=\"mobile\"]')")
            await js("document.querySelector('[data-nav=\"mobile\"]').click()")
            await until("document.querySelector('.mobile-push')?.dataset.state==='on'")
            assert await js('fixturePushCalls===1'), 'enable did not own exactly one permission gesture'
            await ab('screenshot', str(out / 'push-on-390.png'))
            await ab('set', 'viewport', '320', '568')
            await ab('screenshot', str(out / 'push-on-320.png'))
            assert await js("document.documentElement.scrollWidth<=innerWidth && [...document.querySelectorAll('.mobile-push button')].every(e=>e.getBoundingClientRect().height>=44)"), 'push controls overflow/small on 320px'
            await ab('set', 'viewport', '390', '844')
            assert await js("!document.querySelector('.mobile-push input[role=switch]').checked"), 'session names unexpectedly enabled by the welcome strip'
            original_action = mobilepush.action
            def reject_preference(peer, grant, value):
                if value.get('action') == 'preferences': raise ValueError('fixture preference failure')
                return original_action(peer, grant, value)
            with mock.patch.object(mobilepush, 'action', side_effect=reject_preference):
                await ab('click', 'label.mobile-push-names')
                await until("document.querySelector('.mobile-push [role=alert]')?.textContent.includes('Couldn’t save') && !document.querySelector('.mobile-push input[role=switch]').checked")
                await ab('screenshot', str(out / 'push-names-rollback-390.png'))
            await ab('click', 'label.mobile-push-names')
            await until("document.querySelector('.mobile-push input[role=switch]')?.checked && !document.querySelector('.mobile-push input[role=switch]')?.disabled")
            await ab('screenshot', str(out / 'push-names-on-390.png'))
            sid = fixture['sessions']['grp/demo']
            expected_title = mobilepush.clean_title(json.loads((work / 'piperd' / 'ph' / 'sessions' / sid / 'meta.json').read_text())['title'])
            loop = asyncio.get_running_loop(); delivered = []
            async def worker_push(payload):
                targets = (await command('Target.getTargets'))['targetInfos']
                worker = next(t for t in targets if t['type'] == 'service_worker' and t['url'] == origin + '/service-worker.js' and t.get('browserContextId') == target.get('browserContextId'))
                worker_session = (await command('Target.attachToTarget', {'targetId': worker['targetId'], 'flatten': True}))['sessionId']
                # No external push provider is reachable in the mandatory sandbox.
                # Inject only its decrypted arrival into the actual worker handler.
                # Synthetic ExtendableEvents need a test-owned lifetime promise.
                expression = """new Promise((resolve,reject)=>{
                  if(!self.fixtureNativeNotices){
                    self.fixtureNativeNotices=[];
                    const native=self.registration.showNotification.bind(self.registration);
                    self.registration.showNotification=(title,options)=>native(title,options).then(()=>{
                      self.fixtureNativeNotices.push({title,body:options.body});
                    });
                  }
                  const event=new PushEvent('push',{data:DATA});
                  Object.defineProperty(event,'waitUntil',{value:p=>Promise.resolve(p).then(()=>resolve(true),reject)});
                  self.dispatchEvent(event);
                })""".replace('DATA', json.dumps(payload.decode()))
                result = await command('Runtime.evaluate', {'expression': expression, 'awaitPromise': True, 'returnByValue': True}, worker_session)
                assert not result.get('exceptionDetails'), result.get('exceptionDetails')
                assert result['result'].get('value') is True, result
            def provider_post(session, url, **kwargs):
                assert not session.trust_env and kwargs['allow_redirects'] is False
                payload = http_ece.decrypt(kwargs['data'], private_key=key, auth_secret=auth, version='aes128gcm')
                value = json.loads(payload)
                assert value == ({'v': 1, 'kind': 'test'} if value['kind'] == 'test' else {'v': 1, 'kind': 'attention', 'session': expected_title})
                try:
                    asyncio.run_coroutine_threadsafe(worker_push(payload), loop).result(9)
                except Exception as error:
                    print('Fixture CDP delivery error: ' + type(error).__name__ + ': ' + str(error), flush=True)
                    raise
                delivered.append(value['kind'])
                response = requests.Response(); response.status_code = 201; response._content = b''; return response
            with mock.patch.object(requests.Session, 'post', autospec=True, side_effect=provider_post):
                await ab('find', 'role', 'button', 'click', '--name', 'Send test', '--exact')
                await until("!!document.querySelector('.mobile-push-toast')")
                assert 'test' in delivered, 'real worker notification API did not accept the test'
                assert await js("document.querySelector('.mobile-push-toast').getBoundingClientRect().width>=innerWidth-34"), 'test toast shrinks to half the phone width'
                await ab('screenshot', str(out / 'push-test-sent-390.png'))
                await js("(async()=>{for(const n of await(await navigator.serviceWorker.ready).getNotifications())n.close();return true})()")
                event_file = work / 'piperd' / 'ph' / 'sessions' / sid / 'claude' / 'piper-events.jsonl'
                def hook(kind):
                    with event_file.open('a') as f: f.write(json.dumps({'ev': kind, 'ts': time.time()}) + '\n')
                hook('UserPromptSubmit'); await asyncio.sleep(4)
                # Navigate away: no Piper page or QUIC GUI connection remains in this browser.
                await ab('open', 'about:blank')
                hook('Stop')
                deadline = time.monotonic() + 15
                while 'attention' not in delivered and time.monotonic() < deadline: await asyncio.sleep(.1)
                assert 'attention' in delivered, 'closed app did not get a background event from piperd/bridge'
                workers = (await command('Target.getTargets'))['targetInfos']
                assert not any(t['type'] == 'page' and t['url'].startswith(origin) and t.get('browserContextId') == target.get('browserContextId') for t in workers), 'Piper page still open in delivery context'
                worker = next(t for t in workers if t['type'] == 'service_worker' and t['url'] == origin + '/service-worker.js' and t.get('browserContextId') == target.get('browserContextId'))
                worker_session = (await command('Target.attachToTarget', {'targetId': worker['targetId'], 'flatten': True}))['sessionId']
                result = await command('Runtime.evaluate', {'expression': '({accepted:self.fixtureNativeNotices,systemVisible:null})', 'returnByValue': True}, worker_session)
                notices = result['result']['value']['accepted']
                assert any(n['body'] == 'Test notification from Piper.' for n in notices)
                assert any(n['body'].startswith('“' + expected_title + '” needs your attention.') for n in notices), 'opted-in session title not shown by actual worker'
                assert any(n['title'] == 'Piper' and 'needs your attention' in n['body'] for n in notices), 'actual background worker did not show notification'
                # A user-chosen session name can itself equal the project label.
                # No project path/ID is added outside that exact opted-in name.
                assert all('grp/demo' not in n['body'].replace('“' + expected_title + '”', '') and sid not in n['body'] for n in notices)
                # OS presentation/history is not a sandbox contract (macOS can suppress
                # it despite a fulfilled real showNotification). Do not claim delivery.
                (out / 'push-background-proof.json').write_text(json.dumps({'piper_page_closed': True, 'actual_worker_api_accepted': True, 'push_arrival_emulated': True, 'system_presentation_verified': False, 'encrypted_provider_payloads': delivered}, indent=2) + '\n')
            # Reopen the same paired browser; clear the subscription through the real peer-bound transport.
            await ab('open', origin)
            await until("!document.querySelector('.mobile-stage') && !!document.querySelector('#queue [data-sid]')")
            assert await js("!document.getElementById('mobile-push-welcome')"), 'offer repeated after background reopen'
            await js("(async()=>{const p=await import('/piper/dist/mobilepush.js');await p.disablePush();return true})()")
            print('PASS: first-open offer, persisted Not now/reopen, explicit gesture, name toggle rollback/opt-in and actual Chromium SW named alert with Piper closed; subscription/arrival emulated, OS presentation unverified', flush=True)
        finally:
            reading.cancel(); await asyncio.gather(reading, return_exceptions=True)
