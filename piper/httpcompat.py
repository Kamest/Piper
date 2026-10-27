"""Local, hash-pinned compatibility overlay for native CLI build 261.25134.95.

No JetBrains classes are distributed or modified in the installed CLI. Eighteen classes
are derived from the user's installation, checked byte-for-byte against the tested
revision, and loaded read-only ahead of it for this confined process only.
"""
from __future__ import annotations

import hashlib
import io
import os
import struct
import zipfile

RESULT = 'com/intellij/httpClient/execution/impl/RequestExecutionResult.class'
SAVE = 'com/intellij/httpClient/cli/RunExecutorKt$saveFile$2.class'
CONVERTER = 'com/intellij/httpClient/http/request/HttpRequestPsiConverter.class'
REQUEST = 'com/intellij/httpClient/execution/RestClientRequest.class'
COOKIE = 'com/intellij/httpClient/execution/impl/engineV2/HttpClientCookieJar.class'
NETTY = 'com/intellij/httpClient/execution/impl/engineV2/NettyHttpClientKt.class'
JS_HANDLER = 'com/intellij/httpClient/http/request/run/js/graalvm/HttpClientGraalVmJsHandler.class'
JS_FS = 'com/intellij/httpClient/http/request/run/js/graalvm/HttpClientGraalVmJsHandler$rawJsContext$2.class'
JS_PRE = 'com/intellij/httpClient/http/request/run/js/graalvm/HttpClientGraalVmExecutor.class'
JS_RESPONSE = 'com/intellij/httpClient/http/request/run/js/graalvm/HttpClientGraalVmJsResponseHandler.class'
SUBSCRIBERS = 'com/intellij/httpClient/http/request/run/js/graalvm/HttpClientGraalVmSubscribersHolder.class'
HTTP_BODY = 'com/intellij/httpClient/execution/impl/engineV2/HttpRequestHandlerV2Kt.class'
CLI_HANDLER = 'com/intellij/httpClient/cli/RequestHandler.class'
CLI_LIFETIME = 'com/intellij/httpClient/cli/RequestHandler$handleRequest$result$1.class'
LINE_SUBSCRIBER = 'com/intellij/httpClient/http/request/run/js/graalvm/HttpClientGraalVmSubscribersHolder$subscribeLines$1$1$1.class'
AUTH = 'com/intellij/httpClient/http/request/dynamicVariables/HttpClientDynamicVariables.class'
WS_CONFIG = 'com/intellij/httpClient/executor/websocket/WebSocketClientRequestRequestHandler$execute$1.class'
SCRIPT_RESULT = 'com/intellij/httpClient/http/request/run/HttpResponseExecutionResult.class'
NATIVE_TEST = 'com/intellij/httpClient/http/request/run/js/graalvm/HttpClientGraalVmJsResponseHandler$execute$2$1$httpClientTests$1$1.class'
HELPER = 'piper/bridge/PiperBinary'
HELPER_NAMES = ('PiperBinary.class', 'PiperBinary$CioTls.class', 'PiperBinary$InsecureTrust.class',
                'PiperBinary$TunnelTls.class', 'PiperBinary$TunnelTrust.class',
                'PiperBinary$DeferredBody.class', 'PiperBinary$StreamDecoder.class',
                'PiperBinary$TrackedContinuation.class', 'PiperModules.class', 'PiperModules$1.class')
