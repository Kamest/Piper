"""m2settings: the sandbox Maven settings derived from a host ~/.m2/settings.xml."""
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "piper"))

import _env  # noqa: E402,F401  (first: an isolated home before any Piper module)
import m2settings  # noqa: E402

HOST_XML = """<?xml version="1.0" encoding="utf-8"?>
<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0">
  <servers>
    <server><id>acme-registry</id><configuration><httpHeaders><property>
      <name>Deploy-Token</name><value>TOKEN-VALUE</value></property></httpHeaders></configuration></server>
    <server><id>release</id><username>build-user</username><password>NEXUS-PW</password></server>
    <server><id>website</id><username>me</username><passphrase>SSH-PASS</passphrase></server>
    <server><id>ossrh</id><username>u</username><password>OSSRH-PW</password></server>
    <server><id>jdkrepo</id><username>j</username><password>JDK-PW</password></server>
    <server><id>deployrepo</id><username>d</username><password>DEPLOY-PW</password></server>
  </servers>
  <proxies><proxy><id>corp</id><active>true</active><host>proxy.example.com</host><port>3128</port></proxy></proxies>
  <profiles>
    <profile>
      <id>basic</id>
      <repositories>
        <repository><id>acme-registry</id><url>https://gitlab.example.com/api/v4/projects/6/packages/maven</url></repository>
        <repository><id>release</id><url>https://nexus.example.com/content/groups/releases/</url></repository>
      </repositories>
      <pluginRepositories>
        <pluginRepository><id>release</id><url>https://nexus.example.com/content/groups/plugin-release-group/</url></pluginRepository>
      </pluginRepositories>
      <properties>
        <SKIP_TESTS>true</SKIP_TESTS>
        <JAVA_COMPILER>/bin/javac</JAVA_COMPILER>
        <JAVA_21_HOME>/Users/dev/Library/Java/temurin-21/Contents/Home</JAVA_21_HOME>
        <JAVA_17_HOME>/Users/dev/Library/Java/temurin-17/Contents/Home</JAVA_17_HOME>
        <JAVA_11_HOME>/Users/dev/Library/Java/temurin-11/Contents/Home</JAVA_11_HOME>
        <JAVA_1_8_HOME>/Users/dev/Library/Java/corretto-8/Contents/Home</JAVA_1_8_HOME>
      </properties>
    </profile>
    <profile>
      <id>ossrh</id>
      <activation><activeByDefault>false</activeByDefault></activation>
      <properties><gpg.passphrase>GPG-PASS</gpg.passphrase></properties>
    </profile>
    <profile>
      <id>bydefault</id>
      <activation><activeByDefault>true</activeByDefault></activation>
      <properties><X>1</X></properties>
    </profile>
    <profile>
      <id>jdk17</id>
      <activation><jdk>17</jdk></activation>
      <repositories><repository><id>jdkrepo</id><url>https://nexus.example.com/jdk</url></repository></repositories>
    </profile>
    <profile>
      <id>deploy</id>
      <repositories><repository><id>deployrepo</id><url>https://nexus.example.com/deploy</url></repository></repositories>
    </profile>
    <profile>
      <id>secrets</id>
      <properties><some.passphrase>PROP-PASS</some.passphrase></properties>
    </profile>
  </profiles>
  <activeProfiles><activeProfile>basic</activeProfile><activeProfile>gone</activeProfile></activeProfiles>
  <pluginGroups><pluginGroup>com.atlassian.maven.plugins</pluginGroup></pluginGroups>
</settings>
"""

JDKS = {17: "/usr/lib/jvm/java-17-openjdk-arm64", 21: "/usr/lib/jvm/java-21-openjdk-arm64"}


