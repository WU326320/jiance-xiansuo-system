'use strict';
// Preserve the existing command; share the formal suite and its isolated server.
const { spawnSync } = require('node:child_process');
const path = require('node:path');
const result = spawnSync('python', ['-X', 'utf8', path.join(__dirname, 'workbench_glass_acceptance.py')], {
  cwd: path.join(__dirname, '..'), stdio: 'inherit', shell: false,
});
if (result.error) { console.error(result.error.message); process.exitCode = 1; }
else process.exitCode = result.status ?? 1;
