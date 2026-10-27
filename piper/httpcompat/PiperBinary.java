package piper.bridge;

import com.intellij.httpClient.execution.common.CommonClientResponse;
import com.intellij.httpClient.execution.common.CommonClientResponseBody;
import java.nio.file.*;
import java.util.concurrent.CancellationException;

/** Completes the native binary download before its request scope is disposed.
 * Copies exports without consuming the file still used by native response scripts.
 * All operations remain inside the existing per-run OS sandbox.
 */
public final class PiperBinary {
  private static final java.util.Map<Object, kotlinx.coroutines.CoroutineScope> streamScopes =
      java.util.Collections.synchronizedMap(new java.util.IdentityHashMap<>());
  private static final java.util.Set<kotlinx.coroutines.CoroutineScope> heldScopes =
      java.util.Collections.synchronizedSet(java.util.Collections.newSetFromMap(new java.util.IdentityHashMap<>()));
  /** The CLI cancels the transport child before running response scripts. A
   * sibling under the enclosing request's parent survives that local boundary
   * while retaining cancellation by the enclosing run/deadline.
   */
  public static kotlinx.coroutines.CoroutineScope transportScope(kotlinx.coroutines.CoroutineScope scope, String name,
      kotlin.coroutines.CoroutineContext context, boolean supervisor, int mask, Object marker) throws Exception {
    var job = scope.getCoroutineContext().get(kotlinx.coroutines.Job.Key);
    if (job == null || job.getParent() == null) throw new IllegalStateException("Native HTTP transport parent missing");
    var parent = kotlinx.coroutines.CoroutineScopeKt.CoroutineScope(
        scope.getCoroutineContext().minusKey(kotlinx.coroutines.Job.Key).plus(job.getParent()));
    var method = com.intellij.platform.util.coroutines.CoroutineScopeKt.class.getDeclaredMethod("childScope$default",
        kotlinx.coroutines.CoroutineScope.class, String.class, kotlin.coroutines.CoroutineContext.class,
        boolean.class, int.class, Object.class);
    return (kotlinx.coroutines.CoroutineScope) method.invoke(null, parent, name, context, supervisor, mask, marker);
  }
  @SuppressWarnings("unchecked")
  public static Object transportRequest(Object receiver, Object request, Object listener, Object scope, Object context, Object continuation)
      throws Exception {
    var typed = (kotlinx.coroutines.CoroutineScope) scope;
    java.util.function.Consumer<Object> completed = value -> {
      if (value instanceof com.intellij.httpClient.execution.impl.RequestExecutionResult result &&
          result.getResponse().getBody() instanceof CommonClientResponseBody.TextStream body) {
        heldScopes.add(typed); streamScopes.put(body, typed);
      }
    };
    Object callback = typed == null ? continuation : new TrackedContinuation(
        (kotlin.coroutines.Continuation<Object>) continuation, completed);
    Object result = invokeOriginal(receiver.getClass(), receiver, "handleRequest", new Class<?>[]{
        com.intellij.httpClient.cli.RequestDeclaration.class, Class.forName("com.intellij.httpClient.cli.RequestExecutionListener"),
        kotlinx.coroutines.CoroutineScope.class, com.intellij.httpClient.execution.common.RequestContext.class,
        kotlin.coroutines.Continuation.class}, request, listener, scope, context, callback);
    if (typed != null && result != kotlin.coroutines.intrinsics.IntrinsicsKt.getCOROUTINE_SUSPENDED()) completed.accept(result);
    return result;
  }
  /** Only the successful stream path defers cancellation; all error paths keep native cleanup. */
  public static void transportCancel(kotlinx.coroutines.CoroutineScope scope, CancellationException reason, int mask, Object marker) {
    if (!heldScopes.contains(scope)) kotlinx.coroutines.CoroutineScopeKt.cancel(scope, (mask & 1) != 0 ? null : reason);
  }
  private static void releaseStream(Object body) {
    var scope = streamScopes.remove(body);
    if (scope != null) {
      heldScopes.remove(scope);
      kotlinx.coroutines.CoroutineScopeKt.cancel(scope, (CancellationException) null);
    }
  }
  private static final class TrackedContinuation implements kotlin.coroutines.Continuation<Object> {
    private final kotlin.coroutines.Continuation<Object> delegate;
    private final java.util.function.Consumer<Object> completed;
    TrackedContinuation(kotlin.coroutines.Continuation<Object> delegate, java.util.function.Consumer<Object> completed) {
      this.delegate = delegate; this.completed = completed;
    }
    @Override public kotlin.coroutines.CoroutineContext getContext() { return delegate.getContext(); }
    @Override public void resumeWith(Object result) { completed.accept(result); delegate.resumeWith(result); }
  }
  /** Subscribe before returning response headers, but request bytes only when
   * the native Flow asks for them. This retains backpressure without replaying
   * HTTP, buffering a whole stream, or dropping bytes during JS initialization.
   */
  public static reactor.core.publisher.Flux<String> streamStrings(reactor.netty.ByteBufFlux source, java.nio.charset.Charset charset) {
    var bridge = new DeferredBody();
    var decoder = new StreamDecoder(charset);
    source.asByteArray().subscribe(bridge);
    return reactor.core.publisher.Flux.<byte[]>create(bridge::attach, reactor.core.publisher.FluxSink.OverflowStrategy.ERROR)
        .map(decoder::decode).concatWith(reactor.core.publisher.Mono.fromSupplier(decoder::finish));
  }
  private static final class DeferredBody extends reactor.core.publisher.BaseSubscriber<byte[]> {
    private reactor.core.publisher.FluxSink<byte[]> sink;
    private boolean done;
    private Throwable failure;
    @Override protected void hookOnSubscribe(org.reactivestreams.Subscription subscription) { }
    synchronized void attach(reactor.core.publisher.FluxSink<byte[]> value) {
      if (sink != null) { value.error(new IllegalStateException("HTTP stream already consumed")); return; }
      sink = value;
      value.onCancel(this::cancel);
      if (done) { if (failure == null) value.complete(); else value.error(failure); }
      else value.onRequest(this::request);
    }
    @Override protected synchronized void hookOnNext(byte[] value) {
      if (sink == null) { cancel(); throw new IllegalStateException("HTTP stream emitted without demand"); }
      sink.next(value);
    }
    @Override protected synchronized void hookOnComplete() { done = true; if (sink != null) sink.complete(); }
    @Override protected synchronized void hookOnError(Throwable error) { done = true; failure = error; if (sink != null) sink.error(error); }
  }
  /** Decode across network chunks; a UTF-8 character is not a packet boundary. */
  private static final class StreamDecoder {
    private final java.nio.charset.CharsetDecoder decoder;
    private byte[] carry = new byte[0];
    StreamDecoder(java.nio.charset.Charset charset) {
      decoder = charset.newDecoder().onMalformedInput(java.nio.charset.CodingErrorAction.REPLACE)
          .onUnmappableCharacter(java.nio.charset.CodingErrorAction.REPLACE);
    }
    String decode(byte[] bytes) { return decode(bytes, false); }
    String finish() { return decode(new byte[0], true); }
    private String decode(byte[] bytes, boolean last) {
      var input = java.nio.ByteBuffer.allocate(Math.addExact(carry.length, bytes.length));
      input.put(carry).put(bytes).flip();
      var output = java.nio.CharBuffer.allocate(8192);
      var text = new StringBuilder();
      while (true) {
        var result = decoder.decode(input, output, last);
        output.flip(); text.append(output); output.clear();
        if (result.isError()) throw new IllegalStateException("HTTP stream decoding failed");
        if (result.isUnderflow()) break;
      }
      carry = new byte[input.remaining()]; input.get(carry);
      if (last) while (true) {
        var result = decoder.flush(output);
        output.flip(); text.append(output); output.clear();
        if (result.isError()) throw new IllegalStateException("HTTP stream decoding failed");
        if (result.isUnderflow()) break;
      }
      return text.toString();
    }
  }
  /** Native line collectors wait forever on SharedFlow after its terminal event.
   * Complete only on that event; retain it to flush the final unterminated line.
   * Transport failures remain failures, and cancellation never fabricates EOF.
   */
  @SuppressWarnings("unchecked")
  public static void finiteSubscribers(Object receiver) throws Exception {
    var field = receiver.getClass().getDeclaredField("content"); field.setAccessible(true);
    var content = (kotlinx.coroutines.flow.Flow<CommonClientResponseBody.TextStream.Message>) field.get(receiver);
    var finite = kotlinx.coroutines.flow.FlowKt.takeWhile(content, (message, continuation) -> {
      if (message instanceof CommonClientResponseBody.TextStream.Message.ConnectionClosed.WithError)
        throw new IllegalStateException("HTTP stream ended with a transport error");
      return !(message instanceof CommonClientResponseBody.TextStream.Message.ConnectionClosed.End);
    });
    field.set(receiver, kotlinx.coroutines.flow.FlowKt.onCompletion(finite, (collector, error, continuation) -> {
      if (error == null) return collector.emit(CommonClientResponseBody.TextStream.Message.ConnectionClosed.End.INSTANCE, continuation);
      return kotlin.Unit.INSTANCE;
    }));
  }
  /** Await subscriber failures in the native handler. A child throwing exit must
   * not cancel that handler before its native exit/error/finally paths can run.
   * Each supervisor is still a child of the run and completes with its one job.
   */
  public static kotlinx.coroutines.Deferred<?> streamAsync(kotlinx.coroutines.CoroutineScope scope,
      kotlin.coroutines.CoroutineContext context, kotlinx.coroutines.CoroutineStart start,
      kotlin.jvm.functions.Function2<?, ?, ?> block, int mask, Object marker) throws Exception {
    var supervisor = kotlinx.coroutines.SupervisorKt.SupervisorJob(scope.getCoroutineContext().get(kotlinx.coroutines.Job.Key));
    var supervised = kotlinx.coroutines.CoroutineScopeKt.CoroutineScope(scope.getCoroutineContext().plus(supervisor));
    try {
      var method = kotlinx.coroutines.BuildersKt.class.getDeclaredMethod("async$default",
          kotlinx.coroutines.CoroutineScope.class, kotlin.coroutines.CoroutineContext.class,
          kotlinx.coroutines.CoroutineStart.class, kotlin.jvm.functions.Function2.class, int.class, Object.class);
      var job = (kotlinx.coroutines.Deferred<?>) method.invoke(null, supervised, context, start, block, mask, marker);
      job.invokeOnCompletion(cause -> { supervisor.complete(); return kotlin.Unit.INSTANCE; });
      return job;
    } catch (Exception error) { supervisor.cancel((CancellationException) null); throw error; }
  }
  /** Internal EOF is not a received line. JSON text "null" still passes normally. */
  public static Object streamLine(Object receiver, Object value, Object continuation) throws Exception {
    var line = (com.intellij.httpClient.http.request.run.HttpResponseLine) value;
    if (line.getValue() == null) return kotlin.Unit.INSTANCE;
    return invokeOriginal(receiver.getClass(), receiver, "emit", new Class<?>[]{
        com.intellij.httpClient.http.request.run.HttpResponseLine.class, kotlin.coroutines.Continuation.class}, value, continuation);
  }
  private static final java.util.Map<Object, Boolean> streamHandlers =
      java.util.Collections.synchronizedMap(new java.util.WeakHashMap<>());
  /** Bind the actual native response kind, never an authored MIME hint or flag. */
  @SuppressWarnings("unchecked")
  public static Object responseExecute(Object receiver, Object session, Object response,
                                       Object source, Object common, int offset, Object continuation) throws Exception {
    if (response instanceof CommonClientResponse nativeResponse)
      streamHandlers.put(receiver, nativeResponse.getBody() instanceof CommonClientResponseBody.TextStream);
    Object body = response instanceof CommonClientResponse nativeResponse ? nativeResponse.getBody() : null;
    Object callback = body != null && streamScopes.containsKey(body)
        ? new TrackedContinuation((kotlin.coroutines.Continuation<Object>) continuation, result -> releaseStream(body)) : continuation;
    try {
      Object result = invokeOriginal(receiver.getClass(), receiver, "execute", new Class<?>[]{
          com.intellij.httpClient.http.request.run.HttpClientSession.class, CommonClientResponse.class,
          String.class, String.class, int.class, kotlin.coroutines.Continuation.class},
          session, response, source, common, offset, callback);
      if (result != kotlin.coroutines.intrinsics.IntrinsicsKt.getCOROUTINE_SUSPENDED()) releaseStream(body);
      return result;
    } catch (Exception error) { releaseStream(body); throw error; }
  }
  /** Native snapshots tests before awaiting subscribers. Recreate its own test
   * objects after that wait, without evaluating any test or replaying a handler.
   * Enabled only for actual native streams; ordinary HTTP remains untouched.
   */
  public static void completeStreamTests(Object value) throws Exception {
    var result = (com.intellij.httpClient.http.request.run.HttpResponseExecutionResult) value;
    if (result.getTests().isEmpty()) return;
    Object first = result.getTests().get(0);
    Class<?> type = first.getClass();
    if (!type.getName().equals("com.intellij.httpClient.http.request.run.js.graalvm.HttpClientGraalVmJsResponseHandler$execute$2$1$httpClientTests$1$1"))
      return;
    String[] names = {"this$0", "$commonScriptOffset", "$globalVariables", "$result"};
    Object[] captured = new Object[4];
    for (int i = 0; i < names.length; i++) {
      var field = type.getDeclaredField(names[i]); field.setAccessible(true);
      captured[i] = field.get(first);
      if (i == 0 && !Boolean.TRUE.equals(streamHandlers.get(captured[0]))) return;
    }
    var clientGetter = captured[3].getClass().getDeclaredMethod("getClient");
    clientGetter.setAccessible(true);
    var client = (org.graalvm.polyglot.Value) clientGetter.invoke(captured[3]);
    var tests = client.getMember("tests").as(java.util.Map.class);
    if (tests.size() > 65536) throw new IllegalStateException("Stream test count limit");
    var constructors = type.getDeclaredConstructors();
    if (constructors.length != 1) throw new IllegalStateException("Unsupported native stream tests");
    var constructor = constructors[0]; constructor.setAccessible(true);
    var complete = new java.util.ArrayList<com.intellij.httpClient.http.request.run.HttpClientTest>();
    for (Object entry : tests.entrySet())
      complete.add((com.intellij.httpClient.http.request.run.HttpClientTest) constructor.newInstance(
          entry, captured[0], captured[1], captured[2], captured[3]));
    var field = result.getClass().getDeclaredField("myTests"); field.setAccessible(true);
    field.set(result, complete);
  }
  /** Native WS only catches IO failures; TLS/config failures otherwise strand its
   * handshake waiter. Fail this dedicated process, never retry or proceed later.
   */
  public static Object webSocketStep(Object receiver, Object result) throws Exception {
    try {
      return invokeOriginal(receiver.getClass(), receiver, "invokeSuspend", new Class<?>[]{Object.class}, result);
    } catch (Exception error) {
      Runtime.getRuntime().halt(92);
      throw new IllegalStateException("WebSocket execution failed; not retried");
    }
  }
  /** CIO has its own HTTP proxy configuration; the CLI SOCKS option does not reach it. */
  @SuppressWarnings("unchecked")
  public static kotlin.Unit webSocketClientConfig(Object value) throws Exception {
    invokeOriginal(Class.forName("com.intellij.httpClient.executor.websocket.WebSocketClientRequestRequestHandler$execute$1"),
        null, "invokeSuspend$lambda$0", new Class<?>[]{io.ktor.client.HttpClientConfig.class}, value);
    int port = Integer.getInteger("piper.http.proxy", 0);
    if (port < 1 || port > 65535) throw new IllegalStateException("WebSocket egress proxy missing");
    var config = (io.ktor.client.HttpClientConfig<io.ktor.client.engine.HttpClientEngineConfig>) value;
    config.setFollowRedirects(false);
    config.engine(engine -> {
      engine.setProxy(new java.net.Proxy(java.net.Proxy.Type.HTTP, new java.net.InetSocketAddress("127.0.0.1", port)));
      try {
        // WS requests do not carry RestClientRequest.sslConfiguration. Reuse the
        // same native certificate reader and the same host-frozen per-run input.
        var request = new com.intellij.httpClient.execution.RestClientRequest();
        if (request.sslConfiguration != null)
          request.sslConfiguration.applyTo(new CioTls(((io.ktor.client.engine.cio.CIOEngineConfig) engine).getHttps()));
      } catch (Exception error) {
        // Engine construction precedes native WS error handling. End this one
        // confined runner instead of leaving its handshake waiter suspended.
        Runtime.getRuntime().halt(88);
        throw new IllegalStateException("WebSocket TLS configuration failed; not retried");
      }
      return kotlin.Unit.INSTANCE;
    });
    return kotlin.Unit.INSTANCE;
  }
  private static final class CioTls implements com.intellij.httpClient.execution.ssl.SslConfigurationTarget<kotlin.Unit> {
    private final io.ktor.network.tls.TLSConfigBuilder config;
    CioTls(io.ktor.network.tls.TLSConfigBuilder config) { this.config = config; }
    @Override public void configureKeyStore(java.security.KeyStore store, String password) {
      try {
        int count = 0;
        var aliases = store.aliases();
        while (aliases.hasMoreElements()) {
          String alias = aliases.nextElement();
          if (!store.isKeyEntry(alias)) continue;
          var key = (java.security.PrivateKey) store.getKey(alias, password == null ? new char[0] : password.toCharArray());
          var chain = store.getCertificateChain(alias);
          if (key == null || chain == null || chain.length == 0) throw new java.security.KeyStoreException();
          var certificates = java.util.Arrays.copyOf(chain, chain.length, java.security.cert.X509Certificate[].class);
          config.getCertificates().add(new io.ktor.network.tls.CertificateAndKey(certificates, key));
          count++;
        }
        if (count != 1) throw new java.security.KeyStoreException();
      } catch (Exception error) { throw new IllegalStateException("WebSocket client identity could not be loaded"); }
    }
    @Override public void configureTrustManager(boolean insecure) {
      // Only the immutable host TLS choice can disable checking. No script flag.
      if (insecure) config.setTrustManager(new InsecureTrust());
    }
    @Override public kotlin.Unit finish() { return kotlin.Unit.INSTANCE; }
  }
  private static final class InsecureTrust implements javax.net.ssl.X509TrustManager {
    @Override public void checkClientTrusted(java.security.cert.X509Certificate[] chain, String authType) { }
    @Override public void checkServerTrusted(java.security.cert.X509Certificate[] chain, String authType) { }
    @Override public java.security.cert.X509Certificate[] getAcceptedIssuers() { return new java.security.cert.X509Certificate[0]; }
  }
  /** Add native dynamic functions; native argument parsing and JS binding remain intact. */
  public static void initAuthVariables(Object instance) throws Exception {
    Path enabled = Path.of(System.getProperty("piper.http.work"), "auth-broker.json");
    if (!Files.isRegularFile(enabled, LinkOption.NOFOLLOW_LINKS)) return;
    var variables = (com.intellij.httpClient.http.request.dynamicVariables.HttpClientDynamicVariables) instance;
    var collection = variables.getVariablesCollection();
    var descriptions = new java.util.ArrayList<>(collection.allVariables());
    for (String kind : java.util.List.of("access", "id")) {
      String name = kind.equals("id") ? "auth.idToken" : "auth.token";
      var parameter = new com.intellij.httpClient.http.request.dynamicVariables.HttpClientDynamicVariableValue.Parameter(
          "authId", com.intellij.httpClient.http.request.dynamicVariables.HttpClientDynamicVariableValue.ParameterType.STRING);
      var value = new com.intellij.httpClient.http.request.dynamicVariables.HttpClientDynamicVariableValue.FunctionValue(
          new com.intellij.httpClient.http.request.dynamicVariables.HttpClientDynamicVariableValue.Parameter[]{parameter},
          (context, arguments) -> {
            try {
              var argument = (com.intellij.httpClient.http.request.dynamicVariables.HttpClientDynamicVariableValue.ParameterValue.Str) arguments.get(0);
              var message = new com.google.gson.JsonObject();
              message.addProperty("kind", "auth");
              message.addProperty("id", argument.getValue());
              message.addProperty("token", kind);
              return rpc(message).get("token").getAsString();
            } catch (Exception error) {
              // Do not let the converter send a literal unresolved credential.
              Runtime.getRuntime().halt(91);
              throw new IllegalStateException("Managed authentication failed; not retried");
            }
          });
      descriptions.add(new com.intellij.httpClient.http.request.dynamicVariables.HttpClientDynamicVariableDescription(
          name, value, com.intellij.httpClient.http.request.dynamicVariables.HttpClientDynamicVariableDescription.Position.ALL));
    }
    var byName = new java.util.LinkedHashMap<String, com.intellij.httpClient.http.request.dynamicVariables.HttpClientDynamicVariableDescription>();
    for (var item : descriptions) byName.put(item.getName(), item);
    // These are per-service, non-static fields of the pinned revision. Native
    // lookup and initDynamicVariables see the same complete collection.
    for (String fieldName : java.util.List.of("allVariables", "variablesByName")) {
      var field = collection.getClass().getDeclaredField(fieldName);
      field.setAccessible(true);
      field.set(collection, fieldName.equals("allVariables") ? descriptions : byName);
    }
  }
  private static final java.util.Map<Object, String> codeContexts =
      java.util.Collections.synchronizedMap(new java.util.IdentityHashMap<>());
  private static final java.util.Map<Object, Integer> codeIterations =
      java.util.Collections.synchronizedMap(new java.util.IdentityHashMap<>());
  private static final java.util.Map<Object, com.intellij.httpClient.http.request.run.preScript.HttpClientRequestElementsProvider> codeProviders =
      java.util.Collections.synchronizedMap(new java.util.IdentityHashMap<>());
  private static final java.util.Map<Object, Path> codeRoots =
      java.util.Collections.synchronizedMap(new java.util.IdentityHashMap<>());
  private static final java.util.Map<Object, Path> codeRootFiles =
      java.util.Collections.synchronizedMap(new java.util.IdentityHashMap<>());
  public static Object preExecute(Object receiver, Object source, Object common, int offset, Object printer,
                                  Object provider, Object globals, int commonOffset, Object continuation) throws Exception {
    if (provider != null) {
      var elements = (com.intellij.httpClient.http.request.run.preScript.HttpClientRequestElementsProvider) provider;
      codeIterations.put(receiver, elements.iteration());
      codeProviders.put(receiver, elements);
    }
    return invokeOriginal(com.intellij.httpClient.http.request.run.js.graalvm.HttpClientGraalVmExecutor.class,
        receiver, "execute", new Class<?>[]{String.class, String.class, int.class,
            com.intellij.httpClient.http.request.run.console.HttpClientOutputPrinter.class,
            com.intellij.httpClient.http.request.run.preScript.HttpClientRequestElementsProvider.class,
            com.intellij.httpClient.http.request.run.HttpRequestGlobalContext.HttpClientVariables.class,
            int.class, kotlin.coroutines.Continuation.class}, source, common, offset, printer, provider, globals, commonOffset, continuation);
  }
  private static com.google.gson.JsonObject codeConfiguration;
  private static synchronized com.google.gson.JsonObject codeConfiguration() throws Exception {
    if (codeConfiguration == null) {
      Path path = Path.of(System.getProperty("piper.http.work"), "code-state.json");
      codeConfiguration = Files.isRegularFile(path, LinkOption.NOFOLLOW_LINKS)
          ? com.google.gson.JsonParser.parseString(Files.readString(path)).getAsJsonObject() : new com.google.gson.JsonObject();
    }
    return codeConfiguration;
  }
  /** Bind a handler before native compilation (imports run before the root body). */
  public static Object initScript(Object receiver, Object source, Object template, Object file, int offset,
                                 Object common, int commonOffset, Object continuation) throws Exception {
    var config = codeConfiguration();
    if (config.has("contexts") && receiver instanceof com.intellij.httpClient.http.request.run.js.graalvm.HttpClientGraalVmJsResponseHandler response) {
      var field = response.getClass().getDeclaredField("substitutor");
      field.setAccessible(true);
      codeIterations.put(receiver, ((com.intellij.httpClient.http.request.HttpRequestVariableSubstitutor) field.get(response)).iteration());
    }
    if (source instanceof String text && config.has("contexts")) {
      var match = java.util.regex.Pattern.compile("/\\*piper-code:([a-f0-9]{32})\\*/").matcher(text);
      if (match.find() && config.getAsJsonObject("contexts").has(match.group(1))) {
        String token = match.group(1);
        codeContexts.put(receiver, token);
        var descriptor = config.getAsJsonObject("contexts").getAsJsonObject(token);
        if (descriptor.has("script_reference") && text.contains(descriptor.get("root_marker").getAsString())) {
          String operation = "substitution";
          try {
            com.intellij.httpClient.http.request.HttpRequestVariableSubstitutor substitutor;
            com.intellij.openapi.project.Project project;
            if (codeProviders.containsKey(receiver)) {
              substitutor = codeProviders.get(receiver).getSubstitutor();
              project = codeProviders.get(receiver).getProject();
            } else {
              var subField = receiver.getClass().getDeclaredField("substitutor");
              subField.setAccessible(true);
              substitutor = (com.intellij.httpClient.http.request.HttpRequestVariableSubstitutor) subField.get(receiver);
              var projectField = receiver.getClass().getDeclaredField("myProject");
              projectField.setAccessible(true);
              project = (com.intellij.openapi.project.Project) projectField.get(receiver);
            }
            String reference = descriptor.get("script_reference").getAsString();
            reference = com.intellij.httpClient.http.request.HttpRequestPsiConverter.fillVariables(reference, substitutor, project,
                com.intellij.httpClient.http.request.HttpRequestVariableSubstitutionKey.insideExternalFileKeyFactory(reference));
            var message = new com.google.gson.JsonObject();
            message.addProperty("kind", "script_load");
            message.addProperty("token", token);
            message.addProperty("reference", reference);
            message.addProperty("iteration", codeIterations.get(receiver));
            var readTemplate = com.intellij.httpClient.http.request.run.js.graalvm.HttpClientGraalVmJsHandler.class.getDeclaredMethod(
                "access$readJsHandlerTemplate", com.intellij.httpClient.http.request.run.js.graalvm.HttpClientGraalVmJsHandler.class, String.class);
            String nativeTemplate = (String) readTemplate.invoke(null, receiver, template);
            if (common instanceof String shared) nativeTemplate = nativeTemplate.replace("//<COMMON_SCRIPT_PLACEHOLDER>", shared);
            int customAt = nativeTemplate.indexOf("//<CUSTOM_SCRIPT_PLACEHOLDER>");
            if (customAt < 0) throw new java.io.IOException("Missing native script template marker");
            var newlines = java.util.regex.Pattern.compile("\\r\\n|[\\r\\n\\u2028\\u2029]");
            message.addProperty("prefix", newlines.matcher(nativeTemplate.substring(0, customAt)).results().count() +
                newlines.matcher(text.substring(0, text.indexOf(descriptor.get("root_marker").getAsString()))).results().count());
            operation = "approval";
            var reply = rpc(message);
            operation = "snapshot";
            Path snapshot = Path.of(reply.get("path").getAsString());
            byte[] bytes = frozen(snapshot, reply.get("sha").getAsString());
            source = text.replace(descriptor.get("root_marker").getAsString(),
                new String(bytes, java.nio.charset.StandardCharsets.UTF_8));
            operation = "source_file";
            file = com.intellij.openapi.vfs.VirtualFileManager.getInstance().findFileByNioPath(snapshot);
            if (file == null) throw new java.io.IOException("Missing root snapshot");
            offset = 0;
            codeRoots.put(receiver, Path.of(reply.get("root").getAsString()));
            codeRootFiles.put(receiver, snapshot);
          } catch (Exception error) {
            var aborted = new com.google.gson.JsonObject();
            aborted.addProperty("kind", "code_abort");
            aborted.addProperty("token", token);
            aborted.addProperty("step", operation);
            try { rpc(aborted); } catch (Exception ignored) { }
            Runtime.getRuntime().halt(90);
            throw new java.io.IOException("Runtime script was refused; earlier requests may have run; not retried");
          }
        }
      }
    }
    return invokeOriginal(com.intellij.httpClient.http.request.run.js.graalvm.HttpClientGraalVmJsHandler.class,
        receiver, "initScript", new Class<?>[]{String.class, String.class, com.intellij.openapi.vfs.VirtualFile.class,
            int.class, String.class, int.class, kotlin.coroutines.Continuation.class},
        source, template, file, offset, common, commonOffset, continuation);
  }
  private static byte[] frozen(Path snapshot, String digest) throws Exception {
    if (!Files.isRegularFile(snapshot, LinkOption.NOFOLLOW_LINKS) || Files.size(snapshot) > LIMIT)
      throw new java.io.IOException("Invalid module snapshot");
    byte[] bytes = Files.readAllBytes(snapshot);
    if (!java.util.HexFormat.of().formatHex(java.security.MessageDigest.getInstance("SHA-256").digest(bytes)).equals(digest))
      throw new java.io.IOException("Changed module snapshot");
    return bytes;
  }
  /** All non-bundled modules come from host-approved immutable snapshots. */
  public static java.nio.channels.SeekableByteChannel moduleChannel(Object receiver, Object input, Object options, Object attributes)
      throws Exception {
    Path path = (Path) input;
    var config = codeConfiguration();
    var builtins = java.util.Set.of("js-graalvm-pre-script-handler.js", "js-graalvm-stdlib.js",
        "js-graalvm-response-handler.js", "js-graalvm-jwt.js", "js-graalvm-jwa.js");
    if (!config.has("contexts") || builtins.contains(path.getFileName().toString()))
      return (java.nio.channels.SeekableByteChannel) invokeOriginal(receiver.getClass(), receiver, "newByteChannel",
          new Class<?>[]{Path.class, java.util.Set.class, java.nio.file.attribute.FileAttribute[].class}, input, options, attributes);
    try {
      var field = receiver.getClass().getDeclaredField("this$0");
      field.setAccessible(true);
      Object handler = field.get(receiver);
      String token = codeContexts.get(handler);
      if (token == null) throw new java.io.IOException("Missing module context");
      var parent = com.intellij.httpClient.http.request.run.js.graalvm.HttpClientGraalVmJsHandler.class.getDeclaredMethod("getSourceFile");
      parent.setAccessible(true);
      var sourceFile = (com.intellij.openapi.vfs.VirtualFile) parent.invoke(handler);
      Path sourceBase = codeRootFiles.containsKey(handler) ? codeRootFiles.get(handler).getParent() : Path.of(sourceFile.getParent().getPath());
      path = path.isAbsolute() ? path.normalize() : sourceBase.resolve(path).normalize();
      var files = config.getAsJsonObject("files");
      String digest;
      Path snapshot;
      Path mirror = Path.of(System.getProperty("piper.http.work"), "project");
      Path root = codeRoots.get(handler);
      if (root != null && path.startsWith(root)) path = mirror.resolve(root.relativize(path));
      if (files.has(path.toString()) && !path.startsWith(mirror)) {
        snapshot = path;
        digest = files.get(path.toString()).getAsString();
      } else {
        if (!path.startsWith(mirror)) throw new java.io.IOException("Module outside project mirror");
        var message = new com.google.gson.JsonObject();
        message.addProperty("kind", "code_load");
        message.addProperty("token", token);
        message.addProperty("path", mirror.relativize(path).toString());
        message.addProperty("iteration", codeIterations.get(handler));
        var reply = rpc(message);
        snapshot = Path.of(reply.get("path").getAsString());
        digest = reply.get("sha").getAsString();
      }
      return new org.apache.commons.compress.utils.SeekableInMemoryByteChannel(frozen(snapshot, digest));
    } catch (Exception error) {
      Runtime.getRuntime().halt(90);
      throw new java.io.IOException("Runtime code was refused; earlier requests may have run; not retried");
    }
  }
  private static final long LIMIT = 64L << 20;
  /** A resource-limit abort ends only this dedicated confined JVM. Host cleanup owns its files. */
  private static void tooLarge() {
    Runtime.getRuntime().halt(86);
  }
  public static void await(Object value) throws InterruptedException {
    CommonClientResponse response = (CommonClientResponse) value;
    if (!(response.getBody() instanceof CommonClientResponseBody.Binary)) return;
    var body = (CommonClientResponseBody.Binary) response.getBody();
    var task = body.getContentDescriptor().getContent();
    if (body.getDeclaredSize() != null && body.getDeclaredSize() > LIMIT) {
      task.cancel(new CancellationException("Binary response exceeds 64 MiB; not retried"));
      tooLarge();
      return;
    }
    task.start();
    while (!task.isCompleted()) {
      if (body.getContentDescriptor().getBytesReceived().getValue().getValue() > LIMIT) {
        task.cancel(new CancellationException("Binary response exceeds 64 MiB; not retried"));
        tooLarge();
        return;
      }
      Thread.sleep(5);
    }
    if (!task.isCancelled() && task.getCompleted().getSize() > LIMIT) {
      tooLarge();
    }
  }
  private static final ThreadLocal<com.intellij.httpClient.http.request.HttpRequestVariableSubstitutor> substitution = new ThreadLocal<>();
  /** Resolve the authored export with the native scope before response code changes it. */
  public static com.intellij.httpClient.execution.RestClientRequest toRestClientRequest(Object input, Object substitutor)
      throws com.intellij.httpClient.http.request.run.HttpRequestValidationException {
    var request = (com.intellij.httpClient.execution.RestClientRequest) original("toRestClientRequest", new Class<?>[]{
        com.intellij.httpClient.http.request.psi.HttpRequest.class,
        com.intellij.httpClient.http.request.HttpRequestVariableSubstitutor.class}, input, substitutor);
    initRequest(request);
    if (request.outputFilePath == null) return request;
    Path config = Path.of(System.getProperty("piper.http.work"), "export-targets.json");
    if (!Files.exists(config)) return request;
    try {
      if (Files.size(config) > (4L << 20)) throw new java.io.IOException("Export configuration limit");
      var targets = com.google.gson.JsonParser.parseString(Files.readString(config)).getAsJsonObject();
      String stage = request.outputFilePath.getPath().toAbsolutePath().normalize().toString();
      for (var entry : targets.entrySet()) {
        if (!stage.equals(entry.getKey()) && !stage.matches(java.util.regex.Pattern.quote(entry.getKey()) + "-[1-9][0-9]*")) continue;
        String path = com.intellij.httpClient.http.request.HttpRequestPsiConverter.fillVariables(entry.getValue().getAsString(),
            (com.intellij.httpClient.http.request.HttpRequestVariableSubstitutor) substitutor,
            ((com.intellij.httpClient.http.request.psi.HttpRequest) input).getProject(),
            com.intellij.httpClient.http.request.HttpRequestVariableSubstitutionKey.insideElementKeyFactory(
                ((com.intellij.httpClient.http.request.psi.HttpRequest) input).getOutputFile()));
        if (path.length() > 4096) throw new java.io.IOException("Export path limit");
        writeExportTarget(stage, path);
        break;
      }
      return request;
    } catch (Exception error) {
      throw new com.intellij.httpClient.http.request.run.HttpRequestValidationException("Native export path could not be resolved; response not published");
    }
  }
  private static void writeExportTarget(String stage, String path) throws java.io.IOException {
    if (path.length() > 4096) throw new java.io.IOException("Export path limit");
    var metadata = new com.google.gson.JsonObject();
    metadata.addProperty("path", path);
    Path destination = Path.of(stage + ".target.json");
    Path temporary = Files.createTempFile(destination.getParent(), ".target-", ".json");
    Files.writeString(temporary, metadata.toString());
    Files.move(temporary, destination, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
  }
  public static java.io.File fileToSend(Object input, Object substitutor)
      throws com.intellij.httpClient.http.request.run.HttpRequestValidationException {
    var previous = substitution.get();
    substitution.set((com.intellij.httpClient.http.request.HttpRequestVariableSubstitutor) substitutor);
    try {
      return (java.io.File) original("getFileToSend", new Class<?>[]{
          com.intellij.httpClient.http.request.psi.HttpInputFile.class,
          com.intellij.httpClient.http.request.HttpRequestVariableSubstitutor.class}, input, substitutor);
    } finally { substitution.set(previous); }
  }
  public static String textToSend(Object file, Object messages, Object substitutor)
      throws com.intellij.httpClient.http.request.run.HttpRequestValidationException {
    var previous = substitution.get();
    substitution.set((com.intellij.httpClient.http.request.HttpRequestVariableSubstitutor) substitutor);
    try {
      return (String) original("getTextToSend", new Class<?>[]{com.intellij.psi.PsiFile.class,
          java.util.List.class, com.intellij.httpClient.http.request.HttpRequestVariableSubstitutor.class},
          file, messages, substitutor);
    } finally { substitution.set(previous); }
  }
  private static Object original(String name, Class<?>[] types, Object... arguments)
      throws com.intellij.httpClient.http.request.run.HttpRequestValidationException {
    try {
      var method = com.intellij.httpClient.http.request.HttpRequestPsiConverter.class.getDeclaredMethod("__piper_" + name, types);
      method.setAccessible(true);
      return method.invoke(null, arguments);
    } catch (java.lang.reflect.InvocationTargetException error) {
      if (error.getCause() instanceof com.intellij.httpClient.http.request.run.HttpRequestValidationException)
        throw (com.intellij.httpClient.http.request.run.HttpRequestValidationException) error.getCause();
      throw new com.intellij.httpClient.http.request.run.HttpRequestValidationException("Native input resolution failed");
    } catch (ReflectiveOperationException error) {
      throw new com.intellij.httpClient.http.request.run.HttpRequestValidationException("Native input adapter is unavailable");
    }
  }
  /** Resolve one runtime input through the host's per-run project broker. */
  public static String resolveFileUrl(Object value)
      throws com.intellij.httpClient.http.request.run.HttpRequestValidationException {
    var input = (com.intellij.httpClient.http.request.psi.HttpInputFile) value;
    String path = input.getFilePath() == null ? "" : input.getFilePath().getPresentablePath();
    if (path.startsWith("piper-runtime:")) {
      String authored = path;
      boolean submitted = false;
      try {
        var message = new com.google.gson.JsonObject();
        message.addProperty("kind", "file");
        if (substitution.get() == null) throw new java.io.IOException("Runtime input substitution context is unavailable");
        message.addProperty("iteration", substitution.get().iteration());
        path = com.intellij.httpClient.http.request.HttpRequestPsiConverter.fillVariables(path, substitution.get(), input.getProject(),
            com.intellij.httpClient.http.request.HttpRequestVariableSubstitutionKey.insideExternalFileKeyFactory(path));
        message.addProperty("reference", path.substring("piper-runtime:".length()));
        submitted = true;
        return Path.of(rpc(message).get("path").getAsString()).toUri().toString();
      } catch (Exception error) {
        if (!submitted) {
          // Bind substitution errors to their authored request without requesting
          // any file capability. The host refuses this message and records identity.
          var aborted = new com.google.gson.JsonObject();
          aborted.addProperty("kind", "abort");
          aborted.addProperty("reference", authored.substring("piper-runtime:".length()));
          try { rpc(aborted); } catch (Exception ignored) { }
        }
        // Native validation failures otherwise continue with later requests. End
        // ONLY this dedicated confined JVM; host preserves prior HTTP results.
        Runtime.getRuntime().halt(87);
        throw new com.intellij.httpClient.http.request.run.HttpRequestValidationException(
            "HTTP runtime dependency refused; earlier requests may have run; not retried");
      }
    }
    return (String) original("resolveFileUrl", new Class<?>[]{
        com.intellij.httpClient.http.request.psi.HttpInputFile.class}, input);
  }

  private static int sequence;
  static synchronized com.google.gson.JsonObject rpc(com.google.gson.JsonObject message) throws Exception {
    int number = sequence++;
    if (number >= 65536) throw new java.io.IOException("HTTP runtime request limit");
    Path directory = Path.of(System.getProperty("piper.http.work"), "runtime", "broker");
    Path temporary = directory.resolve("request-" + number + ".tmp");
    Path target = directory.resolve("request-" + number + ".json");
    String payload = message.toString();
    if (payload.getBytes(java.nio.charset.StandardCharsets.UTF_8).length > 65536)
      throw new java.io.IOException("HTTP runtime message limit");
    Files.writeString(temporary, payload, StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE);
    Files.move(temporary, target, StandardCopyOption.ATOMIC_MOVE);
    Path reply = directory.resolve("reply-" + number + ".json");
    // Selected by the host launcher; the host also enforces the whole run's remaining deadline.
    long deadline = System.nanoTime() + java.util.concurrent.TimeUnit.MILLISECONDS.toNanos(
        Long.getLong("piper.http.rpc.timeout.ms", 3600000L));
    while (!Files.exists(reply, LinkOption.NOFOLLOW_LINKS)) {
      if (System.nanoTime() >= deadline) throw new java.io.IOException("HTTP runtime deadline");
      Thread.sleep(10);
    }
    if (!Files.isRegularFile(reply, LinkOption.NOFOLLOW_LINKS) || Files.size(reply) > 65536)
      throw new java.io.IOException("Invalid HTTP runtime reply");
    var result = com.google.gson.JsonParser.parseString(Files.readString(reply)).getAsJsonObject();
    Files.delete(reply);
    if (!result.has("ok") || !result.get("ok").getAsBoolean())
      throw new java.io.IOException("HTTP runtime dependency refused");
    return result;
  }
  private static final java.util.Map<com.intellij.httpClient.execution.RestClientRequest, java.util.List<String>> explicitCookies =
      java.util.Collections.synchronizedMap(new java.util.IdentityHashMap<>());
  /** Snapshot authored headers before the CLI merges its attribute-less cookie storage. */
  public static Object convertFromHttpRequest(Object input, Object substitutor, Object builder)
      throws com.intellij.httpClient.http.request.run.HttpRequestValidationException {
    Object result = original("convertFromHttpRequest", new Class<?>[]{com.intellij.httpClient.http.request.psi.HttpRequest.class,
        com.intellij.httpClient.http.request.HttpRequestVariableSubstitutor.class,
        com.intellij.httpClient.converters.RequestBuilder.class}, input, substitutor, builder);
    if (result instanceof com.intellij.httpClient.execution.RestClientRequest)
      rememberCookies((com.intellij.httpClient.execution.RestClientRequest) result);
    return result;
  }
  private static boolean cookieBroker() {
    return Files.isRegularFile(Path.of(System.getProperty("piper.http.work"), "cookie-broker.json"), LinkOption.NOFOLLOW_LINKS);
  }
  private static void rememberCookies(com.intellij.httpClient.execution.RestClientRequest request) {
    if (!cookieBroker()) return;
    var values = new java.util.ArrayList<String>();
    for (var header : request.headers)
      if (header.getKey().equalsIgnoreCase("Cookie")) values.add(header.getValue());
    explicitCookies.put(request, values);
  }
  private static Object invokeOriginal(Class<?> owner, Object receiver, String name, Class<?>[] types, Object... args)
      throws Exception {
    var method = owner.getDeclaredMethod("__piper_" + name, types);
    method.setAccessible(true);
    try { return method.invoke(receiver, args); }
    catch (java.lang.reflect.InvocationTargetException error) {
      // Native coroutine/JS handlers recognize their own exception types (exit,
      // cancellation and transport failures). Reflection must preserve those.
      if (error.getCause() instanceof Exception cause) throw cause;
      if (error.getCause() instanceof Error cause) throw cause;
      throw error;
    }
  }
  private static String cookieUrl(String value) throws Exception {
    var uri = new java.net.URI(value);
    // Cookies do not depend on query, fragment or URL user info. Never persist those secrets.
    return new java.net.URI(uri.getScheme(), null, uri.getHost(), uri.getPort(), uri.getPath(), null, null).toASCIIString();
  }
  /** Capture every actual response origin, including redirects, before the next selection. */
  public static reactor.netty.http.client.HttpClient nettyClient(Object value, Object url, boolean legacy) throws Exception {
    var request = (com.intellij.httpClient.execution.RestClientRequest) value;
    var client = (reactor.netty.http.client.HttpClient) invokeOriginal(
        com.intellij.httpClient.execution.impl.engineV2.NettyHttpClientKt.class, null, "createReactorNettyClient",
        new Class<?>[]{com.intellij.httpClient.execution.RestClientRequest.class, java.net.URL.class, boolean.class}, request, url, legacy);
    Path tunnel = Path.of(System.getProperty("piper.http.work"), "tunnel-tls.json");
    if (!Boolean.getBoolean("piper.http.insecure") && Files.exists(tunnel, LinkOption.NOFOLLOW_LINKS)) {
      if (!Files.isRegularFile(tunnel, LinkOption.NOFOLLOW_LINKS) || Files.size(tunnel) > 1024)
        throw new java.io.IOException("Invalid tunnel TLS configuration");
      var ports = new java.util.HashSet<Integer>();
      for (var port : com.google.gson.JsonParser.parseString(Files.readString(tunnel)).getAsJsonObject().getAsJsonArray("ports")) {
        int number = port.getAsInt();
        if (number < 1 || number > 65535) throw new java.io.IOException("Invalid tunnel TLS port");
        ports.add(number);
      }
      var trust = new TunnelTrust(ports);
      boolean http2 = java.util.Arrays.asList(client.configuration().protocols()).contains(reactor.netty.http.HttpProtocol.H2);
      reactor.netty.tcp.SslProvider.ProtocolSslContextSpec spec = http2
          ? reactor.netty.http.Http2SslContextSpec.forClient().configure(builder -> builder.sslProvider(io.netty.handler.ssl.SslProvider.JDK).trustManager(trust))
          : reactor.netty.http.Http11SslContextSpec.forClient().configure(builder -> builder.sslProvider(io.netty.handler.ssl.SslProvider.JDK).trustManager(trust));
      if (request.sslConfiguration != null) request.sslConfiguration.applyTo(new TunnelTls(spec));
      client = client.secure(reactor.netty.tcp.SslProvider.builder().sslContext(spec).handlerConfigurator(handler -> {
        var engine = handler.engine();
        var parameters = engine.getSSLParameters();
        parameters.setEndpointIdentificationAlgorithm(trust.local(engine.getPeerHost(), engine.getPeerPort()) ? "" : "HTTPS");
        engine.setSSLParameters(parameters);
      }).build());
    }
    if (!cookieBroker()) return client;
    java.util.function.BiConsumer<reactor.netty.http.client.HttpClientResponse, reactor.netty.Connection> save = (response, connection) -> {
      if (!request.config.isSaveCookies()) return;
      var headers = response.responseHeaders().getAll("Set-Cookie");
      if (headers.isEmpty()) return;
      try {
        var message = new com.google.gson.JsonObject();
        message.addProperty("kind", "cookie_save");
        message.addProperty("url", cookieUrl(response.resourceUrl()));
        var list = new com.google.gson.JsonArray();
        for (String header : headers) list.add(header);
        message.add("headers", list);
        rpc(message);
      } catch (Exception error) { Runtime.getRuntime().halt(89); }
    };
    return client.doOnResponse(save).doOnRedirect(save);
  }
  /** Keep the native client identity and HTTP protocol while narrowing tunnel trust. */
  private static final class TunnelTls implements com.intellij.httpClient.execution.ssl.SslConfigurationTarget<kotlin.Unit> {
    private final reactor.netty.tcp.SslProvider.ProtocolSslContextSpec spec;
    TunnelTls(reactor.netty.tcp.SslProvider.ProtocolSslContextSpec spec) { this.spec = spec; }
    @Override public void configureKeyStore(java.security.KeyStore store, String password) {
      try {
        var keys = javax.net.ssl.KeyManagerFactory.getInstance(javax.net.ssl.KeyManagerFactory.getDefaultAlgorithm());
        keys.init(store, password == null ? new char[0] : password.toCharArray());
        spec.configure(builder -> builder.keyManager(keys));
      } catch (Exception error) { throw new IllegalStateException("Tunnel client identity could not be loaded"); }
    }
    @Override public void configureTrustManager(boolean ignored) { }
    @Override public kotlin.Unit finish() { return kotlin.Unit.INSTANCE; }
  }
  /** Trust exceptions are checked at every handshake, including native redirects. */
  private static final class TunnelTrust extends javax.net.ssl.X509ExtendedTrustManager {
    private final java.util.Set<Integer> ports;
    private final javax.net.ssl.X509ExtendedTrustManager verified;
    TunnelTrust(java.util.Set<Integer> ports) throws java.security.GeneralSecurityException {
      this.ports = java.util.Set.copyOf(ports);
      var factory = javax.net.ssl.TrustManagerFactory.getInstance(javax.net.ssl.TrustManagerFactory.getDefaultAlgorithm());
      factory.init((java.security.KeyStore) null);
      verified = java.util.Arrays.stream(factory.getTrustManagers())
          .filter(item -> item instanceof javax.net.ssl.X509ExtendedTrustManager)
          .map(item -> (javax.net.ssl.X509ExtendedTrustManager) item).findFirst()
          .orElseThrow(() -> new java.security.KeyStoreException("System TLS verifier unavailable"));
    }
    boolean local(String host, int port) {
      return ports.contains(port) && ("localhost".equalsIgnoreCase(host) || "127.0.0.1".equals(host) || "::1".equals(host) || "[::1]".equals(host));
    }
    @Override public void checkServerTrusted(java.security.cert.X509Certificate[] chain, String type, javax.net.ssl.SSLEngine engine) throws java.security.cert.CertificateException {
      if (!local(engine.getPeerHost(), engine.getPeerPort())) verified.checkServerTrusted(chain, type, engine);
    }
    @Override public void checkServerTrusted(java.security.cert.X509Certificate[] chain, String type, java.net.Socket socket) throws java.security.cert.CertificateException {
      // Netty uses SSLEngine; a socket without that destination context gets no exception.
      verified.checkServerTrusted(chain, type, socket);
    }
    @Override public void checkServerTrusted(java.security.cert.X509Certificate[] chain, String type) throws java.security.cert.CertificateException { verified.checkServerTrusted(chain, type); }
    @Override public void checkClientTrusted(java.security.cert.X509Certificate[] chain, String type, javax.net.ssl.SSLEngine engine) throws java.security.cert.CertificateException { verified.checkClientTrusted(chain, type, engine); }
    @Override public void checkClientTrusted(java.security.cert.X509Certificate[] chain, String type, java.net.Socket socket) throws java.security.cert.CertificateException { verified.checkClientTrusted(chain, type, socket); }
    @Override public void checkClientTrusted(java.security.cert.X509Certificate[] chain, String type) throws java.security.cert.CertificateException { verified.checkClientTrusted(chain, type); }
    @Override public java.security.cert.X509Certificate[] getAcceptedIssuers() { return verified.getAcceptedIssuers(); }
  }
  /** Native Biscuit loses Secure/hostOnly. The host selects; upstream only encodes. */
  @SuppressWarnings("unchecked")
  public static java.util.List<com.intellij.httpClient.execution.RestClientRequest.Biscuit> selectCookies(Object jar, Object target)
      throws Exception {
    if (!cookieBroker()) return (java.util.List<com.intellij.httpClient.execution.RestClientRequest.Biscuit>) invokeOriginal(
        jar.getClass(), jar, "selectCookiesFor", new Class<?>[]{java.net.URL.class}, target);
    try {
      var field = jar.getClass().getDeclaredField("request");
      field.setAccessible(true);
      var request = (com.intellij.httpClient.execution.RestClientRequest) field.get(jar);
      var url = (java.net.URL) target;
      var message = new com.google.gson.JsonObject();
      message.addProperty("kind", "cookie_select");
      message.addProperty("url", cookieUrl(url.toString()));
      var result = new java.util.ArrayList<com.intellij.httpClient.execution.RestClientRequest.Biscuit>();
      for (var element : rpc(message).getAsJsonArray("cookies")) {
        var item = element.getAsJsonObject();
        result.add(new com.intellij.httpClient.execution.RestClientRequest.Biscuit(item.get("name").getAsString(),
            item.get("value").getAsString(), item.get("domain").getAsString(), item.get("path").getAsString(), Long.MAX_VALUE));
      }
      var original = new java.net.URL(request.getURL());
      // Preserve authored Cookie values only for their original host and without a TLS downgrade.
      if (original.getHost().equalsIgnoreCase(url.getHost()) &&
          (!original.getProtocol().equals("https") || url.getProtocol().equals("https"))) {
        for (String header : explicitCookies.getOrDefault(request, java.util.List.of())) {
          for (String pair : header.split(";")) {
            int at = pair.indexOf('=');
            if (at < 1) continue;
            String name = pair.substring(0, at).trim(), cookie = pair.substring(at + 1).trim();
            result.removeIf(item -> item.getName().equals(name));
            result.add(new com.intellij.httpClient.execution.RestClientRequest.Biscuit(name, cookie, url.getHost(), "/", Long.MAX_VALUE));
          }
        }
      }
      return result;
    } catch (Exception error) {
      Runtime.getRuntime().halt(89);
      throw error;
    }
  }
  /** Native collection copies omit --insecure. Retain only the host's explicit per-run choice. */
  public static void initRequest(Object value) {
    var request = (com.intellij.httpClient.execution.RestClientRequest) value;
    Path config = Path.of(System.getProperty("piper.http.work"), "ssl-client.json");
    if (Files.exists(config, LinkOption.NOFOLLOW_LINKS)) {
      try {
        if (!Files.isRegularFile(config, LinkOption.NOFOLLOW_LINKS) || Files.size(config) > 16384)
          throw new java.io.IOException("Client certificate configuration limit");
        var data = com.google.gson.JsonParser.parseString(Files.readString(config)).getAsJsonObject();
        var certificate = data.getAsJsonObject("certificate");
        var key = data.getAsJsonObject("key");
        request.sslConfiguration = new com.intellij.httpClient.execution.ssl.SslConfiguration.Configured(
            new com.intellij.httpClient.execution.ssl.SslCertificate(Path.of(certificate.get("path").getAsString()),
                com.intellij.httpClient.execution.ssl.CertificateFormat.valueOf(certificate.get("format").getAsString())),
            key == null ? null : new com.intellij.httpClient.execution.ssl.SslCertificatePrivateKey(Path.of(key.get("path").getAsString()),
                com.intellij.httpClient.execution.ssl.PrivateKeyFormat.valueOf(key.get("format").getAsString())),
            Boolean.getBoolean("piper.http.insecure"), null,
            com.intellij.httpClient.execution.ssl.SslConfigurationContext.Default.INSTANCE);
      } catch (Exception error) {
        // Never continue another request with a silently missing client identity.
        Runtime.getRuntime().halt(88);
      }
    } else if (Boolean.getBoolean("piper.http.insecure")) {
      request.sslConfiguration = new com.intellij.httpClient.execution.ssl.SslConfiguration.Configured(
          null, null, true, null, com.intellij.httpClient.execution.ssl.SslConfigurationContext.Default.INSTANCE);
    }
  }
  public static Path copyFile(Path from, Path to, CopyOption[] ignored) throws java.io.IOException {
    if (Files.size(from) > LIMIT) throw new java.io.IOException("Binary response exceeds 64 MiB; not retried");
    return Files.copy(from, to, StandardCopyOption.REPLACE_EXISTING);
  }
}