HASHES = {CONVERTER: 'ee5d561cb09b3997dfea06da63d54e3582463c4bf902c1cefb1ff724cd4e8a23', RESULT: 'aba42ed32e741bc83c1941336e0205982bc1c447179f6d954f9802ca464ddb16',
          SAVE: '068ced4f9d8d4525c5da22fedb94e929f614de54df60bb6cc5ee34228e98fd95',
          REQUEST: 'af965cdb610d65c78df47cf46bca190138833e42219d9c6b7ec9d1454631aab5',
          COOKIE: 'a32f000f797b6c175fdeb830360784cac854fb469d06196d53dcc4c87b2c0cb8',
          NETTY: 'd5b9b5b838e85f68fbd55ea4761ebd8253dce0e7d5b2078017b1ab7b8cffa09f',
          JS_HANDLER: '7ba2454de294183d29b98d22950173ccbb8ac23d83b30d2a9e106f302c9306aa',
          JS_FS: '133a8c537abcacd6bde8ddad924fb2bd0e240e53805fcb96762a04ae054b3073',
          JS_PRE: 'f87c0a26cc4bdcc969d4c57b59325e915b2c25d80ba026158e86d5055d5573c9',
          JS_RESPONSE: '9d2b15cc8a8085ec614b6906986743b84c719eadb0dc51de2e559729a63f7a4b',
          SUBSCRIBERS: '44402e773bbe503be32df01459b430e00e5791572da15c2561527eb9fdeabe10',
          HTTP_BODY: '4be15fe0fe5ff2e4985ca109bfde4b46dcc996ba7ce0967c4c5f33dd0bc823fa',
          CLI_HANDLER: 'b1c79cbc885bc6f08cb26f1b4d5c8e85f7f20a5ec187ec8d03b993c882986689',
          CLI_LIFETIME: 'cb9f14095d7efba795f57592f2bb304964073d0dfea409c49c6a20971576e1b5',
          LINE_SUBSCRIBER: '3df9e166bc3344a7fb07c450015e31029fd96c01f1a2f510162da8362eb7ed36',
          AUTH: '6aed7f0fe053816f1f9f0a313a9d1efbf68cab10c8d671f426b2ca5afc3eb8c9',
          WS_CONFIG: '45ef538a09ae1d978a12e8062f78a287bf26f68ec692b924d3cb85e4a4381b5b',
          SCRIPT_RESULT: 'ba44d9bd1b64142b2430a436d26f41f684eba4f7cea517dfdc3d26c9248364b8',
          NATIVE_TEST: 'fff463ba67ba19756acd19d9778800fa9e64fb39c06ff4a811c049ae7f663e5f'}


def u2(data, pos):
    return struct.unpack_from('>H', data, pos)[0]


def u4(data, pos):
    return struct.unpack_from('>I', data, pos)[0]


