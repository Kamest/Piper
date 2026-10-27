package piper.bridge;

import com.google.gson.*;
import com.oracle.js.parser.*;
import com.oracle.js.parser.ir.*;
import com.oracle.js.parser.ir.visitor.NodeVisitor;
import java.nio.file.*;

/** Parse-only dependency discovery. Never evaluates JavaScript or opens a project path. */
public final class PiperModules {
  public static JsonObject parse(String name, String text) {
    var result = new JsonObject();
    var refs = new JsonArray();
    var computed = new JsonArray();
    result.add("refs", refs);
    result.add("computed", computed);
    var options = ScriptEnvironment.builder()
        .ecmaScriptVersion(com.oracle.truffle.js.runtime.JSContextOptions.ECMASCRIPT_VERSION.getDefaultValue())
        .strict(false).shebang(true).allowBigInt(true).annexB(true).classFields(true)
        .privateFieldsIn(true).topLevelAwait(true).build();
    try {
      var source = Source.sourceFor(name, text);
      var tree = new Parser(options, source, new ErrorManager.ThrowErrorManager()).parseModule(name);
      var ordered = new java.util.TreeMap<Integer, JsonObject>();
      var visitor = new NodeVisitor<LexicalContext>(new LexicalContext()) {
        private void reference(LiteralNode<?> node) {
          if (node == null || !node.isString()) return;
          var ref = new JsonObject();
          ref.addProperty("ref", node.getString());
          // Parser offsets count UTF-16 code units, just like JavaScript strings.
          ref.addProperty("start", node.getStart());
          ref.addProperty("end", node.getFinish());
          ordered.put(node.getStart(), ref);
        }
        @Override public boolean enterImportNode(ImportNode node) {
          reference(node.getModuleSpecifier());
          return true;
        }
        @Override public boolean enterFromNode(FromNode node) {
          reference(node.getModuleSpecifier());
          return true;
        }
        @Override public boolean enterCallNode(CallNode node) {
          if (node.isImport()) {
            if (!node.getArgs().isEmpty() && node.getArgs().get(0) instanceof LiteralNode<?> literal && literal.isString())
              reference(literal);
            else computed.add(node.getLineNumber());
          }
          return true;
        }
      };
      tree.accept(visitor);
      for (var node : tree.getModule().getImports()) node.accept(visitor);
      for (var node : tree.getModule().getExports()) node.accept(visitor);
      for (var ref : ordered.values()) refs.add(ref);
    } catch (ParserException error) {
      // Parser messages can echo source/credentials. Only structured location crosses the boundary.
      result.addProperty("error_line", Math.max(1, error.getLineNumber()));
    }
    return result;
  }

  /** Fixed, host-selected entry point inside exactly the existing native HTTP jail. */
  public static void main(String[] args) throws Exception {
    if (args.length != 0) throw new IllegalArgumentException("Invalid parse-only invocation");
    Path work = Path.of(System.getProperty("piper.http.work"));
    for (int index = 0; index < 65536; index++) {
      var message = new JsonObject();
      message.addProperty("kind", "parser_next");
      message.addProperty("index", index);
      var next = PiperBinary.rpc(message);
      if (next.get("done").getAsBoolean()) return;
      Path path = Path.of(next.get("path").getAsString());
      String text = Files.readString(path);
      var result = parse(next.get("name").getAsString(), text);
      Files.writeString(work.resolve("runtime/tmp/parse-" + index + ".json"), result.toString(),
          StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE);
    }
    throw new IllegalStateException("Module parser source limit");
  }
}
