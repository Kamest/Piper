import fs from 'node:fs/promises';
import { createReadStream } from 'node:fs';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

export function releaseVersion(tag) {
  const match = /^desktop-v((?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))$/.exec(tag);
  if (!match || match[0] !== tag) throw new Error('Expected a stable release tag: desktop-vX.Y.Z');
  return match[1];
}

async function sha256(file) {
  const hash = createHash('sha256');
  for await (const chunk of createReadStream(file)) hash.update(chunk);
  return hash.digest('hex');
}

export async function manifests(tag, assets, out) {
  const version = releaseVersion(tag);
  const release = `https://github.com/Kamest/Piper/releases`;
  const download = `${release}/download/${tag}`;
  const hashes = {};
  // Read every required asset before writing output; a partial download must never produce manifests.
  for (const os of ['mac', 'win']) {
    for (const arch of ['arm64', 'x64']) {
      const name = `Piper-${version}-${os}-${arch}.${os === 'mac' ? 'dmg' : 'exe'}`;
      hashes[`${os}-${arch}`] = await sha256(path.join(assets, name));
    }
  }
  const cask = `cask "piper" do
  arch arm: "arm64", intel: "x64"

  version "${version}"
  sha256 arm:   "${hashes['mac-arm64']}",
         intel: "${hashes['mac-x64']}"

  url "${release}/download/desktop-v#{version}/Piper-#{version}-mac-#{arch}.dmg"
  name "Piper"
  desc "Desktop window and tray for the independent Piper service"
  homepage "https://kamest.github.io/Piper/"

  livecheck do
    url "${release}"
    regex(/^desktop-v(\\d+(?:\\.\\d+)+)$/i)
    strategy :github_releases
  end

  depends_on macos: :ventura

  app "Piper.app"

  zap trash: [
    "~/Library/Application Support/piper-desktop",
    "~/Library/Logs/piper-desktop",
  ]

  caveats <<~EOS
    This build is not signed with an Apple Developer ID or notarized yet.
    After the first launch attempt, open System Settings > Privacy & Security > Open Anyway.
    Docker is your own prerequisite; install and start it before setting up Piper.
  EOS
end
`;
  const header = type => `# yaml-language-server: $schema=https://aka.ms/winget-manifest.${type}.1.10.0.schema.json\n\nPackageIdentifier: Kamest.Piper\nPackageVersion: ${version}\n`;
  const footer = type => `ManifestType: ${type}\nManifestVersion: 1.10.0\n`;
  const files = {
    'Kamest.Piper.yaml': header('version') + `DefaultLocale: en-US\n` + footer('version'),
    'Kamest.Piper.locale.en-US.yaml': header('defaultLocale') + `PackageLocale: en-US
Publisher: Piper contributors
PublisherUrl: https://github.com/Kamest
PublisherSupportUrl: https://github.com/Kamest/Piper/issues
PackageName: Piper
PackageUrl: https://kamest.github.io/Piper/
License: Apache-2.0
LicenseUrl: https://github.com/Kamest/Piper/blob/${tag}/LICENSE
ShortDescription: A desktop window and tray for the independent Piper service
ReleaseNotesUrl: ${release}/tag/${tag}
` + footer('defaultLocale'),
    'Kamest.Piper.installer.yaml': header('installer') + `InstallerType: nullsoft
Scope: user
InstallerSwitches:
  Silent: /S
  SilentWithProgress: /S
  Custom: /currentuser
Installers:
` + ['x64', 'arm64'].map(arch => `- Architecture: ${arch}
  InstallerUrl: ${download}/Piper-${version}-win-${arch}.exe
  InstallerSha256: ${hashes[`win-${arch}`].toUpperCase()}
`).join('') + footer('installer')
  };
  const winget = path.join(out, 'manifests', 'k', 'Kamest', 'Piper', version);
  await fs.mkdir(path.join(out, 'Casks'), { recursive: true });
  await fs.mkdir(winget, { recursive: true });
  await fs.writeFile(path.join(out, 'Casks', 'piper.rb'), cask);
  for (const [name, body] of Object.entries(files)) await fs.writeFile(path.join(winget, name), body);
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  if (process.argv.length !== 5) throw new Error('Usage: manifests.mjs <tag> <assets-dir> <out-dir>');
  await manifests(...process.argv.slice(2));
}