class M2SettingsTest(unittest.TestCase):
    def setUp(self):
        self.notes = []
        self.xml = m2settings.generate_settings(HOST_XML, JDKS, self.notes)
        self.root = ET.fromstring(self.xml)

    def test_credentials_of_used_repositories_stay_others_leave(self):
        ids = [s.findtext("id") for s in self.root.findall("./servers/server")]
        self.assertEqual(ids, ["acme-registry", "release", "jdkrepo", "deployrepo"])
        self.assertIn("TOKEN-VALUE", self.xml)        # the gitlab.example.com Deploy-Token header
        self.assertIn("NEXUS-PW", self.xml)
        for gone in ("SSH-PASS", "OSSRH-PW", "GPG-PASS", "PROP-PASS", "proxy.example.com"):
            self.assertNotIn(gone, self.xml)
        self.assertIsNone(self.root.find("proxies"))
        self.assertNotIn("xmlns", self.xml)           # plain tags, like a hand-written settings.xml

    def test_profiles_active_only(self):
        ids = [p.findtext("id") for p in self.root.findall("./profiles/profile")]
        # jdk-activated and -P profiles with repositories stay (the sandbox may activate them); a
        # properties-only profile nothing activates goes (that is where passphrases live)
        self.assertEqual(ids, ["basic", "bydefault", "jdk17", "deploy"])
        self.assertEqual([a.text for a in self.root.findall("./activeProfiles/activeProfile")], ["basic"])
        self.assertEqual([g.text for g in self.root.findall("./pluginGroups/pluginGroup")], ["com.atlassian.maven.plugins"])
        self.assertTrue(any("profile ossrh dropped" in n for n in self.notes))
        self.assertTrue(any("profile secrets dropped" in n for n in self.notes))
        self.assertTrue(any("server website dropped" in n for n in self.notes))

    def test_java_homes_point_at_the_image_jdks(self):
        props = self.root.find("./profiles/profile/properties")
        self.assertEqual(props.findtext("JAVA_21_HOME"), JDKS[21])
        self.assertEqual(props.findtext("JAVA_17_HOME"), JDKS[17])
        self.assertEqual(props.findtext("JAVA_11_HOME"), JDKS[17])    # nearest higher
        self.assertEqual(props.findtext("JAVA_1_8_HOME"), JDKS[17])
        self.assertEqual(props.findtext("JAVA_COMPILER"), "/bin/javac")   # a suffix, untouched
        self.assertEqual(props.findtext("SKIP_TESTS"), "true")
        self.assertEqual(self.root.findtext("localRepository"), m2settings.LOCAL_REPO)

    def test_no_jdk_listed_falls_back_to_default_java(self):
        xml = m2settings.generate_settings(HOST_XML, {})
        self.assertIn(m2settings.DEFAULT_JDK, xml)
        self.assertEqual(m2settings.pick_jdk(25, JDKS), JDKS[21])    # nothing higher: the highest

    def test_jdks_from_dependencies(self):
        deps = ["python3", "openjdk-21-jdk-headless", "openjdk-17-jdk", "temurin-11-jdk", "maven"]
        self.assertEqual(m2settings.sandbox_jdks(deps, "amd64"),
                         {11: "/usr/lib/jvm/temurin-11-jdk-amd64", 17: "/usr/lib/jvm/java-17-openjdk-amd64",
                          21: "/usr/lib/jvm/java-21-openjdk-amd64"})
        self.assertIn(m2settings.host_arch(), ("arm64", "amd64", m2settings.host_arch()))

    def test_toolchains(self):
        root = ET.fromstring(m2settings.generate_toolchains(JDKS))
        self.assertEqual([t.findtext("provides/version") for t in root], ["17", "21"])
        self.assertEqual(root[1].findtext("configuration/jdkHome"), JDKS[21])

    def test_invalid_xml_raises(self):
        with self.assertRaises(ET.ParseError):
            m2settings.generate_settings("<settings><servers>", JDKS)

    def test_plan_respects_policy_host_files_and_personal_includes(self):
        with tempfile.TemporaryDirectory() as home:
            box = os.path.join(home, "box")
            self.assertEqual(m2settings.plan({}, home, box), [])            # no host ~/.m2/settings.xml
            os.makedirs(os.path.join(home, ".m2"))
            with open(os.path.join(home, ".m2", "settings.xml"), "w") as f:
                f.write(HOST_XML)
            self.assertEqual(m2settings.plan({}, home, box, enabled=False), [])
            p = m2settings.plan({}, home, box)
            self.assertEqual([(t, k) for _h, t, k in p],
                             [("~/.m2/settings.xml", "settings"), ("~/.m2/toolchains.xml", "toolchains"),
                              ("~/.m2/repository", "repository")])   # no cache include: the host's cache
            self.assertEqual(p[0][0], os.path.join(box, "piper", "maven-settings.xml"))
            self.assertEqual(p[2][0], os.path.join(home, ".m2", "repository"))
            with open(os.path.join(home, ".m2", "settings-security.xml"), "w") as f:
                f.write("<settingsSecurity/>")
            self.assertIn("security", [k for _h, _t, k in m2settings.plan({}, home, box)])
            # the developer's own include onto ~/.m2/settings.xml wins (base or a profile, $HOME or ~)
            own = {"files": {"include": {"~/.m2/agent-settings.xml": "$HOME/.m2/settings.xml",
                                          "~/.m2/repository": "~/.m2/repository"}}}
            self.assertEqual([k for _h, _t, k in m2settings.plan(own, home, box)], ["toolchains"])
            prof = {"profiles": {"x": {"files": {"include": {"~/t.xml": {"path": "~/.m2/toolchains.xml"}}}}}}
            self.assertEqual([k for _h, _t, k in m2settings.plan(prof, home, box)], ["settings", "security", "repository"])
            whole = {"files": {"include": {"~/.m2": "~/.m2"}}}   # a directory above every target
            self.assertEqual(m2settings.plan(whole, home, box), [])
            proj = {"files": {"include": {"$PROJECT_PATH/ci/s.xml": "~/.m2/settings.xml"}}}   # the project's own
            self.assertEqual([k for _h, _t, k in m2settings.plan({}, home, box, extra_docs=[proj])],
                             ["toolchains", "repository"])


if __name__ == "__main__":
    unittest.main()