class ClassFile:
    """Minimal lossless classfile editor; only hash-pinned input reaches mutation."""
    def __init__(self, data):
        if data[:4] != b'\xca\xfe\xba\xbe':
            raise ValueError('invalid native HTTP class')
        self.header, self.pool = data[:8], [None]
        pos = 10
        while len(self.pool) < u2(data, 8):
            tag, start = data[pos], pos
            pos += 1
            if tag == 1:
                pos += 2 + u2(data, pos)
            elif tag in (3, 4, 9, 10, 11, 12, 17, 18):
                pos += 4
            elif tag in (5, 6):
                pos += 8
            elif tag in (7, 8, 16, 19, 20):
                pos += 2
            elif tag == 15:
                pos += 3
            else:
                raise ValueError('unsupported native HTTP constant pool')
            self.pool.append(data[start:pos])
            if tag in (5, 6):
                self.pool.append(None)
        self.body = data[pos:]

    def text(self, index):
        entry = self.pool[index]
        return entry[3:].decode() if entry and entry[0] == 1 else None

    def add(self, tag, payload):
        self.pool.append(bytes([tag]) + payload)
        return len(self.pool) - 1

    def utf8(self, value):
        data = value.encode()
        return self.add(1, struct.pack('>H', len(data)) + data)

    def clazz(self, name):
        return self.add(7, struct.pack('>H', self.utf8(name)))

    def method(self, owner, name, descriptor):
        owner_index = self.clazz(owner)
        pair = self.add(12, struct.pack('>HH', self.utf8(name), self.utf8(descriptor)))
        return self.add(10, struct.pack('>HH', owner_index, pair))

    def replace_move(self):
        changed = 0
        for index, entry in enumerate(list(self.pool)):
            if not entry or entry[0] != 10:
                continue
            owner, pair = u2(entry, 1), u2(entry, 3)
            owner_name = self.text(u2(self.pool[owner], 1))
            pair_entry = self.pool[pair]
            name, desc = self.text(u2(pair_entry, 1)), self.text(u2(pair_entry, 3))
            if owner_name == 'java/nio/file/Files' and name == 'move':
                expected = '(Ljava/nio/file/Path;Ljava/nio/file/Path;[Ljava/nio/file/CopyOption;)Ljava/nio/file/Path;'
                if desc != expected:
                    raise ValueError('unexpected native HTTP export signature')
                new_owner = self.clazz(HELPER)
                new_pair = self.add(12, struct.pack('>HH', self.utf8('copyFile'), self.utf8(desc)))
                self.pool[index] = bytes([10]) + struct.pack('>HH', new_owner, new_pair)
                changed += 1
        if changed != 1:
            raise ValueError('unexpected native HTTP export call count')

    def await_constructor(self, suffix="CommonClientResponse;)V", helper="await", load=b"\x2c"):
        data = self.body
        pos = 8 + 2 * u2(data, 6)  # class header + interfaces

        def attributes(position, count):
            for _ in range(count):
                size = u4(data, position + 2)
                yield position, position + 6, size
                position += 6 + size

        fields = u2(data, pos)
        pos += 2
        for _ in range(fields):
            count = u2(data, pos + 6)
            pos += 8
            for at, _, size in attributes(pos, count):
                pos = at + 6 + size
        methods = u2(data, pos)
        pos += 2
        for _ in range(methods):
            name, desc, count = self.text(u2(data, pos + 2)), self.text(u2(data, pos + 4)), u2(data, pos + 6)
            pos += 8
            for at, content, size in attributes(pos, count):
                if name == '<init>' and desc.endswith(suffix) and self.text(u2(data, at)) == 'Code':
                    code_len = u4(data, content + 4)
                    code = data[content + 8:content + 8 + code_len]
                    if not code or code[-1] != 0xb1 or u2(data, content + 8 + code_len) != 0:
                        raise ValueError('unexpected native HTTP constructor control flow')
                    reference = self.method(HELPER, helper, '(Ljava/lang/Object;)V')
                    code = code[:-1] + load + b'\xb8' + struct.pack('>H', reference) + b'\xb1'
                    replacement = (struct.pack('>HHI', max(1, u2(data, content)), u2(data, content + 2), len(code)) +
                                   code + data[content + 8 + code_len:content + size])
                    self.body = data[:at + 2] + struct.pack('>I', len(replacement)) + replacement + data[content + size:]
                    return
                pos = at + 6 + size
        raise ValueError('native HTTP constructor is missing')

    def redirect_call(self, method, descriptor, offset, owner, name, called_descriptor, helper, *, virtual=True):
        """Replace one verified invocation with an equal-width static adapter.

        The whole native class is hash-pinned first. Verify the enclosing method,
        bytecode offset, opcode, owner and signature too; no control-flow offsets
        or stack-map entries change, and the receiver stays on the same stack.
        """
        data = self.body
        pos = 8 + 2 * u2(data, 6)
        fields = u2(data, pos)
        pos += 2
        for _ in range(fields):
            count = u2(data, pos + 6)
            pos += 8
            for _ in range(count):
                pos += 6 + u4(data, pos + 2)
        count = u2(data, pos)
        pos += 2
        for _ in range(count):
            member_name, member_desc, attributes = self.text(u2(data, pos + 2)), self.text(u2(data, pos + 4)), u2(data, pos + 6)
            pos += 8
            for _ in range(attributes):
                size = u4(data, pos + 2)
                if member_name == method and member_desc == descriptor and self.text(u2(data, pos)) == 'Code':
                    start, length = pos + 14, u4(data, pos + 10)
                    at = start + offset
                    if offset < 0 or offset + 3 > length or data[at] != (0xb6 if virtual else 0xb8):
                        raise ValueError('unexpected native HTTP invocation offset')
                    reference = self.pool[u2(data, at + 1)]
                    if reference is None or reference[0] != 10:
                        raise ValueError('unexpected native HTTP method reference')
                    actual_owner = self.text(u2(self.pool[u2(reference, 1)], 1))
                    pair = self.pool[u2(reference, 3)]
                    if (actual_owner, self.text(u2(pair, 1)), self.text(u2(pair, 3))) != (owner, name, called_descriptor):
                        raise ValueError('unexpected native HTTP invocation signature')
                    target = self.method(HELPER, helper, '(L' + owner + ';' + called_descriptor[1:] if virtual else called_descriptor)
                    self.body = data[:at] + b'\xb8' + struct.pack('>H', target) + data[at + 3:]
                    return
                pos += 6 + size
        raise ValueError('native HTTP invocation is missing')

    def wrap_static(self, name, descriptor, helper, result_descriptor, arguments=None, *, instance=False, boolean_last=False):
        """Preserve the pinned method and add a straight-line private adapter.

        No branch/exception/StackMap offset of upstream bytecode is changed.
        Loads are derived from the verified JVM descriptor (including local-slot
        widths), never guessed from arity. Instance adapters pass their receiver.
        """
        data = self.body
        pos = 8 + 2 * u2(data, 6)
        def end_member(start):
            end = start + 8
            for _ in range(u2(data, start + 6)):
                end += 6 + u4(data, end + 2)
            return end
        fields = u2(data, pos)
        pos += 2
        for _ in range(fields):
            pos = end_member(pos)
        count_at = pos
        count = u2(data, pos)
        pos += 2
        target = None
        for _ in range(count):
            if self.text(u2(data, pos + 2)) == name and self.text(u2(data, pos + 4)) == descriptor:
                if target is not None or bool(u2(data, pos) & 8) == instance:
                    raise ValueError('unexpected native HTTP adapter signature')
                target = pos
            pos = end_member(pos)
        if target is None:
            raise ValueError('native HTTP adapter method is missing')
        renamed = self.utf8('__piper_' + name)
        params, cursor = [], 1
        if not descriptor.startswith('('):
            raise ValueError('invalid native HTTP adapter descriptor')
        while cursor < len(descriptor) and descriptor[cursor] != ')':
            start = cursor
            while descriptor[cursor:cursor + 1] == '[':
                cursor += 1
            if descriptor[cursor:cursor + 1] == 'L':
                cursor = descriptor.index(';', cursor) + 1
            elif descriptor[cursor:cursor + 1] in tuple('ZBCSIJFD'):
                cursor += 1
            else:
                raise ValueError('invalid native HTTP adapter argument')
            params.append(descriptor[start:cursor])
        if descriptor[cursor:cursor + 1] != ')' or descriptor[cursor + 1:] != result_descriptor:
            raise ValueError('native HTTP adapter return type mismatch')
        if not (result_descriptor.startswith(('L', '[')) or result_descriptor == 'V'):
            raise ValueError('unsupported native HTTP adapter return type')
        params = (['Ljava/lang/Object;'] if instance else []) + params
        if arguments is not None and arguments != len(params):
            raise ValueError('native HTTP adapter argument count mismatch')
        if boolean_last and (instance or not params or params[-1] != 'Z'):
            raise ValueError('native HTTP adapter boolean mismatch')
        signature = ''.join('Ljava/lang/Object;' if p.startswith(('L', '[')) else p for p in params)
        reference = self.method(HELPER, helper, '(' + signature + ')' + result_descriptor)
        loads, slots = bytearray(), 0
        for param in params:
            opcode, compact = {'J':(0x16, 0x1e), 'F':(0x17, 0x22), 'D':(0x18, 0x26)}.get(
                param, (0x19, 0x2a) if param.startswith(('L', '[')) else (0x15, 0x1a))
            loads.extend(bytes([compact + slots]) if slots < 4 else bytes([opcode, slots]))
            slots += 2 if param in ('J', 'D') else 1
            if slots > 255:
                raise ValueError('native HTTP adapter local-slot limit')
        code = bytes(loads) + b'\xb8' + struct.pack('>H', reference) + (b'\xb1' if result_descriptor == 'V' else b'\xb0')
        attribute = struct.pack('>HHI', max(1, slots), slots, len(code)) + code + b'\0\0\0\0'
        wrapper = (data[target:target + 6] + b'\0\1' +
                   struct.pack('>HI', self.utf8('Code'), len(attribute)) + attribute)
        data = data[:target + 2] + struct.pack('>H', renamed) + data[target + 4:]
        self.body = (data[:count_at] + struct.pack('>H', count + 1) + data[count_at + 2:pos] + wrapper + data[pos:])

    def bytes(self):
        return self.header + struct.pack('>H', len(self.pool)) + b''.join(entry for entry in self.pool if entry) + self.body


