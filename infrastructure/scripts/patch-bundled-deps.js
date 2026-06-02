/**
 * Patches bundled vulnerable dependencies inside aws-cdk-lib.
 * This runs automatically after `npm install` via the postinstall hook.
 *
 * Why: aws-cdk-lib bundles brace-expansion@5.0.5 which has CVE-2026-45149.
 * The fix (5.0.6) exists but AWS hasn't released a patched CDK yet.
 * This script replaces the vulnerable version with the patched one.
 */

const { execSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const PATCHES = [
  {
    package: "brace-expansion",
    vulnerablePath: "node_modules/aws-cdk-lib/node_modules/brace-expansion",
    fixedVersion: "5.0.6",
  },
];

for (const patch of PATCHES) {
  const targetDir = path.resolve(__dirname, "..", patch.vulnerablePath);

  if (!fs.existsSync(targetDir)) {
    console.log(`[patch] ${patch.package}: not found at ${targetDir}, skipping`);
    continue;
  }

  // Check current version
  const pkgJsonPath = path.join(targetDir, "package.json");
  const currentPkg = JSON.parse(fs.readFileSync(pkgJsonPath, "utf-8"));

  if (currentPkg.version === patch.fixedVersion) {
    console.log(`[patch] ${patch.package}@${patch.fixedVersion}: already patched ✓`);
    continue;
  }

  console.log(
    `[patch] ${patch.package}: replacing ${currentPkg.version} → ${patch.fixedVersion}`
  );

  // Remove the vulnerable version
  fs.rmSync(targetDir, { recursive: true, force: true });

  // Install the fixed version into that exact location
  execSync(
    `npm pack ${patch.package}@${patch.fixedVersion} --pack-destination /tmp`,
    { stdio: "pipe" }
  );

  const tarball = `/tmp/${patch.package}-${patch.fixedVersion}.tgz`;
  fs.mkdirSync(targetDir, { recursive: true });

  execSync(`tar -xzf ${tarball} --strip-components=1 -C ${targetDir}`, {
    stdio: "pipe",
  });

  // Clean up
  fs.rmSync(tarball, { force: true });

  // Verify
  const patchedPkg = JSON.parse(fs.readFileSync(pkgJsonPath, "utf-8"));
  console.log(`[patch] ${patch.package}@${patchedPkg.version}: patched ✓`);
}