def overlay(lib):
    """Unknown engine versions retain their unmodified behavior; never guess a binary patch."""
    try:
        with zipfile.ZipFile(os.path.join(lib, 'ijhttp.jar')) as source:
            if any(source.getinfo(name).file_size > 1 << 20 for name in HASHES):
                return None
            originals = {name:source.read(name) for name in HASHES}
    except (KeyError, zipfile.BadZipFile):
        return None
    if any(hashlib.sha256(data).hexdigest() != HASHES[name] for name, data in originals.items()):
        return None
    constructor, export = ClassFile(originals[RESULT]), ClassFile(originals[SAVE])
    constructor.await_constructor()
    export.replace_move()
    request = ClassFile(originals[REQUEST])
    request.await_constructor('()V', 'initRequest', b'\x2a')
    converter = ClassFile(originals[CONVERTER])
    converter.wrap_static('toRestClientRequest', '(Lcom/intellij/httpClient/http/request/psi/HttpRequest;Lcom/intellij/httpClient/http/request/HttpRequestVariableSubstitutor;)Lcom/intellij/httpClient/execution/RestClientRequest;',
                          'toRestClientRequest', 'Lcom/intellij/httpClient/execution/RestClientRequest;', 2)
    converter.wrap_static('resolveFileUrl', '(Lcom/intellij/httpClient/http/request/psi/HttpInputFile;)Ljava/lang/String;',
                          'resolveFileUrl', 'Ljava/lang/String;')
    converter.wrap_static('getFileToSend', '(Lcom/intellij/httpClient/http/request/psi/HttpInputFile;Lcom/intellij/httpClient/http/request/HttpRequestVariableSubstitutor;)Ljava/io/File;',
                          'fileToSend', 'Ljava/io/File;', 2)
    converter.wrap_static('getTextToSend', '(Lcom/intellij/psi/PsiFile;Ljava/util/List;Lcom/intellij/httpClient/http/request/HttpRequestVariableSubstitutor;)Ljava/lang/String;',
                          'textToSend', 'Ljava/lang/String;', 3)
    converter.wrap_static('convertFromHttpRequest', '(Lcom/intellij/httpClient/http/request/psi/HttpRequest;Lcom/intellij/httpClient/http/request/HttpRequestVariableSubstitutor;Lcom/intellij/httpClient/converters/RequestBuilder;)Ljava/lang/Object;',
                          'convertFromHttpRequest', 'Ljava/lang/Object;', 3)
    cookie, netty = ClassFile(originals[COOKIE]), ClassFile(originals[NETTY])
    cookie.wrap_static('selectCookiesFor', '(Ljava/net/URL;)Ljava/util/List;', 'selectCookies', 'Ljava/util/List;', 2, instance=True)
    netty.wrap_static('createReactorNettyClient', '(Lcom/intellij/httpClient/execution/RestClientRequest;Ljava/net/URL;Z)Lreactor/netty/http/client/HttpClient;',
                      'nettyClient', 'Lreactor/netty/http/client/HttpClient;', 3, boolean_last=True)
    js_handler, js_fs = ClassFile(originals[JS_HANDLER]), ClassFile(originals[JS_FS])
    js_handler.wrap_static('initScript', '(Ljava/lang/String;Ljava/lang/String;Lcom/intellij/openapi/vfs/VirtualFile;ILjava/lang/String;ILkotlin/coroutines/Continuation;)Ljava/lang/Object;',
                           'initScript', 'Ljava/lang/Object;', instance=True)
    js_fs.wrap_static('newByteChannel', '(Ljava/nio/file/Path;Ljava/util/Set;[Ljava/nio/file/attribute/FileAttribute;)Ljava/nio/channels/SeekableByteChannel;',
                      'moduleChannel', 'Ljava/nio/channels/SeekableByteChannel;', instance=True)
    js_pre = ClassFile(originals[JS_PRE])
    js_pre.wrap_static('execute', '(Ljava/lang/String;Ljava/lang/String;ILcom/intellij/httpClient/http/request/run/console/HttpClientOutputPrinter;Lcom/intellij/httpClient/http/request/run/preScript/HttpClientRequestElementsProvider;Lcom/intellij/httpClient/http/request/run/HttpRequestGlobalContext$HttpClientVariables;ILkotlin/coroutines/Continuation;)Ljava/lang/Object;',
                       'preExecute', 'Ljava/lang/Object;', instance=True)
    js_response = ClassFile(originals[JS_RESPONSE])
    js_response.wrap_static('execute', '(Lcom/intellij/httpClient/http/request/run/HttpClientSession;Lcom/intellij/httpClient/execution/common/CommonClientResponse;Ljava/lang/String;Ljava/lang/String;ILkotlin/coroutines/Continuation;)Ljava/lang/Object;',
                           'responseExecute', 'Ljava/lang/Object;', instance=True)
    subscribers = ClassFile(originals[SUBSCRIBERS])
    subscribers.await_constructor('(Lkotlinx/coroutines/flow/Flow;Lkotlinx/coroutines/CoroutineScope;Lcom/intellij/httpClient/execution/common/CommonClientResponseBody$TextStream$ClientMessagesStream;)V',
                                 'finiteSubscribers', b'\x2a')
    for kind in ('Lines', 'Messages'):
        subscribers.redirect_call('subscribe' + kind + '$lambda$0',
            '(Lcom/intellij/httpClient/http/request/run/js/graalvm/HttpClientGraalVmSubscribersHolder;Lkotlinx/coroutines/CoroutineScope;Lorg/graalvm/polyglot/Value;Lorg/graalvm/polyglot/Value;Lkotlinx/coroutines/CoroutineName;)Lkotlinx/coroutines/Deferred;',
            45, 'kotlinx/coroutines/BuildersKt', 'async$default',
            '(Lkotlinx/coroutines/CoroutineScope;Lkotlin/coroutines/CoroutineContext;Lkotlinx/coroutines/CoroutineStart;Lkotlin/jvm/functions/Function2;ILjava/lang/Object;)Lkotlinx/coroutines/Deferred;',
            'streamAsync', virtual=False)
    line_subscriber = ClassFile(originals[LINE_SUBSCRIBER])
    line_subscriber.wrap_static('emit', '(Lcom/intellij/httpClient/http/request/run/HttpResponseLine;Lkotlin/coroutines/Continuation;)Ljava/lang/Object;',
                                'streamLine', 'Ljava/lang/Object;', instance=True)
    http_body = ClassFile(originals[HTTP_BODY])
    http_body.redirect_call('restClientResponse$lambda$0', '(Lcom/intellij/httpClient/execution/impl/engineV2/HttpClientCookieJar;Lkotlinx/coroutines/CoroutineScope;Lcom/intellij/httpClient/execution/RestClientRequest;Lreactor/netty/http/client/HttpClientResponse;Lreactor/netty/ByteBufFlux;)Lorg/reactivestreams/Publisher;',
                               266, 'reactor/netty/ByteBufFlux', 'asString', '(Ljava/nio/charset/Charset;)Lreactor/core/publisher/Flux;', 'streamStrings')
    cli_handler = ClassFile(originals[CLI_HANDLER])
    cli_handler.wrap_static('handleRequest', '(Lcom/intellij/httpClient/cli/RequestDeclaration;Lcom/intellij/httpClient/cli/RequestExecutionListener;Lkotlinx/coroutines/CoroutineScope;Lcom/intellij/httpClient/execution/common/RequestContext;Lkotlin/coroutines/Continuation;)Ljava/lang/Object;',
                            'transportRequest', 'Ljava/lang/Object;', instance=True)
    lifetime = ClassFile(originals[CLI_LIFETIME])
    lifetime.redirect_call('invokeSuspend', '(Ljava/lang/Object;)Ljava/lang/Object;', 52,
                           'com/intellij/platform/util/coroutines/CoroutineScopeKt', 'childScope$default',
                           '(Lkotlinx/coroutines/CoroutineScope;Ljava/lang/String;Lkotlin/coroutines/CoroutineContext;ZILjava/lang/Object;)Lkotlinx/coroutines/CoroutineScope;',
                           'transportScope', virtual=False)
    lifetime.redirect_call('invokeSuspend', '(Ljava/lang/Object;)Ljava/lang/Object;', 131,
                           'kotlinx/coroutines/CoroutineScopeKt', 'cancel$default',
                           '(Lkotlinx/coroutines/CoroutineScope;Ljava/util/concurrent/CancellationException;ILjava/lang/Object;)V',
                           'transportCancel', virtual=False)
    auth = ClassFile(originals[AUTH])
    auth.await_constructor('()V', 'initAuthVariables', b'\x2a')
    ws = ClassFile(originals[WS_CONFIG])
    ws.wrap_static('invokeSuspend$lambda$0', '(Lio/ktor/client/HttpClientConfig;)Lkotlin/Unit;',
                   'webSocketClientConfig', 'Lkotlin/Unit;')
    ws.wrap_static('invokeSuspend', '(Ljava/lang/Object;)Ljava/lang/Object;',
                   'webSocketStep', 'Ljava/lang/Object;', instance=True)
    script_result = ClassFile(originals[SCRIPT_RESULT])
    script_result.await_constructor('(Ljava/util/List;)V', 'completeStreamTests', b'\x2a')
    helper_dir = os.path.join(os.path.dirname(__file__), 'httpcompat')
    helpers = []
    for name in HELPER_NAMES:
        with open(os.path.join(helper_dir, name), 'rb') as compiled:
            helpers.append(('piper/bridge/' + name, compiled.read()))
    result = io.BytesIO()
    with zipfile.ZipFile(result, 'w', compression=zipfile.ZIP_STORED) as destination:
        for name, data in [(RESULT, constructor.bytes()), (SAVE, export.bytes()), (REQUEST, request.bytes()), (CONVERTER, converter.bytes()),
                           (COOKIE, cookie.bytes()), (NETTY, netty.bytes()), (JS_HANDLER, js_handler.bytes()), (JS_FS, js_fs.bytes()), (JS_PRE, js_pre.bytes()), (JS_RESPONSE, js_response.bytes()), (SUBSCRIBERS, subscribers.bytes()), (LINE_SUBSCRIBER, line_subscriber.bytes()), (HTTP_BODY, http_body.bytes()), (CLI_HANDLER, cli_handler.bytes()), (CLI_LIFETIME, lifetime.bytes()), (AUTH, auth.bytes()), (WS_CONFIG, ws.bytes()), (SCRIPT_RESULT, script_result.bytes()), *helpers]:
            destination.writestr(zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)), data)
    return result.getvalue()


def install(lib, directory):
    """Called only with an app/wrapper-owned directory, never with script-supplied paths."""
    content = overlay(lib)
    if content is None:
        return ''
    os.makedirs(directory, mode=0o700, exist_ok=True)
    path = os.path.join(directory, hashlib.sha256(content).hexdigest() + '.jar')
    if not os.path.exists(path):
        # The cache is not writable by the child. Refuse links, never open an existing file for writing.
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o400)
        with os.fdopen(fd, 'wb') as output:
            output.write(content)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as existing:
        if existing.read(len(content) + 1) != content:
            raise ValueError('native HTTP compatibility cache failed integrity check')
    return path


def compatible(binary):
    if not binary:
        return False
    try:
        return overlay(os.path.join(os.path.dirname(os.path.realpath(binary)), 'lib')) is not None
    except (OSError, ValueError):
        return False
